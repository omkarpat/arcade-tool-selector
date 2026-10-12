"""Snapshot the proxy's cached gateway catalog for reproducible evals: `uv run python evals/snapshot.py workspace`."""

import sys
from pathlib import Path

from arcade_tool_selector.catalog import CatalogStore
from arcade_tool_selector.settings import load_settings

settings = load_settings()
catalog = CatalogStore(settings.data_dir, settings.gateway_key).load()
out = Path(__file__).parent / "catalogs" / f"{sys.argv[1]}.json"
out.write_text(catalog.model_dump_json(by_alias=True, exclude_none=True, indent=1))
print(f"{len(catalog.tools)} tools → {out}")
