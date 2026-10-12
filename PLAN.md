# Plan: arcade-tool-selector

Status: **draft for review**. Nothing is built yet. Each phase below is meant to land as its own PR.

## 1. Problem

Today an Arcade-backed agent is handed a toolkit (or a gateway's whole "Allowed Tools" list) and the LLM decides which tool to call. As the catalog grows this costs tokens on every turn, degrades tool choice (near-duplicate tools like `Gmail.ListEmails` vs `Gmail.SearchThreads`), and widens the blast radius of a bad call.

Goal: put a **tool selector** in front of the LLM. Given the project's configuration and the user's prompt, it returns the small set of Arcade tools the LLM is allowed to see for that turn. The LLM still picks among them and fills arguments; it never sees the rest.

Non-goals (for now): replacing Arcade auth/execution, a hosted SaaS, generating tool arguments.

## 2. Prior art we borrow from: jev-search

[superagents-lab/jev-search](https://github.com/superagents-lab/jev-search) (MIT) picks which of 12 search sources to query for a natural-language request. What we take from it:

| jev-search idea | Here |
|---|---|
| Each source carries a hand-written `ask: {question, yes, no}` | Each tool (and toolkit) carries a **tool card** with a yes/no question and true/false criteria, incl. what a false match looks like |
| One batched call; the model returns a **probability per yes/no question**, never free text | One batched gate call over the shortlist; output is `P(tool needed)` per tool |
| Keep `p >= 0.6`; if none pass, fall back to `defaultOn` | Same rule, with a per-project default set |
| User's explicit source choice overrides the model | Project pins / user pins override the model |
| Code proposes candidates, model only chooses | The model can only score tools we hand it, so it can't invent a tool name |
| Provider adapters + fallback chain on 402/429/5xx | Same, behind a `Judge` interface |
| Batched reranking (40 items per call) | Gate batches of ~40 tools |

What we do **not** take: query rewriting, time windows, URL dedup, streaming UI. And what we must **add**, because jev-search doesn't have it:
- **Scale:** jev asks one question per entry over 12 entries. An Arcade catalog has hundreds or thousands of tools, so we need a cheap retrieval stage before the per-tool gate.
- **Selection caching:** jev only caches search results.
- **Evaluation:** jev has no ground truth. We need a labeled prompt→tools set and recall metrics from day one.

## 3. What Arcade gives us (from the docs)

- **Catalog:** `client.tools.list(toolkit=, user_id=, limit<=100, offset=, include_format=[...])` returns `ToolDefinition`s, which carry:
  - `qualified_name` / `fully_qualified_name`, `description`, `toolkit{name, description, version}`
  - `input.parameters[]`
  - `requirements.authorization` (and `requirements.met` when a `user_id` is passed)
- **Formatted schemas:** `client.tools.formatted.get(name, format="openai"|"anthropic")` produces the per-provider schema for the chosen tools.
- **Execution:** `client.tools.execute(...)` runs a tool, and `client.tools.authorize(...)` + `auth.wait_for_completion(...)` handles the auth flow. We don't touch these.
- **Framework helpers already accept an explicit tool list,** so our output plugs straight in:
  - OpenAI Agents: `get_arcade_tools(tools=[...])`
  - Vercel AI: `toZodToolSet`
  - LangChain: `tools.get`
- **MCP Gateways:** project-scoped, with a static "Allowed Tools" list. This is the natural **project-level outer boundary**.
- **Contextual Access hooks** (access / pre / post webhooks, org or project level) can allow or deny toolkits and calls, but **never see the prompt**. They're useful for enforcement, not selection.
- **arcade-mcp** middleware: `on_list_tools` / `on_call_tool` let a custom MCP server filter or wrap tools.
- **Nothing built-in does prompt-based selection.** Arcade Labs' experimental "Omni" does dynamic discovery, but it's not something to build on.
- **Legacy LLM API:** `/v1/chat/completions` with `tools=[names]` accepts a per-call subset, but it's gone from the current docs, so we won't depend on it.

## 4. Design

### 4.1 Pipeline

```
                       project config (yaml)
                       ├─ allowlist (toolkits/tools)   ← mirrors gateway Allowed Tools
                       ├─ project prompt (what this agent is for)
                       ├─ pins / defaults / k / threshold
                       ▼
Arcade tools.list ──► Catalog ──► Project scope ──► Shortlist ──► Gate ──► Selection
  (synced, cached)    tool cards   (static, cached   (per prompt,  (per prompt,   names + scores
                                    per project)      cheap)        LLM, batched)  + reasons
                                                                                  ▼
                                                     tools.formatted.get / framework helper
                                                                                  ▼
                                                                         LLM sees only these
```

1. **Catalog sync.** Page through `tools.list` for the project's toolkits and normalize each tool into a `ToolCard`:
   - `qualified_name`, `toolkit`, `description`, param names/descriptions, `auth_provider`, `version`
   - the gate question with `criteria_true` / `criteria_false`, generated once by an LLM from the definition, overridable by hand in a YAML file, and versioned by catalog hash
2. **Project scope** (once per project, cached until the catalog or config changes). The allowlist intersects with the catalog. If a *project prompt* is set ("Support agent for billing questions"), a toolkit-level gate (same mechanism as step 4, but over toolkits) drops toolkits that are irrelevant to the project. The result is the project's candidate pool.
3. **Shortlist** (per prompt, no LLM).
   - Hybrid retrieval over the candidate pool: BM25 over name, description and params, plus embeddings, fused with reciprocal rank fusion.
   - Take the top `N` (default 40, one gate batch).
   - Skip this stage when the pool is ≤ N.
4. **Gate** (per prompt, one LLM call per batch of ≤40). Each shortlisted tool gets a yes/no question: *"Would answering this request plausibly require calling `Gmail.SendEmail`?"*, along with its true/false criteria. The judge returns `P(yes)` per tool.
5. **Select.**
   - Pins always go in; denied tools never do.
   - Keep `p ≥ threshold` (default 0.6), sorted by p, capped at `k` (default 8).
   - If nothing passes, use the project's `defaults`.
   - Return names, scores and a short trace.
6. **Hand-off.** The caller turns the names into provider-formatted tools through the Arcade SDK or a framework helper.

### 4.2 Multi-turn

The prompt for selection is the latest user message plus a compact summary of the conversation so far. Tools selected in earlier turns get a "sticky" bonus, so follow-ups ("now send it") keep the tool that's mid-task. There's also an escape hatch: an always-on `request_more_tools(need: str)` meta-tool. It lets the LLM ask for a re-selection when the current set is insufficient. This is optional per project.

### 4.3 Judge providers

`Judge` is a protocol: `score(request, items: list[GateItem]) -> dict[name, float]`.

| Adapter | How it returns a probability |
|---|---|
| OpenAI Decisions API | Native predicate probabilities (what jev-search uses) |
| OpenAI chat with logprobs | `P(yes)` from the first-token logprobs of a per-item yes/no |
| Anthropic | Structured output `{name: {needed: bool, confidence: 0–1}}`. Claude exposes no logprobs, so confidence is model-stated; we calibrate on the eval set |

The adapters share a fallback chain on 402, 429 and 5xx, plus a timeout. If every judge fails, the selector degrades to shortlist top-k.

### 4.4 Caching

| What | Key | Store |
|---|---|---|
| Catalog + tool cards | toolkit + version | local file / sqlite |
| Embeddings | card hash | same |
| Project scope | project config hash + catalog hash | same |
| Selections | project + normalized prompt (+ sticky set) | LRU, short TTL |

The stores are pluggable so a Redis backend can be added later.

### 4.5 Integration surfaces (in build order)

1. **Python library (primary).** Usage: `selector.select(prompt, user_id=None, history=None) -> Selection`. It includes helpers to emit OpenAI or Anthropic tool lists and to feed `get_arcade_tools(tools=...)`. It works with any framework, and the selector sees the prompt directly.
2. **MCP server (arcade-mcp)** for clients we don't control, such as Claude Desktop or Cursor. Two modes:
   - **Meta-tool mode:** `find_tools(task)` returns the selected tools' schemas, and `call_tool(name, args)` proxies `tools.execute` / `authorize`. Robust, works in every client.
   - **Dynamic mode:** `find_tools` mutates the session's tool list via `on_list_tools` and emits `notifications/tools/list_changed`. Nicer UX, but only for clients that honor list_changed.

   The server would be registered as a remote MCP server inside an Arcade project and exposed through a gateway.
3. **Contextual Access pre-hook (optional, enforcement only).** A webhook that denies any `tools.execute` for a tool outside the project allowlist or the last selection for that user/session. This is defense-in-depth, so a model can't call a tool it wasn't given.
4. **TypeScript port.** Later, only if there's demand, built on `toZodToolSet`.

### 4.6 Project config (sketch)

```yaml
project: support-bot
arcade:
  toolkits: [gmail, slack, linear, zendesk]     # catalog scope
  allow: []                                     # optional explicit tool allowlist
  deny: [Gmail.TrashEmail]
project_prompt: >
  Customer support agent. Reads and replies to customer email,
  files and updates Linear issues, posts to #support in Slack.
selection:
  k: 8
  threshold: 0.6
  shortlist: 40
  pins: [Linear.SearchIssues]
  defaults: [Gmail.ListEmails, Linear.SearchIssues]
  sticky_turns: 2
  request_more_tools: true
judge:
  chain: [openai-decisions:gpt-6-luna, anthropic:claude-haiku-5-5]
cards:
  overrides: cards/support-bot.yaml           # hand-tuned questions/criteria
```

## 5. Evaluation (built in phase 1, not bolted on)

- **Dataset.** Labeled `prompt → required tools (+ acceptable alternates)` per project.
  - Seed it with ~150 hand-written prompts across Gmail, Slack, Linear, GitHub, Google Calendar and Drive.
  - Add hard negatives: look-alike tools, prompts that need zero tools, prompts that need two toolkits.
- **Metrics.**
  - **Recall@selection** is the headline: missing a needed tool is worse than including an extra one.
  - Precision, mean selected-set size, and the empty-selection fallback rate.
  - Latency p50/p95, and tokens saved vs. sending the full toolkit.
- **Ablations.**
  - shortlist only vs. gate only vs. both
  - BM25 vs. embeddings vs. hybrid
  - generated vs. hand-tuned cards
  - the threshold sweep
  - each judge adapter
- **End-to-end check.** On a subset, run a real agent loop and compare the tool actually called against the full-catalog baseline.
- **CI.** The eval runs on PRs touching selection logic, with recorded judge responses so CI is deterministic and free. A live run happens on demand.

## 6. Repo layout

```
arcade-tool-selector/
├─ src/arcade_tool_selector/
│  ├─ catalog.py        # Arcade sync → ToolCard
│  ├─ cards.py          # gate question generation + overrides
│  ├─ project.py        # config load, project scope
│  ├─ retrieve.py       # BM25 + embeddings + RRF shortlist
│  ├─ judge/            # base.py, openai_decisions.py, openai_logprobs.py, anthropic.py
│  ├─ select.py         # gate → threshold/pins/defaults/k → Selection
│  ├─ cache.py
│  └─ adapters/         # openai/anthropic tool lists, openai-agents, langchain
├─ mcp_server/          # arcade-mcp meta-tool + dynamic modes
├─ hooks/               # optional Contextual Access pre-hook
├─ evals/               # datasets/, run.py, recorded/
├─ examples/            # minimal agent loops per framework
└─ tests/
```

The stack is Python 3.11+ and `arcadepy`, with `uv` for packaging and an MIT license. The only abstraction is the judge layer. Each provider's official SDK sits behind it, so there's no hand-rolled REST.

## 7. Phases

| # | Deliverable | Done when |
|---|---|---|
| 0 | This plan, README, license | Plan reviewed |
| 1 | Catalog sync + `ToolCard` + eval dataset v0 + eval harness with a "send everything" baseline and a BM25-only baseline | `evals/run.py` prints recall/precision/size for the baselines on a real catalog |
| 2 | Judge interface + one adapter + batched gate + select rules (threshold, pins, defaults, k) | Gate beats the BM25 baseline on recall at equal set size |
| 3 | Hybrid shortlist (embeddings + RRF) + caching | Shortlist recall@40 ≥ 98% on the eval set; p95 latency target met |
| 4 | Project config + project scope (allowlist + project-prompt toolkit gate) | Two example projects with distinct pools; eval per project |
| 5 | Card generation + override file | Generated vs. hand-tuned ablation reported |
| 6 | Library adapters + examples (OpenAI Agents, Anthropic, LangChain) | Examples run end-to-end against Arcade |
| 7 | MCP server, meta-tool mode | Works in Claude Desktop / Cursor through an Arcade gateway |
| 8 | MCP dynamic mode + multi-turn sticky + `request_more_tools` | Multi-turn eval slice passes |
| 9 | Contextual Access pre-hook (optional) | Out-of-selection calls denied in a demo project |
| 10 | Packaging, docs, PyPI release | `pip install arcade-tool-selector` |

## 8. Open questions for review

1. **"Project level."** Is the project scope meant to be config in this repo (YAML per project), or tied to an **Arcade project / MCP gateway** (read the gateway's Allowed Tools as the allowlist)? The docs show no public API to read gateway config. If that holds, YAML mirrors the gateway by hand.
2. **Primary consumer.** Is the first target your own agent code (library-first, as planned) or third-party MCP clients (MCP server first)?
3. **Default judge.** Is it the OpenAI Decisions API (closest to jev-search, native probabilities) or Anthropic (stated confidence, needs calibration)?
4. **Granularity.** Should we select individual tools, or allow selecting whole toolkits when most of a toolkit passes?
5. **Card authoring.** Do we auto-generate cards and hand-tune only the confusable ones, or hand-write all of them for the seed toolkits?
6. **Name.** Is `arcade-tool-selector` fine?
