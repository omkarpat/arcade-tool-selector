import hashlib
import os
import socket
import subprocess
from pathlib import Path

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class PromptSubmitted(BaseModel):
    """UserPromptSubmit → proxy: the user's own words, the evidence scope is judged against."""

    kind: Literal["prompt"] = "prompt"
    session_id: str | None
    prompt_id: str | None
    prompt: str


class VetRequest(BaseModel):
    """PreToolUse → proxy: should this call run? Answered with a policy.Decision."""

    kind: Literal["vet"] = "vet"
    session_id: str | None
    tool: str  # gateway name, e.g. Gmail_SendEmail
    arguments: dict


HookMessage = TypeAdapter(Annotated[PromptSubmitted | VetRequest, Field(discriminator="kind")])


def ask_proxy(data_dir: Path, message: PromptSubmitted | VetRequest, timeout: float) -> str:
    """Send one message to this session's proxy and return its reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path(data_dir)))
        sock.sendall(message.model_dump_json().encode())
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode()


def claude_pid() -> int | None:
    """Pid of the nearest ancestor process named `claude`."""
    pid = os.getpid()
    while pid > 1:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.split(None, 1)
        if len(out) < 2:
            return None
        ppid, comm = int(out[0]), out[1].strip()
        if Path(comm).name == "claude":
            return pid
        pid = ppid
    return None


def socket_path(data_dir: Path) -> Path:
    """Socket shared by a Claude Code process's proxy and hooks.

    Both are descendants of the same `claude` process; without one, fall back to the cwd.
    Lives under a short /tmp dir because AF_UNIX paths are capped at ~104 bytes.
    """
    pid = claude_pid()
    owner = str(pid) if pid else "cwd-" + hashlib.sha256(os.getcwd().encode()).hexdigest()[:12]
    run_dir = Path("/tmp") / f"ats-{os.getuid()}"
    run_dir.mkdir(mode=0o700, exist_ok=True)
    return run_dir / f"{owner}-{hashlib.sha256(str(data_dir).encode()).hexdigest()[:8]}.sock"
