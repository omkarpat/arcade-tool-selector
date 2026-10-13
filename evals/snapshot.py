"""Snapshot your gateway's tool catalog for the evals: `uv run python evals/snapshot.py [name]` (default: workspace).

Reads every tools/list page straight from the gateway in .env; no proxy needed. Snapshots are gitignored.
"""

import sys
from pathlib import Path

import anyio

from arcade_tool_selector import gateway
from arcade_tool_selector.catalog import Catalog
from arcade_tool_selector.settings import load_settings

catalog = Catalog(tools=anyio.run(gateway.list_tools, load_settings()))
out = Path(__file__).parent / "catalogs" / f"{sys.argv[1] if len(sys.argv) > 1 else 'workspace'}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(catalog.model_dump_json(by_alias=True, exclude_none=True, indent=1))
print(f"{len(catalog.tools)} tools → {out}")
