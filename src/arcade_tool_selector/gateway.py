"""The role's Arcade gateway, as an MCP client: one short session per request, Arcade Headers auth."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from arcade_tool_selector.settings import Settings

# Arcade answers the session DELETE with 202 Accepted, which the SDK logs as a failure.
logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)


@asynccontextmanager
async def connect(settings: Settings) -> AsyncIterator[ClientSession]:
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Arcade-User-ID": settings.user_id,
        # Arcade's edge rejects default HTTP-library user agents.
        "User-Agent": f"arcade-tool-selector/{version('arcade-tool-selector')}",
    }
    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(settings.gateway_url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def list_tools(settings: Settings) -> list[types.Tool]:
    """Every page of tools/list: the gateway pages at ~105 tools."""
    tools: list[types.Tool] = []
    cursor = None
    async with connect(settings) as session:
        while True:
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            tools += page.tools
            if not (cursor := page.next_cursor):
                return tools


async def call_tool(settings: Settings, name: str, arguments: dict) -> types.CallToolResult:
    async with connect(settings) as session:
        return await session.call_tool(name, arguments)
