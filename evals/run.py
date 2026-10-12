"""Selection eval: `uv run python evals/run.py [catalog] [dataset]`.

The catalog is a local snapshot of your gateway (gitignored): `uv run python evals/snapshot.py workspace`.

Scores each selector on the labeled prompts:
  recall    share of a prompt's needs covered by the selection (any tool in a need's group counts)
  solved    share of prompts with every need covered
  precision share of selected tools that are needed or acceptable (`ok`)
  size      mean tools selected; on no-tool prompts (`none`) the ideal is 0
  tokens    mean schema tokens handed to the model (~chars/4), vs. sending every tool
"""

import json
import statistics
import sys
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel

from arcade_tool_selector.cards import build_cards
from arcade_tool_selector.catalog import Catalog, tool_json
from arcade_tool_selector.retrieve import BM25Index

HERE = Path(__file__).parent


class Case(BaseModel):
    prompt: str
    needs: list[list[str]]
    ok: list[str] = []
    tags: list[str] = []


class Score(BaseModel):
    selector: str
    recall: float
    solved: float
    precision: float
    size: float
    none_size: float
    tokens: float


Selector = Callable[[str], list[str]]


def score(name: str, select: Selector, cases: list[Case], tokens_per_tool: dict[str, int]) -> Score:
    recalls, solved, precisions, sizes, none_sizes, tokens = [], [], [], [], [], []
    for case in cases:
        picked = select(case.prompt)
        tokens.append(sum(tokens_per_tool[t] for t in picked))
        if not case.needs:
            none_sizes.append(len(picked))
            continue
        covered = [any(t in picked for t in group) for group in case.needs]
        relevant = {t for group in case.needs for t in group} | set(case.ok)
        recalls.append(sum(covered) / len(covered))
        solved.append(all(covered))
        precisions.append(len(relevant & set(picked)) / len(picked) if picked else 0.0)
        sizes.append(len(picked))
    return Score(
        selector=name,
        recall=statistics.mean(recalls),
        solved=statistics.mean(solved),
        precision=statistics.mean(precisions),
        size=statistics.mean(sizes),
        none_size=statistics.mean(none_sizes) if none_sizes else 0.0,
        tokens=statistics.mean(tokens),
    )


def main() -> None:
    catalog_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "catalogs" / "workspace.json"
    dataset_path = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "datasets" / "tool_selection.yaml"
    catalog = Catalog.model_validate_json(catalog_path.read_text())
    cases = [Case.model_validate(c) for c in yaml.safe_load(dataset_path.read_text())]

    names = catalog.names()
    unknown = sorted({t for c in cases for t in [*(t for g in c.needs for t in g), *c.ok]} - names)
    if unknown:
        sys.exit(f"dataset names tools missing from the catalog: {unknown}")

    cards = build_cards(catalog)
    tokens_per_tool = {t.name: len(json.dumps(tool_json(t))) // 4 for t in catalog.tools}
    index = BM25Index(cards)
    selectors: dict[str, Selector] = {"all tools": lambda prompt: sorted(names)}
    for k in (5, 8, 10, 20, 40):
        selectors[f"bm25@{k}"] = lambda prompt, k=k: index.top(prompt, k)

    n_none = sum(1 for c in cases if not c.needs)
    print(f"{catalog_path.name}: {len(catalog.tools)} tools · {dataset_path.name}: {len(cases)} prompts ({n_none} need no tool)\n")
    print(f"{'selector':<10} {'recall':>7} {'solved':>7} {'precision':>9} {'size':>6} {'size(none)':>10} {'tokens':>8}")
    for name, select in selectors.items():
        s = score(name, select, cases, tokens_per_tool)
        print(f"{s.selector:<10} {s.recall:>7.1%} {s.solved:>7.1%} {s.precision:>9.1%} {s.size:>6.1f} {s.none_size:>10.1f} {s.tokens:>8,.0f}")


if __name__ == "__main__":
    main()
