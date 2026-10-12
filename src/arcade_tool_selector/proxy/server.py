"""Stdio MCP server that fronts an Arcade gateway for Claude Code.

Every gateway tool is listed; every call is vetted against the project's purpose before it is
forwarded (policy.py). The PreToolUse hook asks first over a local socket, so Claude Code can deny
or prompt; the proxy re-checks every call itself, so a call that skips the hook is still vetted.
The tool list comes from a shared on-disk cache that a background poll keeps fresh.
"""

import json
import logging
import random
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import anyio
from anyio.abc import SocketStream, TaskGroup
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server

from arcade_tool_selector.audit import Audit
from arcade_tool_selector.cards import build_cards
from arcade_tool_selector.catalog import Catalog, CatalogStore, diff
from arcade_tool_selector.claude_code.runtime import HookMessage, PromptSubmitted, VetRequest, socket_path
from arcade_tool_selector.executions import ExecutionKey
from arcade_tool_selector.judge import DecisionsJudge
from arcade_tool_selector.policy import Decision, Evidence, Vetter
from arcade_tool_selector.proxy.upstream import Upstream
from arcade_tool_selector.settings import Settings, load_settings

logger = logging.getLogger("ats.proxy")

WATCH_INTERVAL = 5
JITTER = 0.1
PROMPTS_KEPT = 20
# How long a hook decision covers the matching call reaching the proxy (an `ask` waits on the user).
DECISION_TTL = 600


class Proxy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = CatalogStore(settings.data_dir, settings.gateway_key)
        self.audit = Audit(settings.data_dir / "audit.jsonl", otel=settings.otel)
        self.upstream = Upstream(settings.gateway_url, settings.api_key, settings.user_id, self._on_upstream_changed)
        self.refresh_interval = settings.config.catalog.refresh_interval
        self.max_staleness = settings.config.catalog.max_staleness
        self.catalog = Catalog.from_tools([])
        # stdio serves one client; it is notified once it has listed tools.
        self.session: ServerSession | None = None
        self._refresh_lock = anyio.Lock()
        self._seen_mtime: float | None = None
        self._tg: TaskGroup | None = None
        self.judge = DecisionsJudge()
        self.vetter = self._vetter()
        # The user's own prompts per Claude Code session, and the session that spoke last.
        self.prompts: dict[str | None, list[str]] = {}
        self.last_session: str | None = None
        # Hook decisions awaiting their call: (tool, arguments) → (decision, time).
        self.decisions: dict[str, tuple[Decision, float]] = {}

    # MCP handlers

    async def list_tools(self, ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        self.session = ctx.session
        return types.ListToolsResult(tools=self.catalog.tools)

    async def call_tool(self, ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        if name not in self.catalog.names():
            await self._try_refresh("unknown_tool")
        arguments = params.arguments or {}
        decision = await self._recheck(name, arguments)
        if decision.action != "allow":
            return _error(f"Blocked by arcade-tool-selector: {decision.reason}")
        key = ExecutionKey.for_gateway_tool(name, self.settings.user_id, datetime.now(UTC))
        try:
            result = await self.upstream.call_tool(name, arguments)
        except Exception as e:
            logger.exception("call to %s failed", name)
            self.audit.write("call_failed", tool=name, error=type(e).__name__)
            return _error(f"Calling {name} through the gateway failed: {e}")
        if _needs_authorization(result):
            # Never executed, so Arcade has no record of it.
            self.audit.write("call_needs_authorization", tool=name, user_id=self.settings.user_id)
        else:
            # Details (status, timing, inputs/outputs) live in Arcade's execution log; this is the key to it.
            self.audit.write("call", **key.model_dump(mode="json"), is_error=bool(result.is_error))
        return result

    # Vetting

    def _vetter(self) -> Vetter:
        config = self.settings.config
        vetter = Vetter(config.scope, config.policy, build_cards(self.catalog, config.policy.tool_tiers), self.judge)
        for problem in vetter.config_problems():
            logger.warning("policy config: %s", problem)
        return vetter

    def _evidence(self, session_id: str | None) -> Evidence:
        return Evidence(project_prompt=self.settings.config.project_prompt, user_messages=self.prompts.get(session_id, []))

    async def vet(self, tool: str, arguments: dict, session_id: str | None, via: str) -> Decision:
        decision = await self.vetter.vet(tool, arguments, self._evidence(session_id))
        self.audit.write("vetted", via=via, tool=tool, arg_keys=sorted(arguments), **decision.model_dump())
        return decision

    async def _recheck(self, tool: str, arguments: dict) -> Decision:
        """The proxy's own check. A hook decision for this exact call stands: an `ask` reaching here means the
        user approved it. Without one the hook was bypassed, so vet now; nobody can be asked, so `ask` denies."""
        hooked = self.decisions.pop(_call_key(tool, arguments), None)
        if hooked and time.monotonic() - hooked[1] < DECISION_TTL:
            return hooked[0] if hooked[0].action == "deny" else hooked[0].model_copy(update={"action": "allow"})
        decision = await self.vet(tool, arguments, self.last_session, via="proxy")
        if decision.action == "ask":
            return decision.model_copy(update={"action": "deny", "reason": decision.reason + " Approval needs Claude Code's prompt."})
        return decision

    # Catalog lifecycle

    @asynccontextmanager
    async def lifespan(self, _server: Server):
        async with anyio.create_task_group() as tg:
            self._tg = tg
            tg.start_soon(self.upstream.run)
            cached = self.store.load()
            if cached:
                self.catalog = cached
                self.vetter = self._vetter()
                self._seen_mtime = self.store.mtime()
                logger.info("serving %d cached tools (%.0fs old)", len(cached.tools), time.time() - cached.fetched_at)
                if self.store.acquire_lease(self._lease_ttl()):
                    tg.start_soon(self._try_refresh, "startup")
            else:
                await self._try_refresh("first_run")
            tg.start_soon(self._poll)
            tg.start_soon(self._watch)
            tg.start_soon(self._serve_hooks)
            yield {}
            tg.cancel_scope.cancel()
        self.audit.close()

    async def refresh(self, reason: str) -> None:
        async with self._refresh_lock:
            fetched = Catalog.from_tools(await self.upstream.list_tools())
            self.store.save(fetched)
            self._seen_mtime = self.store.mtime()
            await self.apply(fetched, reason)

    async def apply(self, new: Catalog, reason: str) -> None:
        changes = diff(self.catalog, new)
        self.catalog = new
        self.vetter = self._vetter()
        if changes.empty:
            return
        logger.info("catalog changed (%s): +%d -%d ~%d", reason, len(changes.added), len(changes.removed), len(changes.changed))
        self.audit.write("catalog_changed", reason=reason, tools=len(new.tools), **changes.model_dump())
        if self.session:
            await self.session.send_tool_list_changed()

    async def _try_refresh(self, reason: str) -> None:
        try:
            await self.refresh(reason)
        except Exception:
            logger.exception("catalog refresh (%s) failed", reason)
            if time.time() - self.catalog.fetched_at > self.max_staleness:
                logger.warning("gateway unreachable and cache is older than %.0fs", self.max_staleness)

    async def _poll(self) -> None:
        if not self.refresh_interval:
            return
        while True:
            await anyio.sleep(self.refresh_interval * random.uniform(1 - JITTER, 1 + JITTER))
            if self.store.acquire_lease(self._lease_ttl()):
                await self._try_refresh("poll")

    async def _watch(self) -> None:
        """Pick up catalogs written by whichever proxy holds the poll lease."""
        while True:
            await anyio.sleep(WATCH_INTERVAL)
            mtime = self.store.mtime()
            if mtime is None or mtime == self._seen_mtime:
                continue
            self._seen_mtime = mtime
            cached = self.store.load()
            if cached and cached.digest != self.catalog.digest:
                await self.apply(cached, "shared_cache")

    async def _on_upstream_changed(self) -> None:
        self._tg.start_soon(self._try_refresh, "upstream_list_changed")

    def _lease_ttl(self) -> float:
        return 2 * (self.refresh_interval or 900)

    # Hook socket

    async def _serve_hooks(self) -> None:
        """Hooks degrade to no-ops without the socket; the proxy keeps serving tools."""
        path = socket_path(self.settings.data_dir)
        path.unlink(missing_ok=True)
        try:
            listener = await anyio.create_unix_listener(path)
        except OSError:
            logger.exception("hook socket unavailable at %s", path)
            return
        logger.info("hook socket at %s", path)
        try:
            await listener.serve(self._handle_hook)
        finally:
            path.unlink(missing_ok=True)

    async def _handle_hook(self, stream: SocketStream) -> None:
        async with stream:
            # A bad hook message must not take down the proxy (listener errors propagate to the lifespan).
            try:
                message = HookMessage.validate_json(b"".join([chunk async for chunk in stream]))
                if isinstance(message, PromptSubmitted):
                    await stream.send(self._prompt(message))
                else:
                    await stream.send(await self._vet_hook(message))
            except Exception:
                logger.exception("bad hook message")

    def _prompt(self, message: PromptSubmitted) -> bytes:
        prompts = self.prompts.setdefault(message.session_id, [])
        prompts.append(message.prompt)
        del prompts[:-PROMPTS_KEPT]
        self.last_session = message.session_id
        # The prompt text stays in memory; only its size is audited.
        self.audit.write("prompt_seen", session_id=message.session_id, prompt_id=message.prompt_id, chars=len(message.prompt))
        return json.dumps({"ok": True, "tools": len(self.catalog.tools)}).encode()

    async def _vet_hook(self, message: VetRequest) -> bytes:
        if message.tool not in self.catalog.names():
            await self._try_refresh("unknown_tool")
        decision = await self.vet(message.tool, message.arguments, message.session_id, via="hook")
        self.decisions[_call_key(message.tool, message.arguments)] = (decision, time.monotonic())
        return decision.model_dump_json().encode()


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
    server = Server(settings.config.server_name, lifespan=proxy.lifespan, on_list_tools=proxy.list_tools, on_call_tool=proxy.call_tool)
    options = server.create_initialization_options(notification_options=NotificationOptions(tools_changed=True))
    async with stdio_server() as (read, write):
        await server.run(read, write, options)


def main() -> None:
    anyio.run(serve)
