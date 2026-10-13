"""Settings come from the repo .env and ATS_ROLE alone, and fail loudly when either is wrong."""

import json
import os

import pytest

from arcade_tool_selector import settings

ROLES = {"support-bot": {"project_id": "7d77", "api_key": "arc_support", "gateway_url": "https://api.arcade.dev/mcp/gw_support"}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo root holding a complete .env with one role."""
    (tmp_path / ".env").write_text(
        f"ARCADE_USER_ID=me@acme.com\nARCADE_ORG_ID=org1\nOPENAI_API_KEY=sk-test\nATS_ROLES='{json.dumps(ROLES)}'\n"
    )
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    return tmp_path


def test_role_settings_come_from_env_only(repo, tmp_path, monkeypatch):
    """.env is the source: it beats an inherited value, ATS_ROLE only selects, and the role gets its own data dir."""
    monkeypatch.setattr(os, "environ", {"ATS_ROLE": "support-bot", "ATS_DATA_DIR": str(tmp_path / "data"), "OPENAI_API_KEY": "sk-stale"})

    loaded = settings.load_settings()

    assert (loaded.api_key, loaded.gateway_id, loaded.project_id) == ("arc_support", "gw_support", "7d77")
    assert (loaded.openai_api_key, loaded.org_domain) == ("sk-test", "acme.com")
    assert loaded.data_dir == tmp_path / "data" / "support-bot"


def test_missing_or_unknown_role_fails_loudly(repo, monkeypatch):
    """A missing or unknown ATS_ROLE exits with a message naming the fix."""
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(SystemExit, match="ATS_ROLE is not set"):
        settings.load_settings()
    monkeypatch.setattr(os, "environ", {"ATS_ROLE": "nope"})
    with pytest.raises(SystemExit, match="not in ATS_ROLES"):
        settings.load_settings()
