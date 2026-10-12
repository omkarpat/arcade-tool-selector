import hashlib
import os
import re
from pathlib import Path
from typing import Annotated

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, BeforeValidator, ConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

_DURATION = re.compile(r"^(\d+)([smhd]?)$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def load_env() -> None:
    """Load ATS_ENV_FILE (default: repo .env); real environment variables win."""
    path = Path(os.environ.get("ATS_ENV_FILE", REPO_ROOT / ".env")).expanduser()
    for key, value in dotenv_values(path).items():
        if value:
            os.environ.setdefault(key, value)


def parse_duration(value: str | int) -> float:
    match = _DURATION.match(str(value).strip())
    if not match:
        raise ValueError(f"bad duration {value!r}; use e.g. 30s, 15m, 24h")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def data_dir() -> Path:
    raw = os.environ.get("ATS_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA") or "~/.local/share/ats"
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


# Seconds; config writes 30s, 15m, 24h or a bare number.
Duration = Annotated[float, BeforeValidator(parse_duration)]


class CatalogConfig(BaseModel):
    refresh_interval: Duration = 15 * 60
    max_staleness: Duration = 24 * 3600


class ProjectConfig(BaseModel):
    # Selection, policy and judge keys belong to later phases.
    model_config = ConfigDict(extra="allow")

    project: str
    server_name: str
    catalog: CatalogConfig = CatalogConfig()


class Settings(BaseModel):
    gateway_url: str
    api_key: str
    user_id: str
    config: ProjectConfig
    data_dir: Path

    @property
    def gateway_key(self) -> str:
        return hashlib.sha256(self.gateway_url.rstrip("/").encode()).hexdigest()[:16]


def load_settings() -> Settings:
    load_env()
    config_path = Path(os.environ.get("ATS_CONFIG", "ats.yaml")).expanduser()
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path
    return Settings(
        gateway_url=os.environ["ATS_GATEWAY_URL"],
        api_key=os.environ["ARCADE_API_KEY"],
        user_id=os.environ["ARCADE_USER_ID"],
        config=ProjectConfig.model_validate(yaml.safe_load(config_path.read_text())),
        data_dir=data_dir(),
    )
