"""`ats-doctor`: check a setup end to end and say what to fix."""

import os
import re
import shutil
import subprocess
import sys

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from arcade_tool_selector.judge import DecisionsJudge
from arcade_tool_selector.proxy.upstream import Upstream
from arcade_tool_selector.settings import REPO_ROOT, load_settings

MIN_CLAUDE_CODE = (2, 1, 295)  # hooks' onFailure
problems = 0


def report(ok: bool, what: str, fix: str = "") -> None:
    global problems
    problems += not ok
    print(f"{'✓' if ok else '✗'} {what}" + (f"\n    → {fix}" if fix and not ok else ""))


async def list_gateway(url: str, headers: dict) -> int:
    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return len(await Upstream._list_all(session))


async def judge_error() -> str | None:
    try:
        await DecisionsJudge().predicates("The sky is blue.", {"blue": "Does the input say the sky is blue?"})
    except Exception as e:
        return str(e)[:200]
    return None


async def check() -> None:
    env_file = os.environ.get("ATS_ENV_FILE", str(REPO_ROOT / ".env"))
    try:
        settings = load_settings()
    except KeyError as e:
        report(False, f"environment: {e.args[0]} is not set", f"copy .env.example to .env and fill it in ({env_file})")
        return
    except FileNotFoundError as e:
        report(False, f"project config not found: {e.filename}", "cp examples/claude-code/ats.yaml ats.yaml")
        return
    except Exception as e:
        report(False, f"project config invalid: {e}", "compare your ats.yaml with examples/claude-code/ats.yaml")
        return
    report(True, f"environment and project config load (project {settings.config.project!r})")
    report(bool(os.environ.get("OPENAI_API_KEY")), "OPENAI_API_KEY is set", "the scope judge needs it; add it to .env")

    upstream = Upstream(settings.gateway_url, settings.api_key, settings.user_id, None)
    try:
        tools = await list_gateway(settings.gateway_url, upstream.headers)
        report(tools > 0, f"gateway answers with {tools} tools", "add tools to the gateway in the Arcade dashboard")
    except Exception as e:
        hint = "set the gateway's authentication to Arcade Headers" if "401" in str(e) or "auth" in str(e).lower() else str(e)[:200]
        report(False, "gateway rejected the connection", hint)

    error = await judge_error()
    report(error is None, "scope judge answers (OpenAI Decisions API, gpt-6-luna)", f"the key needs Decisions API access: {error}")

    claude = shutil.which("claude")
    if not claude:
        print("· Claude Code not found: `uv run ats-demo` runs the same vetter without it")
        return
    version = re.search(r"(\d+)\.(\d+)\.(\d+)", subprocess.run([claude, "--version"], capture_output=True, text=True).stdout)
    found = tuple(map(int, version.groups())) if version else (0, 0, 0)
    report(found >= MIN_CLAUDE_CODE, f"Claude Code {'.'.join(map(str, found))}", f"update to {'.'.join(map(str, MIN_CLAUDE_CODE))} or later (hooks' onFailure)")


def main() -> None:
    anyio.run(check)
    print("\nAll good." if not problems else f"\n{problems} problem(s).")
    sys.exit(1 if problems else 0)
