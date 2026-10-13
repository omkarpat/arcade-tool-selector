"""Keyword retrieval over tool cards (BM25). The cheap first stage before the LLM gate."""

import re

from rank_bm25 import BM25Okapi

from arcade_tool_selector.cards import ToolCard, split_words


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, with names split into words."""
    return re.findall(r"[a-z0-9]+", split_words(text).lower())


class BM25Index:
    """BM25 over tool cards' searchable text."""

    def __init__(self, cards: list[ToolCard]):
        """Index these cards."""
        self.names = [c.name for c in cards]
        self._bm25 = BM25Okapi([tokenize(c.text()) for c in cards])

    def top(self, query: str, k: int) -> list[str]:
        """The k best-matching tool names, skipping tools with no match."""
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(zip(scores, self.names), key=lambda pair: -pair[0])
        return [name for score, name in ranked[:k] if score > 0]
