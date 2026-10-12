"""Snapshot your gateway's tool catalog for the evals: `uv run python evals/snapshot.py [name]` (default: workspace).

Reads every tools/list page straight from the gateway in .env; no proxy needed. Snapshots are gitignored.
"""

import sys
from pathlib import Path

import anyio

from arcade_tool_selector.catalog import Catalog
from arcade_tool_selector.proxy.upstream import Upstream
from arcade_tool_selector.settings import load_settings


async def fetch() -> Catalog:
    settings = load_settings()

    async def ignore() -> None:
        pass

    upstream = Upstream(settings.gateway_url, settings.api_key, settings.user_id, ignore)
    async with anyio.create_task_group() as tg:
        tg.start_soon(upstream.run)
        catalog = Catalog.from_tools(await upstream.list_tools())
        tg.cancel_scope.cancel()
    return catalog


catalog = anyio.run(fetch)
out = Path(__file__).parent / "catalogs" / f"{sys.argv[1] if len(sys.argv) > 1 else 'workspace'}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(catalog.model_dump_json(by_alias=True, exclude_none=True, indent=1))
print(f"{len(catalog.tools)} tools → {out}")
