"""Settings: the repo .env and the Arcade platform are the only sources; nothing else is configured locally.

.env holds ARCADE_USER_ID, ARCADE_ORG_ID, OPENAI_API_KEY and ATS_ROLES (one Arcade project per role). ATS_ROLE,
set when Claude Code is launched, picks the role. Each role's purpose is its gateway's instructions on
the Arcade platform (see platform.py).
"""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, TypeAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path("~/.local/share/ats").expanduser()


def load_env() -> None:
    """Load the repo .env into the environment. The file is the source: it overrides inherited values."""
    os.environ.update({key: value for key, value in dotenv_values(REPO_ROOT / ".env").items() if value})


class Role(BaseModel):
    """One Arcade project acting as a role: its own key and gateway."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    api_key: str
    gateway_url: str

    @property
    def gateway_id(self) -> str:
        return self.gateway_url.rstrip("/").rsplit("/", 1)[-1]


class Settings(BaseModel):
    role: str
    project_id: str
    api_key: str
    gateway_url: str
    gateway_id: str
    org_id: str
    user_id: str
    openai_api_key: str
    data_dir: Path
    otel: bool

    @property
    def org_domain(self) -> str:
        """Who counts as inside the organization: the user's own email domain."""
        return self.user_id.rsplit("@", 1)[-1]


def load_settings() -> Settings:
    load_env()
    if not (name := os.environ.get("ATS_ROLE")):
        raise SystemExit("ATS_ROLE is not set: name one of the roles in ATS_ROLES in .env")
    roles = TypeAdapter(dict[str, Role]).validate_json(os.environ["ATS_ROLES"])
    if name not in roles:
        raise SystemExit(f"ATS_ROLE={name!r} is not in ATS_ROLES in .env (roles: {', '.join(roles)})")
    role = roles[name]
    # The role's audit and proxy logs. ATS_DATA_DIR only isolates tests.
    data_dir = Path(os.environ.get("ATS_DATA_DIR", DATA_ROOT)) / name
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        role=name,
        project_id=role.project_id,
        api_key=role.api_key,
        gateway_url=role.gateway_url,
        gateway_id=role.gateway_id,
        org_id=os.environ["ARCADE_ORG_ID"],
        user_id=os.environ["ARCADE_USER_ID"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        data_dir=data_dir,
        otel=bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")),
    )
