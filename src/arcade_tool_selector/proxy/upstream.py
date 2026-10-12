import logging
from collections.abc import Awaitable, Callable
from importlib.metadata import version

import anyio
from mcp import types
from mcp.client.session import ClientSession, IncomingMessage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30


class Upstream:
    """Long-lived MCP client session to the Arcade gateway, reconnecting on failure."""

    def __init__(self, url: str, api_key: str, user_id: str, on_tools_changed: Callable[[], Awaitable[None]]):
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Arcade-User-ID": user_id,
            # Arcade's edge rejects default HTTP-library user agents.
            "User-Agent": f"arcade-tool-selector/{version('arcade-tool-selector')}",
        }
        self.on_tools_changed = on_tools_changed
        self._session: ClientSession | None = None
        self._connected = anyio.Event()
        self._dropped = anyio.Event()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with (
                    create_mcp_http_client(headers=self.headers) as http,
                    streamable_http_client(self.url, http_client=http) as (read, write),
                    ClientSession(read, write, message_handler=self._on_message) as session,
                ):
                    await session.initialize()
                    logger.info("connected to gateway")
                    backoff = 1.0
                    self._dropped = anyio.Event()
                    self._session = session
                    self._connected.set()
                    await self._dropped.wait()
            except Exception:
                logger.exception("gateway connection failed; retrying in %.0fs", backoff)
            self._drop()
            await anyio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def list_tools(self) -> list[types.Tool]:
        """Every page of the gateway's tools/list. Retried once on a dropped connection."""
        try:
            return await self._list_all(await self._get_session())
        except Exception:
            self._drop()
            return await self._list_all(await self._get_session())

    async def call_tool(self, name: str, arguments: dict | None) -> types.CallToolResult:
        """Never retried: a tool call may not be idempotent."""
        session = await self._get_session()
        try:
            return await session.call_tool(name, arguments)
        except Exception:
            self._drop()
            raise

    def _drop(self) -> None:
        """Tear down the session; callers wait for run() to reconnect."""
        # Replace only a set event: waiters on an unset one must see the next connect.
        if self._connected.is_set():
            self._connected = anyio.Event()
        self._session = None
        self._dropped.set()

    async def _get_session(self) -> ClientSession:
        with anyio.fail_after(CONNECT_TIMEOUT):
            while self._session is None:
                await self._connected.wait()
        return self._session

    @staticmethod
    async def _list_all(session: ClientSession) -> list[types.Tool]:
        tools: list[types.Tool] = []
        cursor = None
        while True:
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            tools += page.tools
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def _on_message(self, message: IncomingMessage) -> None:
        # Runs inline in the session's receive loop: the callback must not wait on this session.
        if isinstance(message, types.ToolListChangedNotification):
            await self.on_tools_changed()
