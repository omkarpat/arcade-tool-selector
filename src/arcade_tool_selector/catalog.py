import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from mcp import types
from pydantic import BaseModel


def tool_json(tool: types.Tool) -> dict:
    return tool.model_dump(mode="json", by_alias=True, exclude_none=True)


def digest(tools: list[types.Tool]) -> str:
    blob = json.dumps(sorted((tool_json(t) for t in tools), key=lambda t: t["name"]), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class Catalog(BaseModel):
    fetched_at: float
    digest: str
    tools: list[types.Tool]

    @classmethod
    def from_tools(cls, tools: list[types.Tool]) -> "Catalog":
        return cls(fetched_at=time.time(), digest=digest(tools), tools=tools)

    def names(self) -> set[str]:
        return {t.name for t in self.tools}


class CatalogDiff(BaseModel):
    added: list[str]
    removed: list[str]
    changed: list[str]

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff(old: Catalog, new: Catalog) -> CatalogDiff:
    before = {t.name: tool_json(t) for t in old.tools}
    after = {t.name: tool_json(t) for t in new.tools}
    return CatalogDiff(
        added=sorted(after.keys() - before.keys()),
        removed=sorted(before.keys() - after.keys()),
        changed=sorted(n for n in after.keys() & before.keys() if after[n] != before[n]),
    )


class CatalogStore:
    """Per-gateway catalog file shared by every proxy, plus a lease electing one poller."""

    def __init__(self, data_dir: Path, gateway_key: str):
        root = data_dir / "catalog"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{gateway_key}.json"
        self.lease_path = root / f"{gateway_key}.lock"

    def load(self) -> Catalog | None:
        if not self.path.exists():
            return None
        return Catalog.model_validate_json(self.path.read_text())

    def save(self, catalog: Catalog) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(catalog.model_dump_json(by_alias=True, exclude_none=True))
        os.replace(tmp, self.path)

    def mtime(self) -> float | None:
        return self.path.stat().st_mtime if self.path.exists() else None

    def acquire_lease(self, ttl: float, pid: int | None = None) -> bool:
        """Take or renew the poll lease. Free when unheld, expired, or its holder has exited."""
        pid = pid or os.getpid()
        now = time.time()
        with self.lease_path.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            raw = f.read()
            lease = json.loads(raw) if raw else {}
            holder = lease.get("pid")
            if holder not in (None, pid) and lease["expires_at"] > now and _alive(holder):
                return False
            f.seek(0)
            f.truncate()
            f.write(json.dumps({"pid": pid, "expires_at": now + ttl}))
            return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
