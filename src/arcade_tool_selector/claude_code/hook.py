"""Claude Code hooks: `ats-hook <user-prompt-submit|pre-tool-use>`. Thin, no secrets; the proxy does the work.

- UserPromptSubmit hands the user's prompt to the proxy: the evidence calls are judged against.
- PreToolUse asks the proxy to vet the call. Deny and ask map to Claude Code's own decisions; allow says nothing,
  so Claude Code's permission rules still apply (the vetter only tightens).
If the proxy can't be reached, the prompt or call is blocked.
"""

import json
import os
import sys

from arcade_tool_selector.claude_code.link import PromptSubmitted, VetRequest, ask_proxy
from arcade_tool_selector.policy import Decision

PROMPT_TIMEOUT = 5
VET_TIMEOUT = 25  # under the hook's 30s timeout


def block(message: str) -> None:
    print(f"arcade-tool-selector: {message}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    if not os.environ.get("ATS_ROLE"):
        block("ATS_ROLE is not set: start Claude Code as `ATS_ROLE=<role> claude` (roles: ATS_ROLES in .env). Blocked.")
    claude_pid = int(os.environ["CLAUDE_PID"])
    command = sys.argv[1]
    event = json.load(sys.stdin)
    if command == "user-prompt-submit":
        try:
            ask_proxy(claude_pid, PromptSubmitted(session_id=event["session_id"], prompt=event["prompt"]), PROMPT_TIMEOUT)
        except OSError as e:
            block(f"proxy unreachable ({e}); check /mcp. Prompt not sent.")
    elif command == "pre-tool-use":
        # MCP tools reach hooks as mcp__<server>__<tool>.
        tool = event["tool_name"].split("__", 2)[2]
        request = VetRequest(session_id=event["session_id"], tool=tool, arguments=event["tool_input"])
        try:
            decision = Decision.model_validate_json(ask_proxy(claude_pid, request, VET_TIMEOUT))
        except Exception as e:
            block(f"could not vet {tool}: {e}. Blocked.")
        if decision.action != "allow":
            output = {"hookEventName": "PreToolUse", "permissionDecision": decision.action, "permissionDecisionReason": f"arcade-tool-selector: {decision.reason}"}
            print(json.dumps({"hookSpecificOutput": output}))
    else:
        sys.exit(f"unknown hook {command!r}")
