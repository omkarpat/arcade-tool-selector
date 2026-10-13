"""What the vetter reads from the Arcade platform: the project's purpose, written in its gateway's instructions
("Guide your agent" in the dashboard). Read with the project's own API key; never forwarded to the agent."""

from arcadepy import Arcade
from arcadepy import BaseModel as ArcadeModel

from arcade_tool_selector.settings import Settings


class Gateway(ArcadeModel):
    instructions: str = ""


def read_purpose(settings: Settings) -> str:
    client = Arcade(api_key=settings.api_key)
    gateway = client.get(f"/v1/orgs/{settings.org_id}/projects/{settings.project_id}/gateways/{settings.gateway_id}", cast_to=Gateway)
    return gateway.instructions.strip()
