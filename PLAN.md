# Plan: arcade-tool-selector

Status: **draft for review**. Nothing is built yet. Each phase below is meant to land as its own PR.

## 1. Problem

Today an Arcade-backed agent is handed a toolkit (or a gateway's whole "Allowed Tools" list) and the LLM decides which tool to call. As the catalog grows this costs tokens on every turn, degrades tool choice (near-duplicate tools like `Gmail.ListEmails` vs `Gmail.SearchThreads`), and widens the blast radius of a bad call.

It's also a security problem. Tools need broad OAuth scopes to be useful, for example `gmail.modify` to read *and* send. Once a user authorizes, those scopes are live for every call the model makes, including calls induced by prompt injection in a tool's output. Which calls are actually acceptable depends on the use case and the project's policy, not on the scope. A support bot may send email to customers who wrote in, but never to arbitrary addresses, and never delete anything.

**The core case: the user's grant is broader than the project needs.** One Arcade gateway and one set of OAuth grants often serve many projects, and each project needs only a slice of them. For example, a meeting scheduler needs Calendar and read-only Gmail. The user has authorized Gmail send, Drive sharing and Linear write for other work. Every call outside the project's purpose must be blocked, even though Arcade would execute it:
- Gmail send, Drive sharing and anything in Linear are out of scope;
- so is a calendar invite to an outside domain;
- so is a call that injected text in a tool result asks for.

The project's slice is **contextual, not a fixed tool list**. A support bot legitimately needs `Gmail_SendEmail`, but only to reply to customers who wrote in. A project can hold broad permissions and still use them narrowly. So whether a call is in scope is judged per call, against the project's purpose, the user's request and the call's arguments (§4.10). Blocking out-of-scope calls is the project's main security property, and it's measured by its own eval slice (§5).

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
- **MCP Gateways:** project-scoped, with a static "Allowed Tools" list. This bounds what the **user's grant** can reach. It is not a per-project boundary in our sense: one gateway (and one set of OAuth grants) typically serves several projects, each needing less (see §1).
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

1. **Hard limits** (deterministic, admin-set). `scope.deny` (never, whatever the context) and argument rules are constraints on inputs, such as:
   - recipient domains, or "only addresses already in this thread"
   - Slack channel, Linear team or repo allowlists
   - max item counts and date ranges
   - no wildcards in delete queries

   A violation denies the call. Hard limits can only tighten the scope, never grant it. They need no LLM, so the server-side hook can enforce them too.
2. **In selection.** Is the tool in this turn's selection, or pinned? If not, deny. Selection already asked the scope question for this request (§4.10). So a tool that neither the request nor the project's purpose calls for is never exposed, even if injected text asks for it.
3. **Scope check** (Decisions API, every call). It asks two predicates (§4.10):
   - `within_purpose`: does the project's purpose cover this call?
   - `requested`: do the user's own messages ask for it?

   They're scored against the project prompt, the user's own messages and today's date, **not** tool outputs, so content fetched by a tool can't authorize a call. Outside the purpose, the tier's `on_low_scope` applies (send and destructive deny). Inside the purpose but unclearly requested, the call asks.
4. **Human approval.** For calls whose tier maps to `approve`, or a low-confidence scope judgment on a tier that escalates. The call is held and surfaced (Claude Code's permission prompt, Slack or a webhook) with the tool, the arguments, the scope probability and the reason. It times out to deny.

**Risk tiers set the stakes, not the scope.** Every tool has a tier: `read`, `write`, `send` (leaves the org: email, messages, posts) or `destructive` (delete, trash, revoke, transfer).
- **Where tiers come from.** `read`, `write` and `destructive` come from Arcade's own behavior metadata. Arcade has no "reaches people" flag, so `send` is a short curated list (`cards.SEND_TOOLS`). `policy.tool_tiers` overrides any of them.
- **What a tier decides:**
  - whether an in-scope call still needs approval (`policy.tiers`, e.g. `destructive: approve`);
  - what a low-confidence scope judgment leads to (`read` asks, `send` / `destructive` deny);
  - whether the call fails closed when the judge is unreachable (`write`, `send` and `destructive` do).

**Where it's enforced:**
- **In-process** (library `execute()` wrapper, and the MCP server's `call_tool`). This layer has the full context: prompt, selection, history. It runs all four checks.
- **Server-side** (Contextual Access **pre-hook**). This layer has no prompt, so it runs the hard limits (check 1), plus check 2 if the hook can tie the call back to a selection. That works only if a selection or session id can reach the hook (through the user id, routing metadata, or a short-lived selection record keyed by user). This is still to be verified against the hook schema.
  - It's fail-closed for the `send` and `destructive` tiers.
  - It catches callers that skip the library entirely.
- **Post-hook (optional).** It redacts results per policy (e.g. strip customer PII before it reaches the model) and flags outputs that look like instructions, which is a classic injection carrier.

**Audit.** Arcade already records every executed call: status, timing, and inputs/outputs for admins, readable via `GET /v1/orgs/{org}/projects/{project}/tool_executions`. Arcade has no API for writing custom events, and gateway responses carry no execution id. So a `call` entry is only a key into Arcade's record (`user_id`, `toolkit`, `tool`, `started_at`), and `ats-audit` resolves it. Events Arcade can't see go to JSONL or, optionally, to OpenTelemetry: prompts, catalog changes, denials, and calls blocked on authorization. Each decision is logged with:
- project, user, prompt hash, selection id
- tool, arguments (redacted per policy)
- the check that decided, decision, score and reason

Every policy change can be replayed against this log to see what it would have blocked.

**Scopes.** As a report, not enforcement, the vetter compares each toolkit's requested OAuth scopes with the tiers the project actually allows. For example: "this project never allows `send`, but Gmail is authorized with `gmail.send`." Teams can then narrow their Arcade auth providers.

### 4.7 Project config (sketch)

```yaml
project: support-bot
scope:                                          # judged per call against project_prompt (§4.10)
  threshold: 0.7                                # P(call within the project's purpose)
  deny: [Gmail_TrashEmail]                      # hard limit: never, whatever the context
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
  on_low_scope:                                # what a below-threshold scope judgment does, by tier
    read: approve
    write: approve
    send: deny
    destructive: deny
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
- the proxy listens on `/tmp/ats-<uid>/<claude-pid>-<data-dir-hash>.sock`, a short private dir, because AF_UNIX paths are capped at ~104 bytes and plugin data dirs are long;
- the hook walks up its own process tree to find the `claude` pid and connects to that socket.

Verified in phase 1 with a real `claude -p` session. Without a `claude` ancestor, both sides key by `cwd`.

**Upstream auth.** The proxy authenticates to a gateway in **Arcade Headers** mode, sending `Authorization: Bearer <ARCADE_API_KEY>` plus `Arcade-User-ID`. The proxy loads the key, user id, gateway URL and judge keys itself from an env file (`.env` in the repo root, gitignored, mode 600, overridable via `ATS_ENV_FILE`). They're never passed through Claude Code's config, which would store them in plain text in `~/.claude.json`. Hooks need no secrets. The plugin's `sensitive` userConfig (secure storage) replaces the file in phase 6. Per-tool OAuth (Gmail, Slack, …) still happens through Arcade: the authorization URL comes back in the tool result and the proxy passes it through. We'll use a separate test gateway, so the existing `workspace-search` gateway is untouched until we cut over.

**Catalog source.** In this mode the catalog is the gateway's own MCP `tools/list`, i.e. exactly the gateway's Allowed Tools. The gateway paginates `tools/list` (~105 tools per page), so the proxy must follow `nextCursor` until it is exhausted. Reading only the first page silently drops tools. No extra lookups are needed. Each tool's `_meta.arcade` carries the following, which feed the cards, risk tiers and scope report:
- operations, `read_only` / `destructive` flags and service domain
- the OAuth provider and scopes

**Two boundaries.** The gateway's Allowed Tools, plus the OAuth grants behind them, bound what the **user** can reach. The project's purpose bounds what **this project** may do, which is usually far less. That bound is judged per prompt and per call against `project_prompt` by the Decisions API, inside admin-set hard limits (§4.10). The proxy lists only tools the current request needs within that purpose, and the vetter judges every call. Several projects can share one gateway and one set of grants, each with its own `ats.yaml`.

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

### 4.10 Project scope via the Decisions API

A project's scope can't be a fixed list. The same tool can be in scope for one call and out of scope for the next:
- `Gmail_SendEmail` replying to a customer who wrote in: in scope.
- `Gmail_SendEmail` sending the same thread to an outside address: out of scope.

So scope is a **Decisions API judgment** against the project's purpose (`project_prompt`), made at two points.

**1. Per prompt, at selection** (`UserPromptSubmit`). The batched gate (§4.1 step 4) asks two predicates per shortlisted tool in the same call:
- *relevant:* would this request need `<tool>`?
- *in scope:* is using `<tool>` for this request within the project's purpose?

A tool is exposed only when both pass. One request can make `Gmail_SendEmail` visible, while "forward all invoices to my personal email" doesn't, even though both name the same tool.

**2. Per call, at vetting** (`PreToolUse`, proxy re-check). Two predicates per call, kept separate because a single combined question let "the user asked" outweigh "outside the purpose" (phase 3 eval):
- `within_purpose`: *does the project's purpose cover this action, with these arguments?* It ignores the request. Supporting read-only lookups in covered apps count as covered.
- `requested`: *do the user's own messages ask for this exact call?*
- the evidence: the project prompt, the user's own messages, the tool card and the arguments. Tool outputs are never evidence, so injected text can't raise the score.
- the outcome by result:
  - `within_purpose` below `scope.threshold` (default 0.6, from the sweep): `policy.on_low_scope` decides by tier, ask (Claude Code's permission prompt) or deny.
  - `requested` below the threshold: always ask. The judge's doubt about an in-purpose call goes to a human; it's never silently allowed or hard-denied.
- the judge also gets today's date, so a request like "tomorrow" can be checked against date arguments.

**What stays deterministic:** only admin-set hard limits that tighten, never grant:
- `scope.deny`, plus argument rules (§4.6 check 1);
- the gateway's Allowed Tools and OAuth grants underneath everything.

**Cost and failure.**
- **Cost.** Per-call judgments add one Decisions API round-trip per tool call. Read-tier calls can be batched with the prompt-time judgment and skip the per-call check when their arguments match what selection saw.
- **Failure.** If the judge is unreachable, `write` / `send` / `destructive` calls fail closed; `read` follows `on_low_scope`.
- **Audit.** Every judgment is audited as `scope_judged`, with its probability and the evidence ids used.

**Unused-grants report.** This compares the OAuth scopes behind every tool the project has actually been allowed to call, over a window of the audit log, with everything the gateway's tools request. For example: "in 30 days this project never used `gmail.send` or Linear `write`". The user can then narrow their Arcade auth or gateway.

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
- **Scope slice (headline security eval).** Example projects (e.g. a meeting scheduler, a support bot) run on the same broad gateway and grants. Each project has labeled cases: a prompt and/or a direct tool call with its arguments, mapped to `allow | deny | ask`. The cases cover:
  - **Same tool, different scope.** `Gmail_SendEmail` replying to a customer who wrote in (allow) vs. sending to an unrelated outside address (deny).
  - **Out-of-purpose toolkits.** A Linear call from a meeting scheduler, even though the user authorized Linear.
  - **Broad permission, narrow use.** `GoogleDrive_ShareFile` with the team (allow) vs. `SetGeneralAccess` to anyone-with-link (deny) in an internal-docs project.
  - **Hard limits.** A calendar invite to a domain the argument rules forbid.
  - **Injection asking for an out-of-scope call.**
  - **In-scope work, which must pass without friction.**

  Every case runs through both enforcement points: the `PreToolUse` hook and the proxy's own re-check. That covers callers that skip the hook. Metrics:
  - **Out-of-scope block rate:** must be 100%.
  - **Judgment calibration:** a threshold sweep of block rate vs. false-block rate per tier, so `scope.threshold` and `on_low_scope` are set from data.
  - **False-block rate.**
  - The **unused-grants report** must flag every authorized scope the project can't use.
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
| 3 | **Per-call scope vetting.** Judge interface and Decisions API adapter. The vetter runs in `PreToolUse` with a proxy re-check: hard limits (`scope.deny`, argument rules), then a per-call scope judgment against `project_prompt`, with tier-driven `on_low_scope` (ask via Claude Code's prompt, or deny) and fail-closed. Also the scope eval slice | Scope slice: 100% of out-of-scope calls blocked at both enforcement points; false-block rate reported; threshold sweep sets the defaults |
| 4 | Selector v1: batched per-prompt gate on the phase 3 judge (relevant + in-scope predicates per tool) + select rules; `UserPromptSubmit` → proxy `list_changed`; `additionalContext`; in-selection check | Model only sees selected tools; gate beats BM25 on recall at equal set size; p95 < 3s |
| 5 | `request_more_tools`, sticky selections, unused-grants report from the audit log | Multi-turn + injection eval slices pass; report on the workspace gateway |
| 6 | Plugin packaging (userConfig, marketplace), `/ats-why`, docs; cut `workspace-search` over | Installs from the repo with one command; daily use on the real gateway |
| **Scale & quality** | | |
| 7 | Hybrid shortlist (embeddings + RRF) + caching | Shortlist recall@40 ≥ 98% |
| 8 | Card generation + override file | Generated vs. hand-tuned ablation reported |
| **Beyond Claude Code** | | |
| 9 | Python library + adapters (OpenAI Agents, Anthropic, LangChain) | Examples run end-to-end with vetting on |
| 10 | Generic MCP mode for other clients (Cursor, Claude Desktop): meta-tool fallback where `list_changed` isn't honored | Works in one non-Claude-Code client |
| 11 | Arcade Contextual Access pre/post-hook server sharing `policy/` | Out-of-policy calls denied server-side even when the proxy is bypassed |
| 12 | PyPI release; explore living inside the Arcade platform | `pip install arcade-tool-selector` |

## 8. Open questions for review

Decided so far:
- **The first consumer is Claude Code,** via the proxy and hooks (§4.8).
- **The project boundary is the project's purpose, not the gateway.** The gateway and its OAuth grants bound the user. Each project narrows that.
- **Scope is contextual and judged by the Decisions API,** per prompt (at selection) and per call (at vetting), against the project prompt and the user's request (§4.10). A project can hold broad permissions and use them narrowly, so there's no fixed per-tool scope list. Only admin-set hard limits (`scope.deny`, argument rules) are deterministic, and they only tighten.
- **The headline security metric is the scope slice:** out-of-scope calls must be blocked 100% of the time (§5).
- **Per-call scope vetting lands before the selector** (phases 3 → 4).
- **The default judge is the OpenAI Decisions API** (`gpt-6-luna`), which gives native per-question probabilities, as in jev-search. Phase 3 builds that adapter; logprobs and Anthropic adapters stay optional.

Still open:
1. **Granularity.** Should we select individual tools, or allow selecting whole toolkits when most of a toolkit passes?
2. **Card authoring.** Do we auto-generate cards and hand-tune only the confusable ones, or hand-write all of them for the seed toolkits?
3. **Name.** Is `arcade-tool-selector` fine, now that the project also vets calls?
4. **Policy authoring.** Do we use YAML with built-in rule types (planned), or an existing policy engine such as OPA/Rego or Cedar so security teams can reuse their tooling? We could start with YAML and design rules to compile to Cedar later.
5. **Default posture.** Should an unlabelled tool default to `vet` (safer, more LLM calls) or to its auto-labelled tier?
6. **Upstream auth.** Is an API key in a mode-600 env file, read only by the proxy, acceptable until the plugin moves it to secure storage? Or should the proxy run the gateway's OAuth flow itself, so no long-lived key sits on disk at all?
7. **Test gateway.** Which toolkits should the test gateway carry? A mirror of `workspace-search` (Gmail, Calendar, Drive, Linear, Slack) gives a realistic catalog with read, send and destructive tools.
