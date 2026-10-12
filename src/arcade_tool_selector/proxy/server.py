"""Stdio MCP server that fronts an Arcade gateway for Claude Code.

Phase 1 is a pass-through: every gateway tool is listed and every call is forwarded.
It serves the tool list from a shared on-disk cache that a background poll keeps fresh,
and listens on a local socket the Claude Code hooks connect to.
"""

import json
import logging
import random
import time
from contextlib import asynccontextmanager

import anyio
from anyio.abc import SocketStream, TaskGroup
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server

from arcade_tool_selector.audit import Audit
from arcade_tool_selector.catalog import Catalog, CatalogStore, diff
from arcade_tool_selector.claude_code.runtime import PromptSubmitted, socket_path
from arcade_tool_selector.proxy.upstream import Upstream
from arcade_tool_selector.settings import Settings, load_settings

logger = logging.getLogger("ats.proxy")

WATCH_INTERVAL = 5
JITTER = 0.1


class Proxy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = CatalogStore(settings.data_dir, settings.gateway_key)
        self.audit = Audit(settings.data_dir / "audit.jsonl")
        self.upstream = Upstream(settings.gateway_url, settings.api_key, settings.user_id, self._on_upstream_changed)
        self.refresh_interval = settings.config.catalog.refresh_interval
        self.max_staleness = settings.config.catalog.max_staleness
        self.catalog = Catalog.from_tools([])
        # stdio serves one client; it is notified once it has listed tools.
        self.session: ServerSession | None = None
        self._refresh_lock = anyio.Lock()
        self._seen_mtime: float | None = None
        self._tg: TaskGroup | None = None

    # MCP handlers

    async def list_tools(self, ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        self.session = ctx.session
        return types.ListToolsResult(tools=self.catalog.tools)

    async def call_tool(self, ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        if name not in self.catalog.names():
            await self._try_refresh("unknown_tool")
        if name not in self.catalog.names():
            self.audit.write("call_denied", tool=name, reason="unknown_tool")
            return _error(f"{name} is not a tool on this gateway.")
        started = time.monotonic()
        try:
            result = await self.upstream.call_tool(name, params.arguments)
        except Exception as e:
            logger.exception("call to %s failed", name)
            self.audit.write("call_failed", tool=name, error=type(e).__name__)
            return _error(f"Calling {name} through the gateway failed: {e}")
        self.audit.write(
            "call",
            tool=name,
            arg_keys=sorted(params.arguments or {}),
            is_error=bool(result.is_error),
            ms=round((time.monotonic() - started) * 1000),
        )
        return result

    # Catalog lifecycle

    @asynccontextmanager
    async def lifespan(self, _server: Server):
        async with anyio.create_task_group() as tg:
            self._tg = tg
            tg.start_soon(self.upstream.run)
            cached = self.store.load()
            if cached:
                self.catalog = cached
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

    async def refresh(self, reason: str) -> None:
        async with self._refresh_lock:
            fetched = Catalog.from_tools(await self.upstream.list_tools())
            self.store.save(fetched)
            self._seen_mtime = self.store.mtime()
            await self.apply(fetched, reason)

    async def apply(self, new: Catalog, reason: str) -> None:
        changes = diff(self.catalog, new)
        self.catalog = new
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
                request = PromptSubmitted.model_validate_json(await stream.receive())
                self.audit.write("prompt_seen", **request.model_dump())
                await stream.send(json.dumps({"ok": True, "tools": len(self.catalog.tools)}).encode())
            except Exception:
                logger.exception("bad hook message")


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
