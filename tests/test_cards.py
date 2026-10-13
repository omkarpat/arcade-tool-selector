"""Tool cards, against the local catalog snapshot (skipped without one)."""

from collections import Counter
from pathlib import Path

import pytest

from arcade_tool_selector.cards import build_cards
from arcade_tool_selector.catalog import Catalog

EVALS = Path(__file__).parents[1] / "evals"
SNAPSHOT = EVALS / "catalogs" / "workspace.json"
if not SNAPSHOT.exists():
    pytest.skip("no catalog snapshot; run `uv run python evals/snapshot.py workspace`", allow_module_level=True)
CATALOG = Catalog.model_validate_json(SNAPSHOT.read_text())


def test_tiers_from_arcade_metadata():
    """Tiers come from Arcade's metadata; a send is still a write, since whether it reaches people is judged per call."""
    cards = {c.name: c for c in build_cards(CATALOG)}
    assert cards["GoogleCalendar_ListEvents"].tier == "read"
    assert cards["Linear_CreateIssue"].tier == "write"
    assert cards["Gmail_SendEmail"].tier == "write"
    assert cards["Gmail_TrashEmail"].tier == "destructive"
    assert Counter(c.tier for c in cards.values())[None] == 0

    send = cards["Gmail_SendEmail"]
    assert (send.qualified_name, send.toolkit, send.auth_provider) == ("Gmail.SendEmail", "Gmail", "google")
    assert "https://www.googleapis.com/auth/gmail.send" in send.scopes
    assert {"recipient", "subject", "body"} <= {p.name for p in send.params}
