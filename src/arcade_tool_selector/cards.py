"""Tool cards: what the selector and vetter know about each gateway tool.

Built from the gateway's tools/list alone: Arcade attaches its own metadata under `_meta.arcade`
(operations, read_only/destructive flags, service domain, OAuth requirements).

Tiers: read, write and destructive come from Arcade's metadata; send is judged per call (policy.py).
"""

import re
from typing import Literal

from mcp import types
from pydantic import BaseModel

from arcade_tool_selector.catalog import Catalog

Tier = Literal["read", "write", "send", "destructive"]


class Param(BaseModel):
    """One input parameter of a tool."""

    name: str
    description: str = ""
    required: bool


class ToolCard(BaseModel):
    """What the selector and vetter know about one gateway tool.

    name: the gateway name, e.g. Gmail_SendEmail.
    qualified_name: the Arcade name, e.g. Gmail.SendEmail.
    tier: None when Arcade gave no behavior metadata.
    """

    name: str
    qualified_name: str
    toolkit: str
    description: str
    params: list[Param]
    operations: list[str]
    domains: list[str]
    auth_provider: str | None
    scopes: list[str]
    tier: Tier | None

    def text(self) -> str:
        """Searchable text: name words, description, parameter names and descriptions."""
        params = " ".join(f"{p.name} {p.description}" for p in self.params)
        return f"{split_words(self.qualified_name)} {self.description} {split_words(params)}"


def split_words(text: str) -> str:
    """`Gmail.ListEmails send_notifications` → `Gmail List Emails send notifications`."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|[._]", " ", text)


def card_for(tool: types.Tool) -> ToolCard:
    """A tool's card, from its tools/list entry and Arcade's `_meta.arcade`."""
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
        tier=_tier(behavior),
    )


def _tier(behavior: dict | None) -> Tier | None:
    """The tier Arcade's behavior flags imply: destructive, read or write."""
    if behavior is None:
        return None
    if behavior["destructive"]:
        return "destructive"
    return "read" if behavior["read_only"] else "write"


def build_cards(catalog: Catalog) -> list[ToolCard]:
    """A card for every tool in the catalog."""
    return [card_for(tool) for tool in catalog.tools]
