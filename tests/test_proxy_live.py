"""End-to-end against the real gateway in .env. Run with: ATS_LIVE=1 uv run pytest tests/test_proxy_live.py -s"""

import json
import os
import subprocess
import time

import anyio
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from arcade_tool_selector.catalog import Catalog, CatalogStore
from arcade_tool_selector.settings import REPO_ROOT, load_settings

pytestmark = [pytest.mark.skipif(not os.environ.get("ATS_LIVE"), reason="set ATS_LIVE=1"), pytest.mark.anyio]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def proxy_params(data_dir) -> StdioServerParameters:
    return StdioServerParameters(
        command="uv", args=["run", "--project", str(REPO_ROOT), "ats-proxy"], env={**os.environ, "ATS_DATA_DIR": str(data_dir)}
    )


async def list_and_call(data_dir):
    async with stdio_client(proxy_params(data_dir)) as (read, write), ClientSession(read, write) as session:
        started = time.monotonic()
        await session.initialize()
        tools = (await session.list_tools()).tools
        ready = time.monotonic() - started
        whoami = await session.call_tool("Linear_WhoAmI", {})
        unknown = await session.call_tool("Nope_NotATool", {})
        hook = subprocess.run(
            ["uv", "run", "--project", str(REPO_ROOT), "ats-hook", "user-prompt-submit"],
            input=json.dumps({"session_id": "s1", "prompt_id": "p1", "prompt": "hello"}),
            env={**os.environ, "ATS_DATA_DIR": str(data_dir)},
            capture_output=True,
            text=True,
        )
        return tools, ready, whoami, unknown, hook


async def test_pass_through_cache_and_hook(tmp_path):
    tools, cold, whoami, unknown, hook = await list_and_call(tmp_path)
    names = {t.name for t in tools}
    print(f"\ncold start: {len(tools)} tools in {cold:.1f}s")
    assert len(tools) > 105, "every tools/list page is served"
    assert {"Linear_ListIssues", "Slack_InviteUsersToChannel"} <= names
    print("Linear_WhoAmI:", whoami.content[0].text[:200])
    assert unknown.is_error

    assert hook.returncode == 0 and hook.stdout == "", "phase-1 hooks change nothing in Claude Code"
    hook_log = (tmp_path / "hook.log").read_text().splitlines()[-1]
    print("hook:", hook_log)
    assert '\\"ok\\": true' in hook_log

    tools_warm, warm, *_ = await list_and_call(tmp_path)
    print(f"warm start: {len(tools_warm)} tools in {warm:.1f}s")
    assert {t.name for t in tools_warm} == names

    events = [json.loads(line)["event"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    print("audit events:", events)
    assert {"catalog_changed", "call", "call_denied", "prompt_seen"} <= set(events)


async def test_shared_cache_change_reaches_client(tmp_path):
    """A catalog written by the lease holder is picked up and announced via list_changed."""
    changed = anyio.Event()

    async def on_message(message):
        if isinstance(message, types.ToolListChangedNotification):
            changed.set()

    async with (
        stdio_client(proxy_params(tmp_path)) as (read, write),
        ClientSession(read, write, message_handler=on_message) as session,
    ):
        await session.initialize()
        before = (await session.list_tools()).tools

        store = CatalogStore(tmp_path, load_settings().gateway_key)
        store.save(Catalog.from_tools([t for t in before if t.name != "Linear_ArchiveIssue"]))

        with anyio.fail_after(15):
            await changed.wait()
        after = {t.name for t in (await session.list_tools()).tools}
        assert after == {t.name for t in before} - {"Linear_ArchiveIssue"}
