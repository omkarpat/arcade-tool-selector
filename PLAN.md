# Plan: arcade-tool-selector

Status: **draft for review**. Nothing is built yet. Each phase below is meant to land as its own PR.

## 1. Problem

Today an Arcade-backed agent is handed a toolkit (or a gateway's whole "Allowed Tools" list) and the LLM decides which tool to call. As the catalog grows this costs tokens on every turn, degrades tool choice (near-duplicate tools like `Gmail.ListEmails` vs `Gmail.SearchThreads`), and widens the blast radius of a bad call.

It's also a security problem. Tools need broad OAuth scopes to be useful, for example `gmail.modify` to read *and* send. Once a user authorizes, those scopes are live for every call the model makes, including calls induced by prompt injection in a tool's output. Which calls are actually acceptable depends on the use case and the project's policy, not on the scope. A support bot may send email to customers who wrote in, but never to arbitrary addresses, and never delete anything.

Goals:
1. **Select:** given the project's configuration and the user's prompt, return the small set of Arcade tools the LLM may see for that turn. The LLM still picks among them and fills the arguments, and never sees the rest.
2. **Vet:** check every tool call against project policy before it executes. A call is allowed, denied, or escalated depending on:
   - the tool's risk
   - its arguments
   - whether the call matches what the user actually asked for

   Selection narrows what the model can attempt. Vetting decides what actually runs.

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
- **Contextual Access hooks** are webhooks we host, configured at org or project level, with a priority order, fail-open or fail-closed, and a 5s default timeout.
  - **Access hook:** filters toolkits per user.
  - **Pre hook:** sees each call before it runs (tool, inputs, user). It can allow, deny, or rewrite the inputs or routing.
  - **Post hook:** can deny or redact a result.
  - Hooks **never see the prompt**. That makes them wrong for selection, but the right server-side place to enforce call policy, because they also cover callers that bypass our library.
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
3. **Contextual Access pre-hook.** The server-side half of call vetting (§4.6). It enforces tiers and argument rules on every `tools.execute` in the project, whoever the caller is.
4. **TypeScript port.** Later, only if there's demand, built on `toZodToolSet`.

### 4.6 Call vetting (project policy)

Selection is about relevance and vetting is about permission. Selection alone isn't a security boundary: a selected `Gmail.SendEmail` can still be pointed at the wrong recipient. So every call passes a **vetter** before `tools.execute`:

```
LLM emits call ──► Vetter ──► allow ─────────────► tools.execute
                     │   ├──► deny (+reason back to the LLM)
                     │   └──► escalate ──► human approval ──► execute / deny
                     ▼
                 audit log
```

The vetter runs these checks in order, cheapest first. The first decisive result wins.

1. **In selection.** Is the tool in this turn's selection, or pinned? If not, deny. This is what turns selection into a security boundary: a tool the user's request didn't need can't run, even if injected text asks for it.
2. **Static policy.** Every tool has a risk tier: `read`, `write`, `send` (leaves the org: email, messages, posts) or `destructive` (delete, trash, revoke, transfer). Tiers are auto-labelled from the name and description, and the project config can override them. Project policy then maps each tier, or a specific tool, to `allow`, `vet`, `approve` or `deny`.
3. **Argument rules.** These are declarative constraints on inputs, such as:
   - recipient domains, or "only addresses already in this thread"
   - Slack channel or Linear team allowlists
   - repo allowlists
   - max item counts and date ranges
   - no wildcards in delete queries
   - regex or allow/deny lists per parameter

   They're deterministic with no LLM, and they're checked by both the library and the hook.
4. **Intent check (LLM).** For `vet`-tier calls, a jev-style yes/no question: *"Is calling `Gmail.SendEmail` with these arguments something the user asked for, within this project's purpose?"* It's scored against the user's own messages and the project prompt, **not** tool outputs. That's the injection defense: content fetched by a tool can't authorize a call. If `P(yes)` is below the threshold, the call escalates or is denied, per policy.
5. **Human approval.** For the `approve` tier, or a failed intent check, the call is held and surfaced through a callback (CLI prompt, Slack, webhook) with the tool, the arguments and the reason. It times out to deny.

**Where it's enforced:**
- **In-process** (library `execute()` wrapper, and the MCP server's `call_tool`). This layer has the full context: prompt, selection, history. It runs all five checks.
- **Server-side** (Contextual Access **pre-hook**). This layer has no prompt, so it runs checks 2–3, plus check 1 if the hook can tie the call back to a selection. That works only if a selection or session id can reach the hook (through the user id, routing metadata, or a short-lived selection record keyed by user). This is still to be verified against the hook schema.
  - It's fail-closed for the `send` and `destructive` tiers.
  - It catches callers that skip the library entirely.
- **Post-hook (optional).** It redacts results per policy (e.g. strip customer PII before it reaches the model) and flags outputs that look like instructions, which is a classic injection carrier.

**Audit.** Each decision is logged with:
- project, user, prompt hash, selection id
- tool, arguments (redacted per policy)
- the check that decided, decision, score and reason

Every policy change can be replayed against this log to see what it would have blocked.

**Scopes.** As a report, not enforcement, the vetter compares each toolkit's requested OAuth scopes with the tiers the project actually allows. For example: "this project never allows `send`, but Gmail is authorized with `gmail.send`." Teams can then narrow their Arcade auth providers.

### 4.7 Project config (sketch)

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
policy:
  tiers:                                        # default action per risk tier
    read: allow
    write: vet
    send: vet
    destructive: deny
  tools:                                        # per-tool overrides
    Linear.CreateIssue: allow
    Zendesk.CloseTicket: approve
  args:
    Gmail.SendEmail:
      recipient: { in_thread_participants: true }   # reply only to people who wrote in
      cc: { domains: [getsphere.com] }
    Slack.SendMessage:
      channel_name: { allow: ["#support", "#support-escalations"] }
    Linear.CreateIssue:
      team_id: { allow: [SUP] }
  intent_threshold: 0.7
  on_intent_fail: approve                       # or deny
  approval:
    via: slack                                  # cli | slack | webhook
    timeout_s: 300                              # then deny
  redact_results: [ssn, card_number]
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
- **Security slice.** Labeled `(prompt, conversation, proposed call) → allow | deny | escalate`, including:
  - **Injection scenarios:** an email or issue body that tells the agent to forward, delete or exfiltrate.
  - **Argument violations:** wrong recipient domain, a channel outside the allowlist, a bulk delete.
  - **Legitimate high-risk calls,** which should pass.

  Metrics:
  - **Unsafe-call block rate** is the headline. Target 100% on deterministic checks.
  - **False-block rate,** i.e. friction on legitimate work.
  - **Escalation rate.**
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
│  ├─ policy/           # tiers.py, args.py (rules), intent.py, approval.py, vetter.py
│  ├─ audit.py
│  ├─ cache.py
│  └─ adapters/         # openai/anthropic tool lists, openai-agents, langchain
├─ mcp_server/          # arcade-mcp meta-tool + dynamic modes
├─ hooks/               # Contextual Access pre/post-hook server (shares policy/)
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
| 6 | Vetter, deterministic: in-selection check, risk tiers (auto-label + overrides), argument rules, audit log, `execute()` wrapper | Security slice: 100% of argument/tier violations blocked, false-block rate reported |
| 7 | Vetter, judgment: intent check + human approval callback (CLI first, Slack next) | Injection scenarios blocked or escalated; legitimate high-risk calls pass |
| 8 | Contextual Access pre/post-hook server sharing `policy/` | Out-of-policy calls denied server-side in a demo project, even when the library is bypassed |
| 9 | Library adapters + examples (OpenAI Agents, Anthropic, LangChain) | Examples run end-to-end against Arcade, with vetting on |
| 10 | MCP server, meta-tool mode (`call_tool` goes through the vetter) | Works in Claude Desktop / Cursor through an Arcade gateway |
| 11 | MCP dynamic mode + multi-turn sticky + `request_more_tools` | Multi-turn eval slice passes |
| 12 | Scope report, packaging, docs, PyPI release | `pip install arcade-tool-selector` |

## 8. Open questions for review

1. **"Project level."** Is the project scope meant to be config in this repo (YAML per project), or tied to an **Arcade project / MCP gateway** (read the gateway's Allowed Tools as the allowlist)? The docs show no public API to read gateway config. If that holds, YAML mirrors the gateway by hand.
2. **Primary consumer.** Is the first target your own agent code (library-first, as planned) or third-party MCP clients (MCP server first)?
3. **Default judge.** Is it the OpenAI Decisions API (closest to jev-search, native probabilities) or Anthropic (stated confidence, needs calibration)?
4. **Granularity.** Should we select individual tools, or allow selecting whole toolkits when most of a toolkit passes?
5. **Card authoring.** Do we auto-generate cards and hand-tune only the confusable ones, or hand-write all of them for the seed toolkits?
6. **Name.** Is `arcade-tool-selector` fine, now that the project also vets calls?
7. **Policy authoring.** Do we use YAML with built-in rule types (planned), or an existing policy engine such as OPA/Rego or Cedar so security teams can reuse their tooling? We could start with YAML and design rules to compile to Cedar later.
8. **Default posture.** Should an unlabelled tool default to `vet` (safer, more LLM calls) or to its auto-labelled tier?
9. **Hook correlation.** Must the server-side hook enforce "only selected tools" (which needs a selection id to reach the hook), or is tier + argument policy enough server-side, with the selection check living in-process?
10. **Priority.** Should vetting move ahead of selection quality (phases 2–5)? It's the more important property for a community security story, and phase 6 only needs the catalog and tiers.
