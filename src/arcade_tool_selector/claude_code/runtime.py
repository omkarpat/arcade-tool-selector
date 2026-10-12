import hashlib
import os
import subprocess
from pathlib import Path


def claude_pid(start: int | None = None) -> int | None:
    """Pid of the nearest ancestor process named `claude`."""
    pid = start or os.getpid()
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
