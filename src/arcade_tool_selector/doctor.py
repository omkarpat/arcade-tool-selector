"""`ats-doctor`: check a setup end to end and say what to fix."""

import re
import shutil
import subprocess
import sys

import anyio

from arcade_tool_selector import gateway
from arcade_tool_selector.judge import DecisionsJudge
from arcade_tool_selector.platform import read_purpose
from arcade_tool_selector.settings import load_settings

MIN_CLAUDE_CODE = (2, 1, 295)  # hooks' onFailure
problems = 0


def report(ok: bool, what: str, fix: str = "") -> None:
    global problems
    problems += not ok
    print(f"{'✓' if ok else '✗'} {what}" + (f"\n    → {fix}" if fix and not ok else ""))


async def judge_error(api_key: str) -> str | None:
    try:
        await DecisionsJudge(api_key).predicates("The sky is blue.", {"blue": "Does the input say the sky is blue?"})
    except Exception as e:
        return str(e)[:200]
    return None


async def check() -> None:
    try:
        settings = load_settings()
    except (KeyError, SystemExit, ValueError) as e:
        report(False, f".env: {e}", "copy .env.example to .env and fill it in; run with ATS_ROLE=<one of ATS_ROLES>")
        return
    report(True, f"role {settings.role!r} loads from .env (project {settings.project_id[:8]}…)")

    try:
        tools = len(await gateway.list_tools(settings))
        report(tools > 0, f"gateway answers with {tools} tools", "add tools to the gateway in the Arcade dashboard")
    except Exception as e:
        hint = "set the gateway's authentication to Arcade Headers, and use this project's own API key" if "40" in str(e) or "auth" in str(e).lower() else str(e)[:200]
        report(False, "gateway rejected the connection", hint)

    try:
        purpose = read_purpose(settings)
        report(bool(purpose), f"purpose (gateway instructions): {purpose[:60]}…" if purpose else "the gateway has no instructions", "write the project's purpose into the gateway's \"Guide your agent\" instructions in the Arcade dashboard; until then every call is denied")
    except Exception as e:
        report(False, "could not read the gateway's instructions", str(e)[:200])

    error = await judge_error(settings.openai_api_key)
    report(error is None, "scope judge answers (OpenAI Decisions API, gpt-6-luna)", f"the key needs Decisions API access: {error}")

    claude = shutil.which("claude")
    if not claude:
        print("· Claude Code not found: `ATS_ROLE=meeting-scheduler uv run ats-demo` runs the same vetter without it")
        return
    version = re.search(r"(\d+)\.(\d+)\.(\d+)", subprocess.run([claude, "--version"], capture_output=True, text=True).stdout)
    found = tuple(map(int, version.groups())) if version else (0, 0, 0)
    report(found >= MIN_CLAUDE_CODE, f"Claude Code {'.'.join(map(str, found))}", f"update to {'.'.join(map(str, MIN_CLAUDE_CODE))} or later (hooks' onFailure)")


def main() -> None:
    anyio.run(check)
    print("\nAll good." if not problems else f"\n{problems} problem(s).")
    sys.exit(1 if problems else 0)
