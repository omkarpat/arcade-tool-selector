# Plan: arcade-tool-selector

**Status:** phases 1–3 are built: the proxy, the catalog and per-call scope vetting, with evals. Phase 4, prompt-time tool selection, is next. Each phase lands as its own PR. Sections marked *(planned)* aren't built yet.

## 1. Problem

An Arcade-backed agent is handed a gateway's whole "Allowed Tools" list, and the LLM decides which tool to call. As the catalog grows, this has three costs:
- tokens on every turn;
- worse tool choice between near-duplicates (`Gmail_ListEmails` vs `Gmail_SearchThreads`);
- a wider blast radius for a bad call.

It's also a security problem. Tools need broad OAuth scopes to be useful, such as `gmail.modify` to read *and* send. Once a user authorizes, those scopes are live for every call the model makes, including calls induced by prompt injection in a tool's output.

**The core case: the user's grant is broader than the project needs.** One set of OAuth grants often serves many projects, and each needs only a slice. A meeting scheduler needs Calendar and read-only Gmail, while the user has also authorized Gmail send, Drive sharing and Linear write for other work. Arcade would execute any of those, but each of these must be blocked:
- Gmail send, Drive sharing and anything in Linear;
- a calendar invite to an outside domain;
- a call that injected text in a tool result asks for.

**The project's slice is contextual, not a fixed tool list.** A support bot legitimately needs to send email, but only to reply to customers who wrote in. So whether a call is in scope is judged per call, against the project's purpose, the user's request and the call's arguments (§4.3). Blocking out-of-scope calls is the main security property, and it has its own eval (§5).

**Goals:**
1. **Vet** *(built)*: check every tool call against the project's purpose before it executes. Allow, deny, or ask the user.
2. **Select** *(planned)*: given the project and the user's prompt, return the small set of tools the LLM may see for that turn. The LLM still picks among them and fills the arguments.

Selection narrows what the model can attempt. Vetting decides what actually runs.

**Non-goals:** replacing Arcade auth or execution, a hosted service, generating tool arguments.

## 2. Prior art: jev-search

[superagents-lab/jev-search](https://github.com/superagents-lab/jev-search) (MIT) picks which of 12 search sources to query for a request. What we take from it:

| jev-search | Here |
|---|---|
| Each source carries a yes/no question | Each tool carries a **tool card**; the vetter asks yes/no questions about each call |
| The model returns a **probability per question**, never free text | The Decisions API answers typed predicates with probabilities |
| Keep `p ≥ threshold`, else fall back to defaults | Thresholds per question; selection falls back to project defaults |
| Code proposes candidates, the model only scores them | The model can't invent a tool name |

What we add: a retrieval stage before the gate (an Arcade catalog has hundreds of tools, not 12), and labelled evals from day one.

## 3. What Arcade gives us

- **MCP gateways:** project-scoped, with a static Allowed Tools list, Arcade Headers or Arcade Auth, and **instructions** ("Guide your agent"). The proxy uses Arcade Headers. The instructions hold the project's purpose.
- **Tool metadata:** each tool's `_meta.arcade` carries `read_only` / `destructive` flags, operations and OAuth scopes. That's where risk tiers come from.
- **Project-scoped API keys and OAuth grants:** another project's key gets a 403, and each project is authorized separately.
- **Tool execution history:** `GET /v1/orgs/{org}/projects/{project}/tool_executions`. There's no API to write custom events, and gateway results carry no execution id.
- **Contextual Access hooks:** org- or project-level webhooks (access, pre and post) with priority, fail-open/closed and a 5s timeout. A pre-hook sees the user, the tool and its inputs, **never the request or the project's purpose**, so it can't judge scope. Closing that gap is the feature proposed in [WRITEUP.md](WRITEUP.md).
- **Nothing built-in does prompt-based selection.**

## 4. Design

### 4.1 Claude Code integration: proxy + hooks *(built)*

The gateway sits behind a local stdio MCP proxy. The proxy is the only thing that talks to Arcade.

```
                   ┌──────────────────────── Claude Code ─────────────────────────┐
 user prompt ─────►│ UserPromptSubmit hook ──(local socket)──┐                     │
                   │                                         ▼                     │
                   │ model ◄── tools/list ──────────── proxy (stdio MCP) ──────────┼──► Arcade gateway
                   │   │                                     ▲  vetter, catalog     │    (Streamable HTTP,
                   │   └─ tool call ─► PreToolUse hook ──────┘  cache, audit        │     Arcade Headers auth)
                   └─────────────────────────────────────────────────────────────────┘
```

**Per turn:**
1. **`UserPromptSubmit`** sends the prompt to the proxy. That prompt is the evidence calls are judged against. If the proxy can't be reached, or no role is set, the prompt is blocked.
2. **`PreToolUse`** (matcher `mcp__arcade__.*`) asks the proxy to vet the call. **Deny** and **ask** map to Claude Code's own decisions, so ask becomes Claude Code's permission prompt, with the reason. **Allow** prints nothing, so Claude Code's own permission rules still apply: the vetter only tightens. With `onFailure: block`, a hook failure blocks the call.
3. **The proxy re-checks every call** before forwarding it, so a call that skips the hook is still vetted.

**Session correlation.** MCP `initialize` doesn't carry Claude Code's session id, but the hook and the proxy are children of the same `claude` process. So:
- the proxy listens on `/tmp/ats-<uid>/<claude-pid>-<data-dir-hash>.sock`, a short path because AF_UNIX paths cap at ~104 bytes;
- the hook walks up its process tree to that pid.

**The repo is the Claude Code project.** `.mcp.json` registers the proxy as `arcade`, for the role in `ATS_ROLE`. `.claude/settings.json`:
- adds the hooks;
- turns off claude.ai connectors, so the gateway is the only route to the user's apps;
- starts in the default permission mode.

Setup is clone, `.env`, `uv sync`, then `ATS_ROLE=<role> claude`. To put another project's existing gateway behind the proxy, use [`examples/claude-code/`](examples/claude-code/).

**Upstream auth.** The proxy sends `Authorization: Bearer <role api_key>` plus `Arcade-User-ID`. Keys come from the repo's `.env` (gitignored, mode 600), read only by the proxy. Claude Code's config would store them in plain text in `~/.claude.json`, so it only ever sets `ATS_ROLE`. Per-tool OAuth still happens through Arcade: the authorization URL comes back in the tool result.

**Limits:**
- The hooks govern this server only. Claude Code's built-in tools keep their usual permission prompts.
- Subagents get their prompts from the parent model, not through `UserPromptSubmit`.
- The vetter guards its own gateway. A user who adds another route to the same apps (a separate MCP server) bypasses it. That's why the check belongs inside Arcade.

### 4.2 Configuration *(built)*

Every setting has one home, with no overrides and no fallbacks. Anything missing fails loudly.
- **Arcade platform:** each role is an Arcade project. Its gateway's instructions are the purpose, and its allow-list, API key and OAuth grants bound what it can reach.
- **`.env`:**
  - `ARCADE_USER_ID`, whose domain is the organization;
  - `ARCADE_ORG_ID`;
  - `OPENAI_API_KEY`;
  - `ATS_ROLES`, one `{project_id, api_key, gateway_url}` per role.

  `ATS_ROLE`, set at launch, picks the role.
- **Code:** the policy itself, set from the scope eval:
  - scope threshold 0.65;
  - disclosure threshold 0.5;
  - destructive calls ask;
  - out-of-scope send and destructive calls are denied.
- **Eval datasets** carry their own project definitions. They're the only local configuration.

### 4.3 Call vetting *(built)*

```
model emits call ──► Vetter ──► allow ───────────────► gateway
                       │   ├──► deny (reason back to the model)
                       │   └──► ask ──► Claude Code's permission prompt
                       ▼
                   audit log
```

**The description is the policy.** There are no tool lists or argument rules in the vetter. Tool-level access is Arcade's job: gateway allow-lists and project-scoped keys.

**One Decisions API request per call** (`gpt-6-luna`), with up to four predicates:
- **`within_purpose`:** does the project's purpose cover this action, with these arguments? Read-only supporting lookups in covered apps count as covered.
- **`requested`:**
  - For writes: do the user's own messages ask for this exact call?
  - For reads: is it a reasonable step toward what the user asked?
- **`reaches_people`** (writes only): does it deliver something directly to other people (email, chat message, calendar invite, file share)? If so, the call carries **send** stakes.
- **`discloses_internal`** (writes only): would it reveal internal details to someone outside the organization? If so, **deny**.

**The evidence:**
- the purpose, read from the gateway at startup and every 15 minutes, and never forwarded to the agent;
- the user's own messages;
- today's date;
- the tool card and the arguments.

**Tool outputs are never evidence,** so injected text can't raise a score.

**Risk tiers set the stakes, not the scope.** `read` and `destructive` come from Arcade's flags. Everything else is `write`, which becomes `send` when the judge says it reaches people.

| Result | Outcome |
|---|---|
| `within_purpose` below the threshold | read and write **ask**; send and destructive **deny** |
| In purpose, `requested` below the threshold | **ask**: a human settles the judge's doubt |
| In scope, destructive | **ask** |
| In scope | allow (Claude Code's own rules apply) |
| Judge unavailable, or the gateway has no instructions | **deny** every call |

**Audit.** Arcade already records executions, so the proxy's `call` event is only a key (`user_id`, `toolkit`, `tool`, `started_at`), which `ats-audit` resolves against Arcade's execution history. The proxy logs what Arcade can't see:
- `vetted`, with every answer;
- `prompt_seen`, with the length only;
- `call_needs_authorization`;
- `catalog_changed`.

Events go to `~/.local/share/ats/<role>/audit.jsonl`, or to OpenTelemetry.

### 4.4 Catalog cache and refresh *(built)*

The catalog is every page of the gateway's `tools/list`. The gateway paginates at ~105 tools, and reading only page one silently drops tools. The catalog is cached on disk per gateway, so startup is fast and a gateway outage doesn't blank the tool list.

- **Startup:** serve from the cache, then refresh in the background. On the first run, fetch before answering.
- **Poll:** every 15 minutes (±10% jitter). Unchanged catalogs are a no-op, and writes are atomic.
- **Many sessions, one poll:** a lease file elects one poller per gateway. The other proxies watch the cache's mtime and reload when it changes.
- **On change:** the proxy diffs added, removed and changed tools, emits `notifications/tools/list_changed` to Claude Code, and audits `catalog_changed`.
- **Other triggers:**
  - an upstream `list_changed`;
  - a call to an unknown tool, which forces a refresh before the call is denied.

### 4.5 Prompt-time selection *(planned, phase 4)*

```
gateway catalog ──► tool cards ──► Shortlist ──► Gate ──► Selection ──► proxy tools/list + list_changed
                                   (per prompt,  (per prompt,  names + scores
                                    no LLM)       Decisions API)
```

1. **Shortlist** (no LLM): BM25 over names, descriptions and parameters, later fused with embeddings. Take the top 40, or the whole pool if it's smaller.
2. **Gate:** one batched Decisions API request asking two predicates per shortlisted tool:
   - *relevant:* would this request need the tool?
   - *in scope:* is using it for this request within the purpose?

   A tool is exposed only when both pass. One prompt can make `Gmail_SendEmail` visible ("reply to the customer") while another doesn't ("forward all invoices to my personal email").
3. **Select:** keep `p ≥ 0.6`, sorted by score and capped at `k` (default 8). Pins always go in. If nothing passes, use the project defaults, never "all tools".
4. **Hand-off:** the proxy swaps its advertised `tools/list` to the selection and sends `list_changed`. The `UserPromptSubmit` hook's `additionalContext` names the available tools.
5. **Multi-turn:** tools from recent turns get a sticky bonus. An always-listed `request_more_tools(need)` re-runs selection when the set falls short; whatever it adds is still vetted.

Selection adds an in-selection check to vetting. A call to a tool outside the current selection is denied, even if injected text asks for it. The latency budget is p95 < 3s, inside the hook's timeout.

### 4.6 Integration surfaces

| Surface | Status |
|---|---|
| Claude Code: proxy + hooks | built |
| Claude Code plugin: marketplace install, secure key storage, `/ats-why` | planned (phase 6) |
| Python library: `select()`, `vet()`, framework adapters | planned |
| Generic MCP server for other clients (Cursor, Claude Desktop): meta-tool mode where `list_changed` isn't honored | planned |
| Contextual Access pre/post-hook server sharing `policy.py` | planned; needs the request and the purpose to reach hooks |

## 5. Evaluation

**Built:**
- **Scope** (`evals/scope.py`, 65 cases, 5 projects on one broad gateway). Each case is a project, the user's messages and a proposed call, labelled allow / ask / deny. The cases cover:
  - the same tool in and out of scope;
  - out-of-purpose toolkits;
  - broad permissions used narrowly;
  - rules stated only in the description;
  - injection;
  - supporting lookups;
  - role limits;
  - internal-detail disclosure.

  The headline is the out-of-scope block rate, which must be 100%. Friction (in-scope calls held) is the second metric. A threshold sweep sets the threshold. Today, at 0.65: 100% blocked, 7% held, 95% exact.
- **Selection baselines** (`evals/run.py`, 103 labelled prompts): recall, solved, precision, set size and schema tokens for "all tools" and BM25. Phase 4's gate has to beat these.
- **Live tests** (`ATS_LIVE=1`): the real gateway, judge, hook and proxy re-check, end to end.

**Planned:**
- labelling real decisions from the audit log;
- per-project threshold calibration;
- recorded judge responses, so CI is deterministic and free;
- a multi-turn and injection slice for selection;
- an **unused-grants report**: OAuth scopes a project never used over a window of the audit log, so teams can narrow their Arcade auth.

## 6. Repo layout

```
arcade-tool-selector/
├─ .mcp.json, .claude/      # the repo as a Claude Code project (proxy server, hooks)
├─ src/arcade_tool_selector/
│  ├─ settings.py           # .env and roles
│  ├─ platform.py           # the purpose, from the gateway's instructions
│  ├─ catalog.py            # shared catalog cache, lease, diff
│  ├─ cards.py              # tool cards and risk tiers from _meta.arcade
│  ├─ judge.py              # OpenAI Decisions API
│  ├─ policy.py             # the vetter
│  ├─ audit.py, executions.py
│  ├─ retrieve.py           # BM25 baseline (selection)
│  ├─ proxy/                # stdio MCP server, gateway client, hook socket, re-check
│  ├─ claude_code/          # hook entrypoints
│  └─ demo.py, doctor.py
├─ evals/                   # scope + selection evals, datasets
├─ examples/                # role purposes, configs for other projects
└─ tests/
```

Python 3.11+, `uv`, MIT. The only abstraction is the judge interface, and each vendor's official SDK sits behind it.

## 7. Phases

| # | Deliverable | Status |
|---|---|---|
| 1 | Pass-through proxy, catalog cache (poll, shared lease, diff), hook ↔ proxy socket, audit | done |
| 2 | Tool cards, risk tiers, selection eval with "all tools" and BM25 baselines | done |
| 3 | Per-call scope vetting with the Decisions API, at the hook and in the proxy, plus the scope eval | done: 100% blocked |
| 4 | Selector: batched gate, select rules, `list_changed`, in-selection check | next. Done when it beats BM25 on recall at equal set size, p95 < 3s |
| 5 | `request_more_tools`, sticky selections, unused-grants report | |
| 6 | Claude Code plugin packaging | |
| 7 | Hybrid shortlist (embeddings + RRF) | shortlist recall@40 ≥ 98% |
| 8 | Python library and adapters; generic MCP mode for other clients | |
| 9 | Contextual Access hook server sharing `policy.py` | |

## 8. Decisions and open questions

**Decided:**
- The first consumer is Claude Code, via the proxy and hooks.
- The project boundary is the project's purpose, not the gateway. The gateway and its grants bound the user; each project narrows that.
- Scope is contextual and judged by the Decisions API. There are no tool lists or argument rules in the vetter.
- The purpose lives only in the gateway's instructions, and the agent never sees it.
- One judge, the Decisions API, with no fallback model. Judge failure denies every call.
- Out-of-scope calls must be blocked 100% of the time. Thresholds come from the eval and from real sessions.

**Open:**
1. **Granularity:** select individual tools, or whole toolkits when most of one passes?
2. **Upstream auth:** is a mode-600 `.env` acceptable until the plugin moves keys to secure storage, or should the proxy run the gateway's OAuth flow so no long-lived key sits on disk?
3. **A local judge:** a small model distilled from Decisions API answers and the audit log, gated on the scope eval, would remove the per-call network hop and allow air-gapped use. When is it worth it?
4. **Name:** is `arcade-tool-selector` still right now that the project mainly vets?
