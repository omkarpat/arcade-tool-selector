import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from arcade_tool_selector.cards import build_cards
from arcade_tool_selector.catalog import Catalog
from arcade_tool_selector.retrieve import BM25Index

EVALS = Path(__file__).parents[1] / "evals"
SNAPSHOT = EVALS / "catalogs" / "workspace.json"
if not SNAPSHOT.exists():
    pytest.skip("no catalog snapshot; run `uv run python evals/snapshot.py workspace`", allow_module_level=True)
CATALOG = Catalog.model_validate_json(SNAPSHOT.read_text())


def test_tiers_from_arcade_metadata():
    cards = {c.name: c for c in build_cards(CATALOG)}
    assert cards["GoogleCalendar_ListEvents"].tier == "read"
    assert cards["Linear_CreateIssue"].tier == "write"
    assert cards["Gmail_SendEmail"].tier == "write"  # whether a write reaches people is judged per call
    assert cards["Gmail_TrashEmail"].tier == "destructive"
    assert Counter(c.tier for c in cards.values())[None] == 0

    send = cards["Gmail_SendEmail"]
    assert (send.qualified_name, send.toolkit, send.auth_provider) == ("Gmail.SendEmail", "Gmail", "google")
    assert "https://www.googleapis.com/auth/gmail.send" in send.scopes
    assert {"recipient", "subject", "body"} <= {p.name for p in send.params}


def test_bm25_finds_by_split_words():
    index = BM25Index(build_cards(CATALOG))
    assert "GoogleCalendar_FindTimeSlotsWhenEveryoneIsFree" in index.top("when is everyone free", 5)


def test_eval_dataset_runs_against_snapshot():
    result = subprocess.run([sys.executable, str(EVALS / "run.py")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "bm25@40" in result.stdout
