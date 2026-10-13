"""A gateway's tool list: what the proxy serves and the evals snapshot."""

from mcp import types
from pydantic import BaseModel


def tool_json(tool: types.Tool) -> dict:
    return tool.model_dump(mode="json", by_alias=True, exclude_none=True)


class Catalog(BaseModel):
    tools: list[types.Tool]

    def names(self) -> set[str]:
        return {t.name for t in self.tools}
