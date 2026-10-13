"""Claude Code hook entrypoint: `ats-hook <user-prompt-submit|pre-tool-use|post-tool-use>`.

Hooks stay thin and hold no secrets; the proxy does the work.
- UserPromptSubmit hands the user's prompt to the proxy: the evidence scope is judged against. If the proxy
  can't be reached the prompt is blocked.
- PreToolUse asks the proxy to vet the call. Deny and ask map to Claude Code's own decisions; allow
  says nothing, so Claude Code's normal permission rules still apply (the vetter only tightens).
  If the proxy can't be reached the call is blocked.
"""

import json
import os
import sys

from arcade_tool_selector.audit import Audit
from arcade_tool_selector.claude_code.runtime import PromptSubmitted, VetRequest, ask_proxy
from arcade_tool_selector.policy import Decision
from arcade_tool_selector.settings import data_dir, load_env

PROMPT_TIMEOUT = 5
VET_TIMEOUT = 25  # under the hook's 30s timeout


def user_prompt_submit(event: dict) -> None:
    root = data_dir()
    message = PromptSubmitted(session_id=event.get("session_id"), prompt_id=event.get("prompt_id"), prompt=event.get("prompt", ""))
    try:
        reply = ask_proxy(root, message, PROMPT_TIMEOUT)
    except OSError as e:
        # Without the prompt, every call would be judged with no evidence: stop here, loudly.
        print(f"arcade-tool-selector proxy unreachable ({e}); restart Claude Code or check /mcp. Prompt not sent.", file=sys.stderr)
        sys.exit(2)
    Audit(root / "hook.log").write("prompt_submitted", reply=reply)


def pre_tool_use(event: dict) -> None:
    # MCP tools reach hooks as mcp__<server>__<tool>.
    tool = event["tool_name"].split("__", 2)[2]
    message = VetRequest(session_id=event.get("session_id"), tool=tool, arguments=event.get("tool_input") or {})
    try:
        decision = Decision.model_validate_json(ask_proxy(data_dir(), message, VET_TIMEOUT))
    except Exception as e:
        print(f"arcade-tool-selector could not vet {tool}: {e}. Blocked.", file=sys.stderr)
        sys.exit(2)
    if decision.action != "allow":
        output = {"permissionDecision": decision.action, "permissionDecisionReason": f"arcade-tool-selector: {decision.reason}"}
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **output}}))


def main() -> None:
    load_env()
    if not os.environ.get("ATS_ROLE"):
        print("ATS_ROLE is not set: start Claude Code as `ATS_ROLE=<role> claude` (roles: ATS_ROLES in .env). Blocked.", file=sys.stderr)
        sys.exit(2)
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    event = json.load(sys.stdin)
    if command == "user-prompt-submit":
        user_prompt_submit(event)
    elif command == "pre-tool-use":
        pre_tool_use(event)
    elif command != "post-tool-use":
        sys.exit(f"unknown hook {command!r}")
