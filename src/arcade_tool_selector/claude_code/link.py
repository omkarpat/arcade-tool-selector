"""The hooks' line to the proxy: one Unix socket per Claude Code process.

The proxy is a direct child of Claude Code (`exec` in .mcp.json), so its parent pid is the CLAUDE_PID that
Claude Code gives its hooks.
"""

import os
import socket
from pathlib import Path

from pydantic import BaseModel


class PromptSubmitted(BaseModel):
    """UserPromptSubmit → proxy: the user's own words, the evidence calls are judged against."""

    session_id: str
    prompt: str


class VetRequest(BaseModel):
    """PreToolUse → proxy: should this call run? Answered with a policy.Decision."""

    session_id: str
    tool: str  # gateway name, e.g. Gmail_SendEmail
    arguments: dict


def socket_path(claude_pid: int) -> Path:
    # Under a short /tmp dir: AF_UNIX paths are capped at ~104 bytes.
    run_dir = Path("/tmp") / f"ats-{os.getuid()}"
    run_dir.mkdir(mode=0o700, exist_ok=True)
    return run_dir / f"{claude_pid}.sock"


def ask_proxy(claude_pid: int, message: PromptSubmitted | VetRequest, timeout: float) -> str:
    """Send one message to the proxy and return its reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path(claude_pid)))
        sock.sendall(message.model_dump_json().encode())
        sock.shutdown(socket.SHUT_WR)
        return b"".join(iter(lambda: sock.recv(65536), b"")).decode()
