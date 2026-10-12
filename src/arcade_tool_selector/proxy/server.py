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
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server

from arcade_tool_selector.audit import Audit
from arcade_tool_selector.catalog import Catalog, CatalogStore, diff
from arcade_tool_selector.claude_code.runtime import socket_path
from arcade_tool_selector.proxy.upstream import Upstream
from arcade_tool_selector.settings import Settings, load_settings, parse_duration

logger = logging.getLogger("ats.proxy")

WATCH_INTERVAL = 5
JITTER = 0.1


class Proxy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = CatalogStore(settings.data_dir, settings.gateway_key)
        self.audit = Audit(settings.data_dir / "audit.jsonl")
        self.upstream = Upstream(settings.gateway_url, settings.api_key, settings.user_id, self._on_upstream_changed)
        self.refresh_interval = parse_duration(settings.config.catalog.refresh_interval)
        self.max_staleness = parse_duration(settings.config.catalog.max_staleness)
        self.catalog: Catalog | None = None
        self.sessions: set[ServerSession] = set()
        self._refresh_lock = anyio.Lock()
        self._seen_mtime: float | None = None
        self._tg: TaskGroup | None = None

    # MCP handlers

    async def list_tools(self, ctx, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        self.sessions.add(ctx.session)
        return types.ListToolsResult(tools=self.catalog.tools if self.catalog else [])

    async def call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        self.sessions.add(ctx.session)
        name = params.name
        if self.catalog is None or name not in self.catalog.names():
            await self._try_refresh("unknown_tool")
        if self.catalog is None or name not in self.catalog.names():
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

    async def start(self, tg: TaskGroup) -> None:
        self._tg = tg
        tg.start_soon(self.upstream.run)
        cached = self.store.load()
        if cached:
            self.catalog = cached
            self._seen_mtime = self.store.mtime()
            age = time.time() - cached.fetched_at
            logger.info("serving %d cached tools (%.0fs old)", len(cached.tools), age)
            if self.store.acquire_lease(self._lease_ttl()):
                tg.start_soon(self._try_refresh, "startup")
        else:
            await self._try_refresh("first_run")
        tg.start_soon(self._poll)
        tg.start_soon(self._watch)
        tg.start_soon(self._serve_hooks)

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
        for session in list(self.sessions):
            try:
                await session.send_tool_list_changed()
            except Exception:
                self.sessions.discard(session)

    async def _try_refresh(self, reason: str) -> None:
        try:
            await self.refresh(reason)
        except Exception:
            logger.exception("catalog refresh (%s) failed", reason)
            if self.catalog and time.time() - self.catalog.fetched_at > self.max_staleness:
                logger.warning("gateway unreachable and cache is older than %s", self.settings.config.catalog.max_staleness)

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
            if cached and (self.catalog is None or cached.digest != self.catalog.digest):
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
            request = json.loads(await stream.receive())
            self.audit.write("prompt_seen", session_id=request.get("session_id"), prompt_id=request.get("prompt_id"), chars=request.get("chars"))
            tools = len(self.catalog.tools) if self.catalog else 0
            await stream.send(json.dumps({"ok": True, "tools": tools}).encode())


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def build_server(proxy: Proxy) -> Server:
    @asynccontextmanager
    async def lifespan(_server: Server):
        async with anyio.create_task_group() as tg:
            await proxy.start(tg)
            yield {}
            tg.cancel_scope.cancel()

    return Server(
        proxy.settings.config.server_name,
        lifespan=lifespan,
        on_list_tools=proxy.list_tools,
        on_call_tool=proxy.call_tool,
    )


async def serve() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(settings.data_dir / "proxy.log")],
    )
    proxy = Proxy(settings)
    server = build_server(proxy)
    options = server.create_initialization_options(notification_options=NotificationOptions(tools_changed=True))
    async with stdio_server() as (read, write):
        await server.run(read, write, options)


def main() -> None:
    anyio.run(serve)
