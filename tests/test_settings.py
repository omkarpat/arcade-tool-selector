import os

from arcade_tool_selector import settings


def test_role_file_overlays_repo_env_and_can_reference_it(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("ARCADE_API_KEY=arc_workspace\nSUPPORT_BOT_ARCADE_API_KEY=arc_support\nOPENAI_API_KEY=sk-shared\n")
    role = tmp_path / "support-bot.env"
    role.write_text("ARCADE_API_KEY=${SUPPORT_BOT_ARCADE_API_KEY}\nATS_GATEWAY_URL=https://api.arcade.dev/mcp/gw_support\n")
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    monkeypatch.setattr(os, "environ", {"ATS_ENV_FILE": str(role)})  # load_env writes here; keep it out of other tests

    settings.load_env()

    assert os.environ["ARCADE_API_KEY"] == "arc_support"  # the role's key, from the repo .env
    assert os.environ["OPENAI_API_KEY"] == "sk-shared"  # inherited
    assert os.environ["ATS_GATEWAY_URL"] == "https://api.arcade.dev/mcp/gw_support"
