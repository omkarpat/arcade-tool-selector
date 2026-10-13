"""A gateway's tool list: what the proxy serves and the evals snapshot."""

from mcp import types
from pydantic import BaseModel


def tool_json(tool: types.Tool) -> dict:
    """The tool as JSON, the way the gateway sent it."""
    return tool.model_dump(mode="json", by_alias=True, exclude_none=True)


class Catalog(BaseModel):
    """Every tool on a gateway."""

    tools: list[types.Tool]

    def names(self) -> set[str]:
        """The tools' gateway names."""
        return {t.name for t in self.tools}
