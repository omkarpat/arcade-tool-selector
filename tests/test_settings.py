import os

from arcade_tool_selector import settings


def test_role_file_overlays_repo_env_and_can_reference_it(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("DEFAULT_ARCADE_API_KEY=arc_workspace\nSUPPORT_BOT_ARCADE_API_KEY=arc_support\nOPENAI_API_KEY=sk-shared\n")
    role = tmp_path / "support-bot.env"
    role.write_text("DEFAULT_ARCADE_API_KEY=${SUPPORT_BOT_ARCADE_API_KEY}\nATS_GATEWAY_URL=https://api.arcade.dev/mcp/gw_support\n")
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    monkeypatch.setattr(os, "environ", {"ATS_ENV_FILE": str(role)})  # load_env writes here; keep it out of other tests

    settings.load_env()

    assert os.environ["DEFAULT_ARCADE_API_KEY"] == "arc_support"  # the role's key, from the repo .env
    assert os.environ["OPENAI_API_KEY"] == "sk-shared"  # inherited
    assert os.environ["ATS_GATEWAY_URL"] == "https://api.arcade.dev/mcp/gw_support"


def test_role_from_the_roles_dict(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "roles").mkdir(parents=True)
    (repo / "roles" / "support-bot.yaml").write_text("project: support-bot\nserver_name: support-bot\n")
    (repo / ".env").write_text(
        "DEFAULT_ARCADE_API_KEY=arc_workspace\nATS_GATEWAY_URL=https://api.arcade.dev/mcp/gw_workspace\nARCADE_USER_ID=me@acme.com\n"
        'ATS_ROLES=\'{"support-bot": {"project_id": "7d77", "api_key": "arc_support", '
        '"gateway_url": "https://api.arcade.dev/mcp/gw_support", "config": "roles/support-bot.yaml"}}\'\n'
    )
    monkeypatch.setattr(settings, "REPO_ROOT", repo)
    monkeypatch.setattr(os, "environ", {"ATS_ROLE": "support-bot", "ATS_DATA_DIR": str(tmp_path / "data")})

    loaded = settings.load_settings()

    assert (loaded.api_key, loaded.gateway_url, loaded.config.project) == ("arc_support", "https://api.arcade.dev/mcp/gw_support", "support-bot")
    assert loaded.data_dir == tmp_path / "data" / "support-bot"  # the role's own cache, audit log and hook socket
