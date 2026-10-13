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
from arcade_tool_selector.executions import ExecutionKey, find_execution
from arcade_tool_selector.settings import REPO_ROOT, load_settings

pytestmark = [pytest.mark.skipif(not os.environ.get("ATS_LIVE"), reason="set ATS_LIVE=1"), pytest.mark.anyio]


@pytest.fixture
def anyio_backend():
    return "asyncio"


DEFAULT_ROLE = "workspace-search"  # its gateway's purpose covers reading the user's calendar


def role_env(data_dir, role: str) -> dict:
    # Everything comes from .env and the role's gateway; the data dir is isolated per test.
    return {**os.environ, "ATS_DATA_DIR": str(data_dir), "ATS_ROLE": role}


def proxy_params(data_dir, role: str = DEFAULT_ROLE) -> StdioServerParameters:
    return StdioServerParameters(command="uv", args=["run", "--project", str(REPO_ROOT), "ats-proxy"], env=role_env(data_dir, role))


def hook(name: str, data_dir, event: dict, role: str = DEFAULT_ROLE) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--project", str(REPO_ROOT), "ats-hook", name],
        input=json.dumps(event),
        env=role_env(data_dir, role),
        capture_output=True,
        text=True,
    )


async def list_and_call(data_dir):
    """The default gateway (Workspace Search: its purpose covers reading the user's calendar)."""
    async with stdio_client(proxy_params(data_dir)) as (read, write), ClientSession(read, write) as session:
        started = time.monotonic()
        await session.initialize()
        tools = (await session.list_tools()).tools
        ready = time.monotonic() - started
        # As in Claude Code, the user's prompt reaches the proxy before any call: it's the scope evidence.
        submitted = hook("user-prompt-submit", data_dir, {"session_id": "s1", "prompt_id": "p1", "prompt": "Which Google account is my calendar connected to?"})
        whoami = await session.call_tool("GoogleCalendar_WhoAmI", {})
        unknown = await session.call_tool("Nope_NotATool", {})
        return tools, ready, whoami, unknown, submitted


async def test_pass_through_cache_and_hook(tmp_path):
    tools, cold, whoami, unknown, submitted = await list_and_call(tmp_path)
    names = {t.name for t in tools}
    print(f"\ncold start: {len(tools)} tools in {cold:.1f}s")
    assert len(tools) > 105, "every tools/list page is served"
    assert {"Linear_ListIssues", "Slack_InviteUsersToChannel"} <= names
    assert not whoami.is_error, "GoogleCalendar is authorized for ARCADE_USER_ID"
    assert unknown.is_error

    assert submitted.returncode == 0 and submitted.stdout == "", "the prompt hook adds nothing to Claude Code"
    hook_log = (tmp_path / DEFAULT_ROLE / "hook.log").read_text().splitlines()[-1]
    print("hook:", hook_log)
    assert '\\"ok\\": true' in hook_log

    tools_warm, warm, *_ = await list_and_call(tmp_path)
    print(f"warm start: {len(tools_warm)} tools in {warm:.1f}s")
    assert {t.name for t in tools_warm} == names

    events = [json.loads(line)["event"] for line in (tmp_path / DEFAULT_ROLE / "audit.jsonl").read_text().splitlines()]
    print("audit events:", events)
    assert {"catalog_changed", "call", "vetted", "prompt_seen"} <= set(events)


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

        os.environ["ATS_ROLE"] = DEFAULT_ROLE
        store = CatalogStore(tmp_path / DEFAULT_ROLE, load_settings().gateway_key)
        store.save(Catalog.from_tools([t for t in before if t.name != "Linear_ArchiveIssue"]))

        with anyio.fail_after(15):
            await changed.wait()
        after = {t.name for t in (await session.list_tools()).tools}
        assert after == {t.name for t in before} - {"Linear_ArchiveIssue"}


async def test_call_resolves_to_arcade_execution(tmp_path):
    """A `call` audit event carries the keys to find Arcade's own record of the execution."""
    await list_and_call(tmp_path)
    os.environ.update({"ATS_ROLE": DEFAULT_ROLE, "ATS_DATA_DIR": str(tmp_path)})
    settings = load_settings()
    call = next(e for e in map(json.loads, (tmp_path / DEFAULT_ROLE / "audit.jsonl").read_text().splitlines()) if e["event"] == "call")
    key = ExecutionKey.model_validate(call)
    with anyio.fail_after(60):
        while (execution := find_execution(key, settings)) is None:  # Arcade's log lags the call by a few seconds
            await anyio.sleep(3)
    print("arcade execution:", execution.id, execution.execution_status)
    assert (execution.toolkit_name, execution.tool_name) == ("GoogleCalendar", "WhoAmI")
    assert execution.execution_status == "completed"


async def test_scope_enforced_at_hook_and_proxy(tmp_path):
    """The meeting-scheduler role, its purpose read from its own gateway: out-of-scope calls are stopped by the
    PreToolUse hook, and by the proxy itself when the hook is bypassed."""
    role = "meeting-scheduler"
    async with stdio_client(proxy_params(tmp_path, role)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.list_tools()
        hook("user-prompt-submit", tmp_path, {"session_id": "s1", "prompt_id": "p1", "prompt": "What's on my calendar tomorrow?"}, role)

        # Hook: an out-of-scope reply is denied before Claude Code runs it (never executed).
        denied = hook("pre-tool-use", tmp_path, {"session_id": "s1", "tool_name": f"mcp__{role}__Gmail_ReplyToEmail",
                                                 "tool_input": {"reply_to_message_id": "msg_invoice", "body": "We'll pay the invoice next week."}}, role)
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", decision

        # Hook: an in-scope read says nothing (Claude Code's own rules apply), then runs through the proxy.
        args = {"min_end_datetime": "2026-10-13T00:00:00", "max_start_datetime": "2026-10-14T00:00:00"}
        allowed = hook("pre-tool-use", tmp_path, {"session_id": "s1", "tool_name": f"mcp__{role}__GoogleCalendar_ListEvents", "tool_input": args}, role)
        assert allowed.returncode == 0 and allowed.stdout == ""
        assert not (await session.call_tool("GoogleCalendar_ListEvents", args)).is_error

        # Proxy: a call that skipped the hook is vetted anyway (a read outside the purpose, so safe if it ran).
        bypassed = await session.call_tool("Gmail_SearchThreads", {"body": ["invoice"]})
        assert bypassed.is_error and "Blocked by arcade-tool-selector" in bypassed.content[0].text

    log = tmp_path / role / "audit.jsonl"
    vetted = [json.loads(line) for line in log.read_text().splitlines() if '"vetted"' in line]
    print("\n".join(f"{v['via']:<5} {v['tool']:<26} {v['action']:<5} {v['check']:<9} {v['answers']}" for v in vetted))
    assert {(v["via"], v["tool"], v["action"]) for v in vetted} >= {
        ("hook", "Gmail_ReplyToEmail", "deny"),
        ("hook", "GoogleCalendar_ListEvents", "allow"),
        ("proxy", "Gmail_SearchThreads", "ask"),
    }
