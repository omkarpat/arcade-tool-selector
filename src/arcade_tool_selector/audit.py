import json
import time
from pathlib import Path
from typing import Any


class Audit:
    """Append-only JSONL event log."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, event: str, **fields: Any) -> None:
        line = json.dumps({"ts": round(time.time(), 3), "event": event, **fields}, default=str)
        with self.path.open("a") as f:
            f.write(line + "\n")
