"""End-to-end against the real gateways in .env. Run with: ATS_LIVE=1 uv run pytest tests/test_proxy_live.py -s

The test process plays Claude Code: it starts the proxy as a direct child and runs the hooks with CLAUDE_PID set
to its own pid, exactly as Claude Code does.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from arcade_tool_selector.executions import ExecutionKey, find_execution
from arcade_tool_selector.settings import load_settings

pytestmark = [pytest.mark.skipif(not os.environ.get("ATS_LIVE"), reason="set ATS_LIVE=1"), pytest.mark.anyio]

BIN = Path(sys.executable).parent
DEFAULT_ROLE = "workspace-search"  # its gateway's purpose covers reading the user's calendar


@pytest.fixture
def anyio_backend():
    return "asyncio"


def role_env(data_dir, role: str) -> dict:
    # Everything comes from .env and the role's gateway; the data dir is isolated per test.
    return {**os.environ, "ATS_DATA_DIR": str(data_dir), "ATS_ROLE": role, "CLAUDE_PID": str(os.getpid())}


def proxy(data_dir, role: str = DEFAULT_ROLE):
    return stdio_client(StdioServerParameters(command=str(BIN / "ats-proxy"), env=role_env(data_dir, role)))


def hook(name: str, data_dir, event: dict, role: str = DEFAULT_ROLE) -> subprocess.CompletedProcess:
    return subprocess.run([BIN / "ats-hook", name], input=json.dumps(event), env=role_env(data_dir, role), capture_output=True, text=True)


def pre_tool_use(data_dir, role: str, tool: str, arguments: dict) -> subprocess.CompletedProcess:
    return hook("pre-tool-use", data_dir, {"session_id": "s1", "tool_name": f"mcp__arcade__{tool}", "tool_input": arguments}, role)


def audit(data_dir, role: str = DEFAULT_ROLE) -> list[dict]:
    return [json.loads(line) for line in (data_dir / role / "audit.jsonl").read_text().splitlines()]


async def test_vetted_call_runs_through_the_gateway(tmp_path):
    async with proxy(tmp_path) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        prompt = hook("user-prompt-submit", tmp_path, {"session_id": "s1", "prompt": "Which Google account is my calendar connected to?"})
        allowed = pre_tool_use(tmp_path, DEFAULT_ROLE, "GoogleCalendar_WhoAmI", {})
        whoami = await session.call_tool("GoogleCalendar_WhoAmI", {})

    assert len(names) > 105, "every tools/list page is served"
    assert prompt.returncode == 0 and prompt.stdout == "", "the prompt hook adds nothing to Claude Code"
    assert allowed.returncode == 0 and allowed.stdout == "", "allow says nothing: Claude Code's own rules apply"
    assert not whoami.is_error, "GoogleCalendar is authorized for ARCADE_USER_ID"
    events = [e["event"] for e in audit(tmp_path)]
    print("audit events:", events)
    assert events == ["catalog_changed", "prompt_seen", "vetted", "call"]


async def test_call_resolves_to_arcade_execution(tmp_path):
    """A `call` audit event carries the keys to find Arcade's own record of the execution."""
    await test_vetted_call_runs_through_the_gateway(tmp_path)
    os.environ.update({"ATS_ROLE": DEFAULT_ROLE, "ATS_DATA_DIR": str(tmp_path)})
    key = ExecutionKey.model_validate(next(e for e in audit(tmp_path) if e["event"] == "call"))
    with anyio.fail_after(60):
        while (execution := find_execution(key, load_settings())) is None:  # Arcade's log lags the call by a few seconds
            await anyio.sleep(3)
    print("arcade execution:", execution.id, execution.execution_status)
    assert (execution.toolkit_name, execution.tool_name) == ("GoogleCalendar", "WhoAmI")
    assert execution.execution_status == "completed"


async def test_scope_enforced_by_hook_and_proxy(tmp_path):
    """The meeting-scheduler role, its purpose read from its own gateway: the hook denies out-of-scope calls, and
    the proxy runs nothing the hook didn't vet."""
    role = "meeting-scheduler"
    async with proxy(tmp_path, role) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.list_tools()
        hook("user-prompt-submit", tmp_path, {"session_id": "s1", "prompt": "What's on my calendar tomorrow?"}, role)

        # An out-of-scope reply is denied before Claude Code runs it, and the proxy won't run it either.
        reply = {"reply_to_message_id": "msg_invoice", "body": "We'll pay the invoice next week."}
        denied = pre_tool_use(tmp_path, role, "Gmail_ReplyToEmail", reply)
        assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        refused = await session.call_tool("Gmail_ReplyToEmail", reply)
        assert refused.is_error and "not vetted" in refused.content[0].text

        # An in-scope read says nothing, then runs through the proxy.
        args = {"min_end_datetime": "2026-10-13T00:00:00", "max_start_datetime": "2026-10-14T00:00:00"}
        assert pre_tool_use(tmp_path, role, "GoogleCalendar_ListEvents", args).stdout == ""
        assert not (await session.call_tool("GoogleCalendar_ListEvents", args)).is_error

        # A call that skipped the hook never reaches Arcade.
        bypassed = await session.call_tool("GoogleCalendar_ListCalendars", {})
        assert bypassed.is_error and "not vetted" in bypassed.content[0].text

    vetted = [(e["tool"], e["action"]) for e in audit(tmp_path, role) if e["event"] == "vetted"]
    print(vetted)
    assert vetted == [("Gmail_ReplyToEmail", "deny"), ("GoogleCalendar_ListEvents", "allow")]


def test_hook_blocks_without_a_role(tmp_path):
    env = {k: v for k, v in role_env(tmp_path, DEFAULT_ROLE).items() if k != "ATS_ROLE"}
    result = subprocess.run([BIN / "ats-hook", "user-prompt-submit"], input="{}", env=env, capture_output=True, text=True)
    assert result.returncode == 2 and "ATS_ROLE is not set" in result.stderr
