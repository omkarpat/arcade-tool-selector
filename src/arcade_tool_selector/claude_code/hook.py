"""Claude Code hook entrypoint: `ats-hook <user-prompt-submit|pre-tool-use|post-tool-use>`.

Hooks stay thin and hold no secrets; the proxy does the work. In phase 1 the
UserPromptSubmit hook only proves it reaches this session's proxy, and the tool
hooks are no-ops, so Claude Code behaves exactly as without them.
"""

import json
import socket
import sys

from arcade_tool_selector.audit import Audit
from arcade_tool_selector.claude_code.runtime import PromptSubmitted, socket_path
from arcade_tool_selector.settings import data_dir, load_env

SOCKET_TIMEOUT = 5


def user_prompt_submit(event: dict) -> None:
    root = data_dir()
    path = socket_path(root)
    request = PromptSubmitted(session_id=event.get("session_id"), prompt_id=event.get("prompt_id"), chars=len(event.get("prompt", "")))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect(str(path))
            sock.sendall(request.model_dump_json().encode())
            sock.shutdown(socket.SHUT_WR)
            reply = sock.recv(65536).decode()
    except OSError as e:
        reply = f"error: {e}"
    Audit(root / "hook.log").write("prompt_submitted", socket=str(path), reply=reply)


def main() -> None:
    load_env()
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    event = json.load(sys.stdin)
    if command == "user-prompt-submit":
        user_prompt_submit(event)
    elif command not in ("pre-tool-use", "post-tool-use"):
        sys.exit(f"unknown hook {command!r}")
