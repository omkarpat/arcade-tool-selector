"""Arcade's record of tool executions, keyed from our `call` audit events.

Gateway responses carry no execution id, so a call is matched on user, toolkit, tool and time.
"""

import json
import sys
from datetime import datetime, timedelta

from arcadepy import Arcade
from arcadepy import BaseModel as ArcadeModel
from pydantic import BaseModel

from arcade_tool_selector.settings import Settings, load_settings

MATCH_WINDOW = timedelta(seconds=30)


class ExecutionKey(BaseModel):
    user_id: str
    toolkit: str
    tool: str
    started_at: datetime

    @classmethod
    def for_gateway_tool(cls, gateway_name: str, user_id: str, started_at: datetime) -> "ExecutionKey":
        # Gateway tools are named <Toolkit>_<Tool>.
        toolkit, tool = gateway_name.split("_", 1)
        return cls(user_id=user_id, toolkit=toolkit, tool=tool, started_at=started_at)


class Execution(ArcadeModel):
    id: str
    tool_name: str
    toolkit_name: str
    execution_status: str
    created_at: datetime
    finished_at: datetime | None = None


class ExecutionsPage(ArcadeModel):
    items: list[Execution]


def find_execution(key: ExecutionKey, settings: Settings) -> Execution | None:
    """The Arcade execution closest to `key.started_at`, in the role's own project."""
    client = Arcade(api_key=settings.api_key)
    page = client.get(
        f"/v1/orgs/{settings.org_id}/projects/{settings.project_id}/tool_executions",
        cast_to=ExecutionsPage,
        options={
            "params": {
                "user_id": key.user_id,
                "toolkit_name": key.toolkit,
                "tool_name": key.tool,
                "start_time": (key.started_at - MATCH_WINDOW).isoformat(),
                "end_time": (key.started_at + MATCH_WINDOW).isoformat(),
            }
        },
    )
    return min(page.items, key=lambda e: abs(e.created_at - key.started_at), default=None)


def main() -> None:
    """`ATS_ROLE=<role> ats-audit [N]`: the role's last N audit events, each call resolved to its Arcade execution."""
    settings = load_settings()
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    for line in (settings.data_dir / "audit.jsonl").read_text().splitlines()[-count:]:
        event = json.loads(line)
        when = datetime.fromtimestamp(event.pop("ts")).strftime("%H:%M:%S")
        name = event.pop("event")
        if name == "call":
            execution = find_execution(ExecutionKey.model_validate(event), settings)
            arcade = f"{execution.execution_status} {execution.id}" if execution else "no Arcade record found"
            print(f"{when}  call  {event['toolkit']}.{event['tool']}  → {arcade}")
        else:
            print(f"{when}  {name}  {json.dumps(event)[:140]}")
