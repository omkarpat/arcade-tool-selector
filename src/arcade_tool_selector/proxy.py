"""`ats-proxy`: the stdio MCP server Claude Code talks to instead of the Arcade gateway.

It serves the gateway's tools, but never its instructions: those are the project's purpose, and only the vetter
sees them. The PreToolUse hook asks the proxy to vet each call over a local socket; the proxy then runs only
calls it vetted and didn't deny, so a call that skips the hook never reaches Arcade.
Tools and purpose are read at startup and every 15 minutes.
"""

import json
import logging
import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import anyio
from anyio.abc import SocketStream
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from pydantic import TypeAdapter

from arcade_tool_selector import gateway
from arcade_tool_selector.audit import Audit
from arcade_tool_selector.cards import build_cards
from arcade_tool_selector.catalog import Catalog, tool_json
from arcade_tool_selector.claude_code.link import PromptSubmitted, VetRequest, socket_path
from arcade_tool_selector.executions import ExecutionKey
from arcade_tool_selector.judge import DecisionsJudge
from arcade_tool_selector.platform import read_purpose
from arcade_tool_selector.policy import Evidence, PolicyConfig, ScopeConfig, Vetter
from arcade_tool_selector.settings import Settings, load_settings

logger = logging.getLogger("ats.proxy")

REFRESH_INTERVAL = 15 * 60
PROMPTS_KEPT = 20
HookMessage = TypeAdapter(VetRequest | PromptSubmitted)


class Proxy:
    """The proxy's state for one Claude Code process.

    - purpose: the gateway's instructions.
    - prompts: the user's own prompts, per Claude Code session.
    - vetted: calls the hook vetted and didn't deny, awaiting execution.
    - session: the MCP client, once it has listed tools.
    """

    def __init__(self, settings: Settings):
        """Start empty: tools and purpose arrive with the first refresh."""
        self.settings = settings
        self.audit = Audit(settings.data_dir / "audit.jsonl", otel=settings.otel)
        self.judge = DecisionsJudge(settings.openai_api_key)
        self.catalog = Catalog(tools=[])
        self.purpose = ""
        self.vetter = self._vetter()
        self.prompts: dict[str, list[str]] = {}
        self.vetted: Counter[str] = Counter()
        self.session: ServerSession | None = None

    async def list_tools(self, ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        """Serve the gateway's tools, and remember the client so it can be told when they change."""
        self.session = ctx.session
        return types.ListToolsResult(tools=self.catalog.tools)

    async def call_tool(self, ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        """Run a call through the gateway only if its hook vetted it and didn't deny it.

        A `call` audit event is the key to Arcade's execution log, which holds status, timing, inputs and outputs.
        A call that needs authorization never executed, so Arcade has no record and it's audited on its own.
        """
        name, arguments = params.name, params.arguments or {}
        key = _call_key(name, arguments)
        if not self.vetted[key]:
            return _error(f"Blocked by arcade-tool-selector: {name} was not vetted by its PreToolUse hook (see .claude/settings.json).")
        self.vetted[key] -= 1
        started = ExecutionKey.for_gateway_tool(name, self.settings.user_id, datetime.now(UTC))
        result = await gateway.call_tool(self.settings, name, arguments)
        if _needs_authorization(result):
            self.audit.write("call_needs_authorization", tool=name, user_id=self.settings.user_id)
        else:
            self.audit.write("call", **started.model_dump(mode="json"), is_error=bool(result.is_error))
        return result

    async def handle_hook(self, stream: SocketStream) -> None:
        """Answer one hook message. A failed exchange blocks that hook's prompt or call, never the proxy."""
        async with stream:
            try:
                message = HookMessage.validate_json(b"".join([chunk async for chunk in stream]))
                reply = self.prompt(message) if isinstance(message, PromptSubmitted) else await self.vet(message)
                await stream.send(reply)
            except Exception:
                logger.exception("hook exchange failed")

    def prompt(self, message: PromptSubmitted) -> bytes:
        """Keep the session's latest prompts as evidence. The text stays in memory; only its size is audited."""
        prompts = self.prompts.setdefault(message.session_id, [])
        prompts.append(message.prompt)
        del prompts[:-PROMPTS_KEPT]
        self.audit.write("prompt_seen", session_id=message.session_id, chars=len(message.prompt))
        return b"{}"

    async def vet(self, message: VetRequest) -> bytes:
        """Judge a call against the purpose and the session's prompts; remember it unless denied."""
        evidence = Evidence(purpose=self.purpose, user_messages=self.prompts.get(message.session_id, []))
        decision = await self.vetter.vet(message.tool, message.arguments, evidence)
        self.audit.write("vetted", tool=message.tool, arg_keys=sorted(message.arguments), **decision.model_dump())
        if decision.action != "deny":
            self.vetted[_call_key(message.tool, message.arguments)] += 1
        return decision.model_dump_json().encode()

    def _vetter(self) -> Vetter:
        """A vetter for the current tools, with the user's email domain as the organization."""
        return Vetter(ScopeConfig(org_domains=[self.settings.org_domain]), PolicyConfig(), build_cards(self.catalog), self.judge)

    async def refresh(self) -> None:
        """Re-read the gateway's tools and purpose; audit and announce any change to the tools."""
        new = Catalog(tools=await gateway.list_tools(self.settings))
        self.purpose = await anyio.to_thread.run_sync(read_purpose, self.settings)
        if not self.purpose:
            logger.error("the gateway has no instructions: every call is denied until the project's purpose is set in the Arcade dashboard")
        before = {t.name: tool_json(t) for t in self.catalog.tools}
        after = {t.name: tool_json(t) for t in new.tools}
        self.catalog = new
        self.vetter = self._vetter()
        if before == after:
            return
        changes = {
            "added": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys()),
            "changed": sorted(n for n in after.keys() & before.keys() if after[n] != before[n]),
        }
        self.audit.write("catalog_changed", tools=len(new.tools), **changes)
        if self.session:
            await self.session.send_tool_list_changed()

    async def poll(self) -> None:
        """Refresh every 15 minutes. A failed refresh denies every call until the purpose can be read again."""
        while True:
            await anyio.sleep(REFRESH_INTERVAL)
            try:
                await self.refresh()
            except Exception:
                logger.exception("refresh failed; every call is denied until the purpose can be read again")
                self.purpose = ""

    @asynccontextmanager
    async def lifespan(self, _server: Server):
        """Load tools and purpose, then serve the hook socket and poll until the client disconnects.

        The socket is keyed by the parent pid: the proxy is Claude Code's direct child.
        """
        await self.refresh()
        path = socket_path(os.getppid())
        path.unlink(missing_ok=True)
        listener = await anyio.create_unix_listener(path)
        async with anyio.create_task_group() as tg:
            tg.start_soon(listener.serve, self.handle_hook)
            tg.start_soon(self.poll)
            yield {}
            tg.cancel_scope.cancel()
        path.unlink(missing_ok=True)
        self.audit.close()


def _call_key(tool: str, arguments: dict) -> str:
    """One call's identity: its tool and arguments."""
    return tool + json.dumps(arguments, sort_keys=True)


def _needs_authorization(result: types.CallToolResult) -> bool:
    """Arcade answers a call the user hasn't authorized with a JSON body carrying `authorization_url`."""
    if not result.is_error or not result.content or result.content[0].type != "text":
        return False
    try:
        return "authorization_url" in json.loads(result.content[0].text)
    except ValueError:
        return False


def _error(text: str) -> types.CallToolResult:
    """A tool error the agent sees as the call's result."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


async def serve() -> None:
    """Run the proxy over stdio for the role in ATS_ROLE, logging to stderr and the role's proxy.log."""
    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(settings.data_dir / "proxy.log")],
    )
    proxy = Proxy(settings)
    server = Server(settings.role, lifespan=proxy.lifespan, on_list_tools=proxy.list_tools, on_call_tool=proxy.call_tool)
    options = server.create_initialization_options(notification_options=NotificationOptions(tools_changed=True))
    async with stdio_server() as (read, write):
        await server.run(read, write, options)


def main() -> None:
    """`ats-proxy` entry point."""
    anyio.run(serve)
