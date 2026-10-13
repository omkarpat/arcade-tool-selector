import hashlib
import io
import os
import re
from pathlib import Path
from typing import Annotated

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, BeforeValidator, ConfigDict, TypeAdapter

from arcade_tool_selector.policy import PolicyConfig, ScopeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

_DURATION = re.compile(r"^(\d+)([smhd]?)$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def load_env() -> None:
    """Load the repo .env, then ATS_ENV_FILE (e.g. a role's file) on top; real environment variables win.

    Read as one file, so the overlay can reference repo .env values with ${NAME}.
    """
    files = [REPO_ROOT / ".env"]
    if "ATS_ENV_FILE" in os.environ:
        files.append(Path(os.environ["ATS_ENV_FILE"]).expanduser())
    text = "\n".join(path.read_text() for path in files if path.exists())
    for key, value in dotenv_values(stream=io.StringIO(text)).items():
        if value:
            os.environ.setdefault(key, value)


def parse_duration(value: str | int) -> float:
    match = _DURATION.match(str(value).strip())
    if not match:
        raise ValueError(f"bad duration {value!r}; use e.g. 30s, 15m, 24h")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


class Role(BaseModel):
    """One Arcade project acting as a role: its own key, gateway and project config."""

    project_id: str
    api_key: str
    gateway_url: str
    config: str


def active_role() -> Role | None:
    """The role named by ATS_ROLE, from the ATS_ROLES dict in .env. None: the top-level settings apply."""
    name = os.environ.get("ATS_ROLE")
    return TypeAdapter(dict[str, Role]).validate_json(os.environ["ATS_ROLES"])[name] if name else None


def data_dir() -> Path:
    raw = os.environ.get("ATS_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA") or "~/.local/share/ats"
    path = Path(raw).expanduser()
    # Each role keeps its own cache, audit log and hook socket; its proxy and hooks both derive it from ATS_ROLE.
    if role := os.environ.get("ATS_ROLE"):
        path = path / role
    path.mkdir(parents=True, exist_ok=True)
    return path


# Seconds; config writes 30s, 15m, 24h or a bare number.
Duration = Annotated[float, BeforeValidator(parse_duration)]


class CatalogConfig(BaseModel):
    refresh_interval: Duration = 15 * 60
    max_staleness: Duration = 24 * 3600


class ProjectConfig(BaseModel):
    # Selection and judge keys belong to later phases.
    model_config = ConfigDict(extra="allow")

    project: str
    server_name: str
    project_prompt: str | None = None  # overrides the gateway's instructions (tests, demos, several purposes on one gateway)
    catalog: CatalogConfig = CatalogConfig()
    scope: ScopeConfig = ScopeConfig()
    policy: PolicyConfig = PolicyConfig()


class Settings(BaseModel):
    gateway_url: str
    api_key: str
    user_id: str
    config: ProjectConfig
    data_dir: Path
    otel: bool

    @property
    def gateway_key(self) -> str:
        return hashlib.sha256(self.gateway_url.rstrip("/").encode()).hexdigest()[:16]


def load_settings() -> Settings:
    load_env()
    if role := active_role():
        gateway_url, api_key, config = role.gateway_url, role.api_key, role.config
    else:
        gateway_url, api_key, config = os.environ["ATS_GATEWAY_URL"], os.environ["DEFAULT_ARCADE_API_KEY"], os.environ.get("ATS_CONFIG", "ats.yaml")
    config_path = Path(config).expanduser()
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path
    return Settings(
        gateway_url=gateway_url,
        api_key=api_key,
        user_id=os.environ["ARCADE_USER_ID"],
        config=ProjectConfig.model_validate(yaml.safe_load(config_path.read_text())),
        data_dir=data_dir(),
        otel=bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")),
    )
