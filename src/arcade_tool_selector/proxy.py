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
    def __init__(self, settings: Settings):
        self.settings = settings
        self.audit = Audit(settings.data_dir / "audit.jsonl", otel=settings.otel)
        self.judge = DecisionsJudge(settings.openai_api_key)
        self.catalog = Catalog(tools=[])
        self.purpose = ""  # the gateway's instructions
        self.vetter = self._vetter()
        self.prompts: dict[str, list[str]] = {}  # the user's own prompts, per Claude Code session
        self.vetted: Counter[str] = Counter()  # calls the hook vetted and didn't deny, awaiting execution
        self.session: ServerSession | None = None  # the client, once it has listed tools

    # MCP handlers

    async def list_tools(self, ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        self.session = ctx.session
        return types.ListToolsResult(tools=self.catalog.tools)

    async def call_tool(self, ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        name, arguments = params.name, params.arguments or {}
        key = _call_key(name, arguments)
        if not self.vetted[key]:
            return _error(f"Blocked by arcade-tool-selector: {name} was not vetted by its PreToolUse hook (see .claude/settings.json).")
        self.vetted[key] -= 1
        started = ExecutionKey.for_gateway_tool(name, self.settings.user_id, datetime.now(UTC))
        result = await gateway.call_tool(self.settings, name, arguments)
        if _needs_authorization(result):
            # Never executed, so Arcade has no record of it.
            self.audit.write("call_needs_authorization", tool=name, user_id=self.settings.user_id)
        else:
            # Status, timing, inputs and outputs live in Arcade's execution log; this is the key to it.
            self.audit.write("call", **started.model_dump(mode="json"), is_error=bool(result.is_error))
        return result

    # Hook socket

    async def handle_hook(self, stream: SocketStream) -> None:
        # A failed exchange blocks that hook's prompt or call; it must not take down the proxy.
        async with stream:
            try:
                message = HookMessage.validate_json(b"".join([chunk async for chunk in stream]))
                reply = self.prompt(message) if isinstance(message, PromptSubmitted) else await self.vet(message)
                await stream.send(reply)
            except Exception:
                logger.exception("hook exchange failed")

    def prompt(self, message: PromptSubmitted) -> bytes:
        prompts = self.prompts.setdefault(message.session_id, [])
        prompts.append(message.prompt)
        del prompts[:-PROMPTS_KEPT]
        # The prompt stays in memory; only its size is audited.
        self.audit.write("prompt_seen", session_id=message.session_id, chars=len(message.prompt))
        return b"{}"

    async def vet(self, message: VetRequest) -> bytes:
        evidence = Evidence(purpose=self.purpose, user_messages=self.prompts.get(message.session_id, []))
        decision = await self.vetter.vet(message.tool, message.arguments, evidence)
        self.audit.write("vetted", tool=message.tool, arg_keys=sorted(message.arguments), **decision.model_dump())
        if decision.action != "deny":
            self.vetted[_call_key(message.tool, message.arguments)] += 1
        return decision.model_dump_json().encode()

    # Tools and purpose

    def _vetter(self) -> Vetter:
        return Vetter(ScopeConfig(org_domains=[self.settings.org_domain]), PolicyConfig(), build_cards(self.catalog), self.judge)

    async def refresh(self) -> None:
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
        while True:
            await anyio.sleep(REFRESH_INTERVAL)
            try:
                await self.refresh()
            except Exception:
                logger.exception("refresh failed; every call is denied until the purpose can be read again")
                self.purpose = ""

    @asynccontextmanager
    async def lifespan(self, _server: Server):
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
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


async def serve() -> None:
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
    anyio.run(serve)
