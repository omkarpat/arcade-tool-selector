"""Tool cards: what the selector and vetter know about each gateway tool.

Built from the gateway's tools/list alone: Arcade attaches its own metadata under `_meta.arcade`
(operations, read_only/destructive flags, service domain, OAuth requirements).
"""

import re
from typing import Literal

from mcp import types
from pydantic import BaseModel

from arcade_tool_selector.catalog import Catalog

Tier = Literal["read", "write", "send", "destructive"]

# Arcade flags read-only and destructive tools but has no "reaches people" flag:
# these create/update tools email, message, invite or share outside the user.
SEND_TOOLS = {
    "Gmail.SendEmail",
    "Gmail.SendDraftEmail",
    "Gmail.ReplyToEmail",
    "Gmail.ForwardEmail",
    "Slack.SendMessage",
    "GoogleCalendar.CreateEvent",
    "GoogleCalendar.UpdateEvent",
    "GoogleCalendar.RespondToEvent",
    "GoogleDrive.ShareFile",
    "GoogleDrive.SetGeneralAccess",
    "Arcade.Registry_Create_Suggestion",
}


class Param(BaseModel):
    name: str
    description: str = ""
    required: bool


class ToolCard(BaseModel):
    name: str  # gateway name, e.g. Gmail_SendEmail
    qualified_name: str  # Arcade name, e.g. Gmail.SendEmail
    toolkit: str
    description: str
    params: list[Param]
    operations: list[str]
    domains: list[str]
    auth_provider: str | None
    scopes: list[str]
    tier: Tier | None  # None: Arcade gave no behavior metadata

    def text(self) -> str:
        """Searchable text: name words, description, parameter names and descriptions."""
        params = " ".join(f"{p.name} {p.description}" for p in self.params)
        return f"{split_words(self.qualified_name)} {self.description} {split_words(params)}"


def split_words(text: str) -> str:
    """`Gmail.ListEmails send_notifications` → `Gmail List Emails send notifications`."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|[._]", " ", text)


def card_for(tool: types.Tool, tier_overrides: dict[str, Tier]) -> ToolCard:
    arcade = (tool.meta or {}).get("arcade", {})
    behavior = arcade.get("metadata", {}).get("behavior")
    auth = arcade.get("requirements", {}).get("authorization") or {}
    qualified = tool.annotations.title if tool.annotations and tool.annotations.title else tool.name.replace("_", ".", 1)
    schema = tool.input_schema
    return ToolCard(
        name=tool.name,
        qualified_name=qualified,
        toolkit=tool.name.split("_", 1)[0],
        description=tool.description or "",
        params=[
            Param(name=n, description=p.get("description", ""), required=n in schema.get("required", []))
            for n, p in schema.get("properties", {}).items()
        ],
        operations=behavior["operations"] if behavior else [],
        domains=arcade.get("metadata", {}).get("classification", {}).get("service_domains", []),
        auth_provider=auth.get("provider_id"),
        scopes=(auth.get("oauth2") or {}).get("scopes", []),
        tier=tier_overrides.get(tool.name) or _tier(qualified, behavior),
    )


def _tier(qualified_name: str, behavior: dict | None) -> Tier | None:
    if qualified_name in SEND_TOOLS:
        return "send"
    if behavior is None:
        return None
    if behavior["destructive"]:
        return "destructive"
    return "read" if behavior["read_only"] else "write"


def build_cards(catalog: Catalog, tier_overrides: dict[str, Tier] | None = None) -> list[ToolCard]:
    return [card_for(tool, tier_overrides or {}) for tool in catalog.tools]
