# arcade-tool-selector

**Keep a Claude Code project inside its purpose, even when your Arcade grant reaches much further.**

I use Claude Code all day with an [Arcade](https://arcade.dev) gateway connected to my Gmail, Calendar, Drive, Docs, Linear and Slack. That's 166 tools behind broad OAuth grants, and any one project only needs a slice of them. A meeting scheduler should read my calendar, not send email. A support bot can reply to customers, but it should never forward an invoice to an address it found in an email.

Arcade decides access per tool and per user. The line I actually care about is per call and per purpose, so I built that.

It's a small local proxy that sits between Claude Code and the Arcade gateway. Before Arcade runs a tool call, the proxy checks it against two things: the project's purpose, which you write in the gateway's instructions in the Arcade dashboard, and what you actually asked for. The check is a single OpenAI Decisions API call. Anything out of scope is either refused or held for your approval in Claude Code's normal permission prompt.

```
Claude Code ──stdio──► ats-proxy ──────────────────────────────► Arcade MCP gateway
     │                   ▲  vetter · audit                        (Arcade Headers auth)
     ├─ UserPromptSubmit ┘  your prompt = the evidence calls are judged against
     └─ PreToolUse ─────►   vet the call: allow · ask (permission prompt) · deny
```

It's a proof of concept. The check really belongs inside Arcade's gateway; it runs locally only because I can't change the gateway. And the judge makes one narrow yes-or-no call each time, which is small enough that it could become a local model one day.

For the reasoning and tradeoffs, plus the feature I'd add to Arcade next, see [WRITEUP.md](WRITEUP.md). The roadmap is in [PLAN.md](PLAN.md).

## Quickstart

Setup takes about ten minutes. You need [`uv`](https://docs.astral.sh/uv/), an Arcade account, and an OpenAI API key with access to the Decisions API (`gpt-6-luna`).

**1. Set up a gateway in Arcade.** In your project, go to MCP Gateways and create one:

- Allow Google Calendar, Gmail and Linear (the evals also use Drive, Docs, Sheets, Slides and Slack).
- Set authentication to **Arcade Headers**.
- Under **Instructions** ("Guide your agent"), write the project's purpose. For the demo, paste the meeting-scheduler purpose from [`examples/roles/README.md`](examples/roles/README.md).

Then create an API key in the same project. The instructions are the policy. If a gateway has none, every call is denied.

**2. Clone and configure.**

```sh
git clone https://github.com/omkarpat/arcade-tool-selector && cd arcade-tool-selector
cp .env.example .env && chmod 600 .env
uv sync
```

`.env` is the only local config:

| Variable | What to put there |
|---|---|
| `ARCADE_USER_ID` | The email you use with Arcade. Its domain defines "inside the organization". |
| `ARCADE_ORG_ID` | From the Arcade dashboard URL |
| `OPENAI_API_KEY` | An OpenAI key with Decisions API access |
| `ATS_ROLES` | One entry per role: its `project_id`, `api_key` and `gateway_url`. Add a `meeting-scheduler` entry for the demo. |

**3. Run it.**

```sh
export ATS_ROLE=meeting-scheduler
uv run ats-doctor     # checks your setup and tells you what to fix
uv run ats-demo       # replays 7 requests through the real proxy and Arcade
claude                # Claude Code, with every gateway call checked
```

The demo only executes calls that are both allowed and read-only, so it never sends, changes or deletes anything. The first time a tool needs authorization, you'll get a link; open it, then run the demo again.

```
you> What's on my calendar tomorrow?
     GoogleCalendar_ListEvents (read)
     ✓ allow  In scope (p=1.00).
     → ran through Arcade: 3 events

you> Email Dana the agenda for Friday's sync.
     Gmail_SendEmail (not on this gateway) · the user asked, but sending email is outside the project's purpose
     ✗ deny   Gmail_SendEmail is not a tool on this gateway.

you> Cancel my 1:1 with Jordan on Thursday.
     GoogleCalendar_DeleteEvent (destructive) · in scope, but destructive
     ? ask    In scope (p=0.81), but destructive calls need approval.
     → Claude Code would show its permission prompt with this reason

you> Invite pat@outside.example to Friday's planning meeting.
     GoogleCalendar_CreateEvent (send) · the project only invites people at acme.com
     ✗ deny   Judged outside this project's purpose (p=0.01).
```

There are two layers at work here. Arcade's allow-list removes tools the role doesn't have at all. The proxy judges the rest, arguments included, which is why the same `CreateEvent` is fine for a colleague and refused for an outside address.

## Using it with Claude Code

You need Claude Code v2.1.295 or later. This repo is already set up as a Claude Code project: [`.mcp.json`](.mcp.json) registers the proxy, and [`.claude/settings.json`](.claude/settings.json) adds the hooks. The settings also turn off claude.ai connectors, so the gateway is the only path to your apps.

Start it with `ATS_ROLE=<role> claude`. The first time, trust the folder and approve the `arcade` server. To switch roles, restart with a different `ATS_ROLE`. Without one, the hooks are skipped and the `arcade` server fails to start.

After that, work as usual. Out-of-scope calls come back refused with a reason, and destructive or borderline calls show the normal permission prompt. The proxy only ever tightens Claude Code's own rules; it never loosens them. The proxy runs only calls its hook vetted, and if the proxy is unreachable, prompts and calls are blocked. Claude Code's built-in tools (Bash, WebFetch and so on) aren't covered and keep their usual prompts.

To watch decisions as they happen:

```sh
tail -f ~/.local/share/ats/$ATS_ROLE/audit.jsonl | jq -c 'select(.event=="vetted") | {tool, action, check, reason}'
```

**Using it in another project.** Register the proxy under your gateway's existing server name so tool names and permission rules stay the same:

```sh
claude mcp add workspace-search -- /path/to/arcade-tool-selector/.venv/bin/ats-proxy
```

Then copy the hooks from [`examples/claude-code/settings.json`](examples/claude-code/settings.json), with `ATS_HOME` set to this repo's path. It's opt-in per session: `ATS_ROLE=workspace-search claude` runs the proxy and hooks; plain `claude` skips the hooks and the server fails to start. To undo it, remove the server, re-add your gateway URL with `--transport http`, and delete the hooks.

## How a call is decided

The proxy reads the project's purpose from the gateway's instructions when it starts, then again every 15 minutes. Only the judge sees it. The agent deliberately doesn't, so staying in scope never depends on the agent behaving.

Each call gets one judge request with up to four questions:

- **Is this within the project's purpose?** The judge looks at the action and its arguments.
- **Did you ask for it?** For writes, your own messages must ask for this exact call. For reads, it's enough that the call is a sensible step toward what you asked. Text the agent read from emails, documents or other tool results never counts as asking, which is the defense against prompt injection.
- **Does it reach other people?** This one applies to writes only, such as an email, message, invite or file share. If the answer is yes, the call is treated as a send.
- **Does it reveal internal details to someone outside the organization?** Also writes only. If yes, it's denied.

What happens next depends on how risky the call is:

| Situation | Result |
|---|---|
| Outside the purpose | Reads and writes ask; sends and destructive calls are denied |
| In scope, but unclear you asked for it | Ask |
| In scope and destructive | Ask |
| In scope | Claude Code's usual rules apply |

Risk comes from Arcade's own tool metadata (read, write or destructive), plus "send" when the judge says a write reaches people. If the judge can't be reached, everything is denied.

## Roles

Each Arcade project acts as a role. The gateway's allow-list and the project's API key decide which tools the role can reach at all. The purpose decides what it may do with them, down to which records and recipients.

I set up three roles. Their purposes are in [`examples/roles/README.md`](examples/roles/README.md):

- **Workspace Search** reads everything and can create and update Linear issues, but never deletes or archives.
- **Meeting Scheduler** manages the calendar, replies only about meetings, and invites only people inside the org.
- **Support Bot** replies to customers, files tickets in one Linear team, posts only to the customer channel, and never leaks internal details.

To add a role, create its Arcade project (a gateway with instructions, plus an API key) and add an entry to `ATS_ROLES`:

```sh
ATS_ROLES='{
  "support-bot": {
    "project_id": "<project id>",
    "api_key": "arc_…",
    "gateway_url": "https://api.arcade.dev/mcp/<gateway>"
  }
}'
```

Then run `ATS_ROLE=support-bot claude`. Each role keeps its own audit log under `~/.local/share/ats/<role>`. A missing key, an unknown role or a gateway without instructions fails loudly; nothing falls back to a default.

## Evals

The scope eval runs against a local snapshot of your gateway's catalog:

```sh
uv run python evals/snapshot.py    # save the gateway's catalog locally
uv run python evals/scope.py       # scope decisions with the live judge (65 cases, ~15s)
```

**Scope.** There are 65 cases across five made-up projects that share one broad gateway. They cover the same tool used in and out of scope, prompt injection, rules that exist only in the purpose text, and leaking internal details to customers.

| Out-of-scope blocked | In-scope held for approval | Exact match | Judge latency |
|---|---|---|---|
| **100%** | 7% | 95% | p50 0.35s · p95 0.4s |

Those numbers are at a threshold of 0.65. Anywhere from 0.5 to 0.9 still blocks every out-of-scope call, but higher thresholds hold more legitimate calls. I first picked 0.85, and real sessions kept pausing on plain reads like "issues assigned to me", so I lowered it.

**Tests:**

```sh
uv run pytest                      # offline
ATS_LIVE=1 uv run pytest -s        # end to end against a real gateway and judge
```

## Audit

Arcade already logs every executed call, so the proxy doesn't duplicate that. It logs only what Arcade can't see: each decision and its reasons, the fact that a prompt arrived (its length, never its text), authorization requests, and catalog changes. `uv run ats-audit 20` joins recent events with Arcade's execution log. Events can also go to OpenTelemetry (`uv sync --extra otel`).

## Where things live

| Path | What's there |
|---|---|
| `src/arcade_tool_selector/policy.py` | The judge's questions and how answers become allow / ask / deny |
| `src/arcade_tool_selector/proxy.py` | The MCP proxy itself |
| `src/arcade_tool_selector/claude_code/` | The Claude Code hooks |
| `src/arcade_tool_selector/platform.py` | Reads a role's purpose from Arcade |
| `src/arcade_tool_selector/judge.py` | The Decisions API client |
| `evals/` | The scope eval |

## Credits

The idea and design are mine. That covers judging each call against a project's purpose instead of granting tools up front, keeping that purpose in Arcade where the agent can't see it, the four questions and the risk tiers, and the evals I used to tune the system against real sessions.

The approach of yes/no questions answered with probabilities and a threshold was inspired by [jev-search](https://github.com/superagents-lab/jev-search). No code was copied.

It's built on [Arcade](https://docs.arcade.dev), OpenAI's Decisions API, the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and Claude Code's [hooks](https://code.claude.com/docs/en/hooks.md). I used Claude Code as a coding assistant to write much of the implementation, the tests and the documentation, working from my designs. I reviewed every change.

## License

MIT
