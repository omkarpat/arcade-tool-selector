# arcade-tool-selector

Prompt-based tool gating for [Arcade](https://arcade.dev). Instead of handing an LLM every Arcade tool and letting it choose, a selector picks the few tools relevant to the user's prompt, within a project-level scope, and only those reach the LLM.

The selection paradigm is borrowed from [jev-search](https://github.com/superagents-lab/jev-search): batched yes/no gates with probabilities, thresholds, pins and fallback defaults.

Status: planning. See [PLAN.md](PLAN.md).

## License

MIT
