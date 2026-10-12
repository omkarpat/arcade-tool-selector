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
| Catalog (gateway `tools/list`) | gateway URL hash; refreshed on startup + poll (§4.9) | JSON file in the data dir |
| Tool cards + tiers | card hash (schema + overrides) | local file / sqlite |
| Embeddings | card hash | same |
| Project scope | project config hash + catalog hash | same |
| Selections | project + normalized prompt (+ sticky set) | LRU, short TTL |

The stores are pluggable so a Redis backend can be added later.

### 4.5 Integration surfaces (in build order)

0. **Claude Code plugin (first target).** A local MCP proxy in front of an Arcade gateway, plus Claude Code hooks; see §4.8. We control both ends, nothing is needed inside Arcade, and it's dogfooded on a real gateway from day one. The selector and vetter are the same Python package that later backs the surfaces below.
1. **Python library.** Usage: `selector.select(prompt, user_id=None, history=None) -> Selection`. It includes helpers to emit OpenAI or Anthropic tool lists and to feed `get_arcade_tools(tools=...)`. It works with any framework, and the selector sees the prompt directly.
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
  chain: [openai-decisions:gpt-6-luna]        # default; other adapters optional
cards:
  overrides: cards/support-bot.yaml           # hand-tuned questions/criteria
```

### 4.8 Claude Code integration: proxy + hooks

**Today** an Arcade gateway is added to Claude Code as a remote HTTP MCP server (`"type": "http", "url": "https://api.arcade.dev/mcp/gw_…"`). Claude Code lists every gateway tool, its built-in tool search defers the schemas, and the model picks which tools to load and call.

**Proposed:** the gateway entry is replaced by a local stdio proxy that keeps the same server name, so tool names stay `mcp__<server>__<Tool>`. The proxy is the only thing that talks to Arcade.

```
                   ┌───────────────────────── Claude Code ──────────────────────────┐
 user prompt ─────►│ UserPromptSubmit hook ──(local socket)──┐                       │
                   │                                         ▼                       │
                   │ model ◄── tools/list (selected only) ── proxy (stdio MCP) ──────┼──► Arcade gateway
                   │   │                                     ▲  selector + vetter     │    (Streamable HTTP,
                   │   └─ tool call ─► PreToolUse hook ──────┘  catalog cache, audit  │     Arcade Headers auth)
                   └───────────────────────────────────────────────────────────────────┘
```

**Flow per turn:**
1. **`UserPromptSubmit` hook.** It receives `prompt`, `session_id`, `prompt_id` and `cwd`, sends `{prompt, session_id}` to the proxy, and waits for the selection. The hook stays thin; classification runs in the proxy, where the catalog, embeddings, cache and judge clients are warm.
   - Its `additionalContext` tells the model which tools are available for this request, and that `request_more_tools` exists.
   - The default timeout here is 30s, so the selector has a latency budget (target p95 < 3s). On timeout or error it falls back to the project's `defaults`, and never to "all tools".
2. **Proxy narrows the tool list.** It swaps its advertised `tools/list` to the selected tools and sends `notifications/tools/list_changed`. Claude Code supports this, and re-fetches the list in interactive sessions. The proxy sets `alwaysLoad` so the few selected schemas load in full, which means **our selector replaces Claude Code's tool search for this server**.
3. **Model calls a tool.** The `PreToolUse` hook (matcher `mcp__<server>__.*`) runs the vetter (§4.6) and returns:
   - `permissionDecision`: `allow`, `deny` (the reason goes back to the model) or `ask`
   - optionally `updatedInput`, e.g. to clamp `max_results`

   `ask` uses **Claude Code's own permission prompt as the human approval step**. The hook is set to `onFailure: "block"`, because by default a timed-out PreToolUse hook doesn't block.
4. **Proxy forwards the call** to the gateway. It re-checks the in-selection rule and the deterministic policy before forwarding, so a call that somehow skips the hook is still gated. Results pass back through the proxy, which applies post-call redaction and the injection-marker flag, then writes the audit log.
5. **`request_more_tools(need)`** is a proxy-native tool that is always listed. The model calls it when the current set falls short. It re-runs selection with `need` added to the request, and the proxy emits `list_changed` again. Calls to it are logged, and the vetter still applies to whatever gets added.

**Session correlation.** The hook knows Claude Code's `session_id`, but the proxy doesn't: MCP `initialize` doesn't carry it. Both are child processes of the same `claude` process, so:
- the proxy listens on `${CLAUDE_PLUGIN_DATA}/run/<claude-pid>.sock`;
- the hook walks up its own process tree to find the `claude` pid and connects to that socket.

This is a spike in phase 1. The fallback is to key by `cwd` and use the most recently initialized proxy.

**Upstream auth.** The proxy authenticates to a gateway in **Arcade Headers** mode, sending `Authorization: Bearer <ARCADE_API_KEY>` plus `Arcade-User-ID`. The proxy loads the key, user id, gateway URL and judge keys itself from an env file (`.env` in the repo root, gitignored, mode 600, overridable via `ATS_ENV_FILE`). They're never passed through Claude Code's config, which would store them in plain text in `~/.claude.json`. Hooks need no secrets. The plugin's `sensitive` userConfig (secure storage) replaces the file in phase 6. Per-tool OAuth (Gmail, Slack, …) still happens through Arcade: the authorization URL comes back in the tool result and the proxy passes it through. We'll use a separate test gateway, so the existing `workspace-search` gateway is untouched until we cut over.

**Catalog source.** In this mode the catalog is the gateway's own MCP `tools/list`, i.e. exactly the gateway's Allowed Tools. The gateway paginates `tools/list` (~105 tools per page), so the proxy must follow `nextCursor` until it is exhausted. Reading only the first page silently drops tools. It's enriched with `arcadepy` `tools.get` for toolkit, version and auth requirements, which feed the risk tiers and the scope report. So **the Arcade project and gateway is the project-level outer boundary**, and the YAML config only adds the project prompt, selection settings and policy.

**Example config.** The `workspace-search` cut-over is in [`examples/claude-code/`](examples/claude-code/):
- `mcp.before.json` → `mcp.json` (direct gateway → proxy)
- `settings.json` (hooks)
- `ats.yaml` (project config with policy, using real `workspace-search` tool and parameter names)

The README walks through the setup for any Claude Code instance.

**Packaging.** One Claude Code plugin in this repo, which also serves as a marketplace:

```
.claude-plugin/plugin.json   # userConfig: gateway_url, arcade_api_key (sensitive), arcade_user_id, project_config
.mcp.json                    # stdio proxy: uv run --project ${CLAUDE_PLUGIN_ROOT} ats-proxy
hooks/hooks.json             # UserPromptSubmit + PreToolUse (+ PostToolUse for audit)
skills/                      # /ats-why (explain the last selection/decision), /ats-policy, /ats-refresh
```

It's installed with `/plugin marketplace add omkarpat/arcade-tool-selector` followed by `/plugin install arcade-tool-selector`. State (cache, audit log, sockets) lives in `${CLAUDE_PLUGIN_DATA}`, which survives updates. Plugin MCP tools are named `mcp__plugin_arcade-tool-selector_<server>__<Tool>`, and the hook matcher uses that form.

**Limits to know:**
- Subagents get their prompt from the parent model, not through `UserPromptSubmit`. They inherit the session's current selection, and `request_more_tools` covers the gaps.
- `claude -p` and the Agent SDK only refresh the tool list on `list_changed`; they don't re-prompt. Gating via `PreToolUse` and the proxy still holds there.
- The hooks govern this server only. Other MCP servers and built-in tools are untouched.

### 4.9 Catalog cache and refresh

The catalog (all pages of the gateway's `tools/list`) is cached on disk. That makes Claude Code startup fast and keeps a gateway outage from blanking the tool list. A poll keeps the cache current.

**On startup.** Claude Code spawns the proxy when the session starts.
1. The proxy loads `<data_dir>/catalog/<gateway-hash>.json` and answers `initialize` and `tools/list` from it immediately.
2. It then refreshes from the gateway in the background (stale-while-revalidate).
3. If there's no cache yet (first run), it fetches synchronously before answering.
4. If that fetch also fails, it fails closed: it advertises no gateway tools, and `request_more_tools` reports why.

**Poll.** A background task in the proxy re-fetches every `catalog.refresh_interval` (default 15 min, with ±10% jitter).
- It follows `nextCursor` to the last page.
- It hashes the normalized schemas, so an unchanged catalog is a no-op.
- It writes atomically (temp file + rename), so readers never see a half-written file.

**Many sessions, one poll.** Every open Claude Code session runs its own proxy, but the cache file is shared per gateway, so they don't all poll.
- A lease file (`<gateway-hash>.lock` with an expiry) elects one poller at a time.
- The others watch the cache file's mtime and reload when it changes.
- If the leaseholder exits, the lease expires and another proxy takes over.

**On change.** The proxy diffs the old and new catalogs into added, removed and changed tools, and only those are re-processed.
- **Added tools** get cards, tiers and embeddings generated. They start in the `vet` tier until labelled, so a tool newly added to the gateway is never silently `allow`ed. They become selectable from the next prompt.
- **Removed tools** drop out of the current selection.
- **Changed schemas** have their card regenerated. Hand-tuned overrides are kept, and flagged for review if a parameter that a rule names disappears.
- If the current selection was affected, the proxy emits `notifications/tools/list_changed` to Claude Code.
- An audit entry `catalog_changed` records the diff.

**Other refresh triggers:**
- **Upstream push:** if the gateway sends `notifications/tools/list_changed` itself, the proxy refreshes immediately and the poll is just a fallback. Whether Arcade's gateway emits this is unverified; we'll check in phase 1.
- **Unknown tool:** a call for a tool the cache doesn't know triggers a forced refresh before the call is denied.
- **Manual:** `/ats-refresh` (skill) forces a refresh and prints the diff.

```yaml
catalog:
  refresh_interval: 15m      # 0 disables polling (startup refresh only)
  max_staleness: 24h         # older cache + unreachable gateway → warn in additionalContext
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
├─ .claude-plugin/      # plugin.json + marketplace.json (Claude Code plugin, §4.8)
├─ .mcp.json            # stdio proxy entry
├─ hooks/hooks.json     # Claude Code UserPromptSubmit / PreToolUse / PostToolUse
├─ skills/              # /ats-why, /ats-policy
├─ src/arcade_tool_selector/
│  ├─ catalog.py        # gateway tools/list + arcadepy enrichment → ToolCard
│  ├─ cards.py          # gate question generation + overrides
│  ├─ project.py        # config load, project scope
│  ├─ retrieve.py       # BM25 + embeddings + RRF shortlist
│  ├─ judge/            # base.py, openai_decisions.py, openai_logprobs.py, anthropic.py
│  ├─ select.py         # gate → threshold/pins/defaults/k → Selection
│  ├─ policy/           # tiers.py, args.py (rules), intent.py, vetter.py
│  ├─ audit.py
│  ├─ cache.py
│  ├─ proxy/            # stdio MCP server → Arcade gateway client, list_changed, local socket
│  ├─ claude_code/      # hook entrypoints (thin: talk to the proxy socket)
│  └─ adapters/         # later: openai/anthropic tool lists, openai-agents, langchain
├─ arcade_hooks/        # later: Contextual Access pre/post-hook server (shares policy/)
├─ evals/               # datasets/, run.py, recorded/
└─ tests/
```

The stack is Python 3.11+ and `arcadepy`, with `uv` for packaging and an MIT license. The only abstraction is the judge layer. Each provider's official SDK sits behind it, so there's no hand-rolled REST.

## 7. Phases

| # | Deliverable | Done when |
|---|---|---|
| 0 | This plan, README, license | Plan reviewed |
| **Claude Code** | | |
| 1 | Pass-through proxy: stdio MCP → test Arcade gateway (Arcade Headers auth), forwards `tools/list`/`tools/call`, audit log; catalog cache with startup load, background poll, shared lease and diff (§4.9); socket-correlation spike with a no-op `UserPromptSubmit` hook | Swapped in for a gateway entry, Claude Code behaves exactly as with the direct connection; warm start serves all 166 tools from cache; a tool added on the gateway appears within one poll; hook reaches the right proxy |
| 2 | Catalog + `ToolCard` + risk tiers (auto-label + overrides) + eval dataset v0 + harness with "all tools" and BM25-only baselines | `evals/run.py` reports recall/precision/size on the test gateway's catalog |
| 3 | Deterministic vetter in `PreToolUse` + proxy re-check: tiers, argument rules, `ask` via Claude Code's prompt | Security slice: 100% of tier/argument violations blocked; false-block rate reported |
| 4 | Selector v1: judge interface + one adapter + batched gate + select rules; `UserPromptSubmit` → proxy `list_changed`; `additionalContext`; in-selection check | Model only sees selected tools; gate beats BM25 on recall at equal set size; p95 < 3s |
| 5 | `request_more_tools`, sticky selections, intent check (LLM) for `vet`-tier calls | Multi-turn + injection eval slices pass |
| 6 | Plugin packaging (userConfig, marketplace), `/ats-why`, docs; cut `workspace-search` over | Installs from the repo with one command; daily use on the real gateway |
| **Scale & quality** | | |
| 7 | Hybrid shortlist (embeddings + RRF) + caching | Shortlist recall@40 ≥ 98% |
| 8 | Card generation + override file; project-prompt toolkit gate | Generated vs. hand-tuned ablation reported |
| **Beyond Claude Code** | | |
| 9 | Python library + adapters (OpenAI Agents, Anthropic, LangChain) | Examples run end-to-end with vetting on |
| 10 | Generic MCP mode for other clients (Cursor, Claude Desktop): meta-tool fallback where `list_changed` isn't honored | Works in one non-Claude-Code client |
| 11 | Arcade Contextual Access pre/post-hook server sharing `policy/` | Out-of-policy calls denied server-side even when the proxy is bypassed |
| 12 | Scope report, PyPI release; explore living inside the Arcade platform | `pip install arcade-tool-selector` |

## 8. Open questions for review

Decided so far:
- **The first consumer is Claude Code,** via the proxy and hooks (§4.8).
- **The project boundary is the Arcade gateway.** The gateway's Allowed Tools list is the outer scope, and the YAML adds the project prompt, selection settings and policy.
- **Deterministic vetting lands before the LLM selector** (phases 3 → 4).
- **The default judge is the OpenAI Decisions API** (`gpt-6-luna`), which gives native per-question probabilities, as in jev-search. Phase 4 builds that adapter first; logprobs and Anthropic adapters stay optional.

Still open:
1. **Granularity.** Should we select individual tools, or allow selecting whole toolkits when most of a toolkit passes?
2. **Card authoring.** Do we auto-generate cards and hand-tune only the confusable ones, or hand-write all of them for the seed toolkits?
3. **Name.** Is `arcade-tool-selector` fine, now that the project also vets calls?
4. **Policy authoring.** Do we use YAML with built-in rule types (planned), or an existing policy engine such as OPA/Rego or Cedar so security teams can reuse their tooling? We could start with YAML and design rules to compile to Cedar later.
5. **Default posture.** Should an unlabelled tool default to `vet` (safer, more LLM calls) or to its auto-labelled tier?
6. **Upstream auth.** Is an API key in a mode-600 env file, read only by the proxy, acceptable until the plugin moves it to secure storage? Or should the proxy run the gateway's OAuth flow itself, so no long-lived key sits on disk at all?
7. **Test gateway.** Which toolkits should the test gateway carry? A mirror of `workspace-search` (Gmail, Calendar, Drive, Linear, Slack) gives a realistic catalog with read, send and destructive tools.
