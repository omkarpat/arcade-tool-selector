# arcade-tool-selector

Prompt-based tool gating for [Arcade](https://arcade.dev). Normally an LLM gets every Arcade tool and decides which one to use. Here a selector picks the few tools relevant to the user's prompt, within a project-level scope, and only those reach the LLM. Each call the model then makes is vetted against project policy before Arcade executes it. Policy covers risk tiers, argument rules, intent checks and human approval.

The selection paradigm is borrowed from [jev-search](https://github.com/superagents-lab/jev-search): batched yes/no gates with probabilities, plus thresholds, pins and fallback defaults.

> **Status: phase 3 (per-call scope vetting).** See [PLAN.md](PLAN.md).
> - The proxy serves every gateway tool from a cached, polled catalog.
> - Every call is vetted against the project's purpose (`project_prompt` in `ats.yaml`) before it runs. Vetting happens in Claude Code's `PreToolUse` hook, and again inside the proxy for calls that skip the hook.
> - Selection (only relevant tools listed) lands in phase 4. The setup below won't change when it does.

## How it works with Claude Code

```
today:  Claude Code ──http──► Arcade MCP gateway                (all tools, model picks)

with this:
        Claude Code ──stdio──► ats-proxy (selector + vetter) ──http──► Arcade MCP gateway
             │                     ▲
             ├─ UserPromptSubmit ──┘  prompt → selection → tools/list narrowed (list_changed)
             └─ PreToolUse ────────►  vet each call: allow / deny / ask (Claude Code's prompt)
```

The proxy keeps your existing server name (e.g. `workspace-search`), so tool names, permission rules and habits stay the same.

## Setup (any Claude Code instance)

### Prerequisites

- Claude Code v2.1.295 or later, which hooks need for `onFailure`.
- [`uv`](https://docs.astral.sh/uv/).
- An Arcade account and an API key (Arcade dashboard → API keys).
- An OpenAI API key for the judge model. The default judge is OpenAI's Decisions API.

### 1. Create a gateway for the proxy

In the Arcade dashboard, open your project → **MCP Gateways** → **Create**:
- **Allowed Tools:** the tools this project may ever use. This is the outer boundary; the selector only narrows within it.
- **Authentication:** **Arcade Headers**. The proxy authenticates with your API key plus an `Arcade-User-ID` header, instead of the browser OAuth flow Claude Code uses for a direct connection.

Copy the gateway URL (`https://api.arcade.dev/mcp/<slug>`).

If you already use a gateway directly from Claude Code, create a **new** gateway with the same tools rather than changing the auth mode on the existing one. You can then switch back at any time.

### 2. Get the code and add your keys

Where each value comes from:

| Variable | Where to get it |
|---|---|
| `ARCADE_API_KEY` | Arcade dashboard → **API Keys** → create a key (starts with `arc_`). Alternatively, `arcade login` with the Arcade CLI stores one locally. |
| `ARCADE_USER_ID` | An id you choose for yourself, usually the email you sign in to Arcade with. Arcade keys tool authorizations (your Gmail, Slack, … OAuth grants) to this id. A new id means re-authorizing each toolkit once on first use. |
| `ATS_GATEWAY_URL` | The gateway from step 1: dashboard → **MCP Gateways** → your gateway → URL (`https://api.arcade.dev/mcp/<slug>`). It must use **Arcade Headers** auth. |
| `ATS_CONFIG` | The project config from step 3. It defaults to `ats.yaml` in the repo root. |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys). The default judge is the OpenAI Decisions API (`judge.chain: [openai-decisions:gpt-6-luna]`). `ANTHROPIC_API_KEY` is only needed if you add an Anthropic judge. |

```sh
git clone https://github.com/omkarpat/arcade-tool-selector ~/arcade-tool-selector
cd ~/arcade-tool-selector && uv sync

cp .env.example .env && chmod 600 .env
$EDITOR .env                     # ARCADE_API_KEY, ARCADE_USER_ID, ATS_GATEWAY_URL, judge key
```

`.env` is gitignored, and only `ats-proxy` reads it. It isn't passed through Claude Code's config, which matters for two reasons:
- `claude mcp add -e KEY=…` would store the key in plain text in `~/.claude.json`.
- Exporting the key from your shell profile would expose it to every process you start.

To keep the file somewhere else, set `ATS_ENV_FILE`. Real environment variables override the file's values, which is useful for CI.

The only variable Claude Code itself needs is the repo path, which the MCP entry and the hooks use:

```sh
export ATS_HOME=~/arcade-tool-selector     # in your shell profile
```

### 3. Write the project config

```sh
cp examples/claude-code/ats.yaml ats.yaml     # gitignored: holds your teams, channels, domains
```

Then edit these fields:
- `project_prompt`: what this agent is for. Selection and intent checks are judged against it.
- `server_name`: must match the MCP server name you use in step 4.
- `policy.args`: your Linear team keys, allowed Slack channels and email domains.

`ATS_CONFIG` in `.env` points at this file. See [PLAN.md §4.6–4.7](PLAN.md) for the policy model.

### 4. Point Claude Code at the proxy

Replace the direct gateway entry with the proxy under the **same name**.

**CLI**, which writes the entry to your user or local config:

```sh
claude mcp remove workspace-search
claude mcp add workspace-search -- uv run --project $ATS_HOME ats-proxy
```

**Or a project `.mcp.json`**, which is checked into a repo:
- Before: [`examples/claude-code/mcp.before.json`](examples/claude-code/mcp.before.json)
- After: [`examples/claude-code/mcp.json`](examples/claude-code/mcp.json)

Neither form carries secrets; the proxy loads them from the env file.

### 5. Add the hooks

Merge [`examples/claude-code/settings.json`](examples/claude-code/settings.json) into `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project). The `PreToolUse` and `PostToolUse` matchers must use your server name: `mcp__<server_name>__.*`.

| Hook | What it does |
|---|---|
| `UserPromptSubmit` | Sends the prompt to the proxy. The proxy selects tools and narrows the server's tool list for this turn. |
| `PreToolUse` | Vets each call. Results are allow, deny (the reason goes to the model) or ask (Claude Code's permission prompt). `onFailure: block` means a crashed or timed-out vetter blocks the call instead of letting it through. |
| `PostToolUse` | Audit log and result redaction. |

### 6. Check it

- Restart Claude Code, then run `/mcp`. The server should be connected through `uv`.
- Send a prompt that needs a tool. In phase 1 every gateway tool is listed; from phase 4, `/mcp` shows only the selected tools.
- Ask for something outside your project's purpose (e.g. Linear, in a calendar-only project). The call should be refused with the vetter's reason. A destructive in-scope call (e.g. deleting an event) should show Claude Code's permission prompt.
- Check the files under `~/.local/share/ats/` (override with `ATS_DATA_DIR`):
  - `audit.jsonl` has one line per call, prompt and catalog change; see [Audit](#audit).
  - `proxy.log` is the proxy's own log.
  - `hook.log` shows whether each prompt hook reached the proxy.

### Audit

Arcade already records every executed tool call: status, timing, and inputs/outputs for admins. It's under the dashboard's tool executions, retained 7–90 days. The proxy doesn't duplicate that. It logs what Arcade can't see:

| Event | Meaning |
|---|---|
| `call` | Pointer to Arcade's record: `user_id`, `toolkit`, `tool`, `started_at` |
| `call_needs_authorization` | The user hasn't authorized the toolkit yet. The call never ran, so Arcade has no record. |
| `vetted` | A scope decision. Records where it was made (`hook` or `proxy`), the action (allow / ask / deny), which check decided, and the judge's answers. |
| `call_failed` | The gateway was unreachable |
| `prompt_seen` | A prompt reached the proxy: session id, prompt id, length. The prompt text is not stored. |
| `catalog_changed` | Tools added, removed or changed on the gateway |

These events go to `audit.jsonl` by default. To send them to an OpenTelemetry collector instead (Datadog, Grafana, a SIEM, …):
1. Run `uv sync --extra otel`.
2. Set `OTEL_EXPORTER_OTLP_ENDPOINT` (plus `OTEL_EXPORTER_OTLP_HEADERS` if your collector needs auth) in `.env`.

Events are emitted as OTel log records named `ats.<event>`.

To see recent events, with each call resolved to its Arcade execution, set `ARCADE_ORG_ID` / `ARCADE_PROJECT_ID` in `.env` and run:

```sh
uv run ats-audit 20
```

### Tests

```sh
uv run pytest                                  # offline
ATS_LIVE=1 uv run pytest -s                    # end-to-end against the gateway in .env
```

### How calls are vetted

Your Arcade grant (the gateway plus OAuth scopes) is usually broader than one project needs. Each call is judged against `project_prompt` by OpenAI's Decisions API, using two questions:
- **`within_purpose`:** does the project's purpose cover this action, with these arguments?
- **`requested`:** do your own messages ask for this exact call? Text from emails, documents or other tool results never counts.

| Result | Outcome |
|---|---|
| Outside the purpose | Your tier's `on_low_scope`: by default, read and write ask, while send and destructive deny |
| Within the purpose, but unclear you asked | ask |
| In scope, tier in `policy.approve` (default: destructive) | ask |
| In scope | Nothing. Claude Code's own permission rules apply; the vetter only tightens. |

Hard limits apply before the judge and always deny: `scope.deny`, and the argument rules in `policy.args` (domains, allowlists, max items).
- If the judge is unreachable, write, send and destructive calls fail closed.
- If the proxy is unreachable, the hook blocks the call.

The judge reads only the project prompt, your prompts in this session, today's date and the proposed call. Prompts stay in the proxy's memory; the audit log records only their length.

### Scope evals

```sh
uv run python evals/scope.py                   # live Decisions API, one call per case
```

`evals/datasets/scope_decisions.yaml` holds the cases: example projects on the same broad gateway, each with your messages and a proposed call, labelled allow / ask / deny. The report shows:
- the share of out-of-scope calls blocked (target 100%);
- the share of in-scope calls held or denied;
- exact matches;
- a threshold sweep.

### Selection evals

```sh
uv run python evals/snapshot.py workspace      # snapshot your gateway's catalog from the proxy's cache (gitignored)
uv run python evals/run.py                     # baselines on that snapshot
```

`evals/datasets/tool_selection.yaml` is the tool-selection (relevance) dataset: labeled prompts. Each one lists the tools it needs; where several tools would each do, any one counts. Each selector is scored on four measures:
- recall
- prompts fully solved
- precision
- schema tokens handed to the model

### Plugin install (phase 6)

Once packaged, steps 2–5 collapse to:

```
/plugin marketplace add omkarpat/arcade-tool-selector
/plugin install arcade-tool-selector@arcade-tool-selector
```

The plugin prompts for the gateway URL, API key (stored in secure storage), user id and config path. Plugin MCP tools are namespaced `mcp__plugin_arcade-tool-selector_<server>__<Tool>`, so permission rules that name the old tool names need updating.

### Roll back

```sh
claude mcp remove workspace-search
claude mcp add --transport http workspace-search https://api.arcade.dev/mcp/<original-slug>
```

Then remove the three hooks from `settings.json`.

## License

MIT
