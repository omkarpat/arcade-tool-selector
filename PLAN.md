# Plan

Phases 1 to 3 are built: the proxy, the catalog, and per-call scope checks with their evals. Phase 4, prompt-time tool selection, comes next. Anything marked *planned* doesn't exist yet.

## The problem

An agent backed by Arcade gets the gateway's whole list of allowed tools, and the model picks which one to call. As the catalog grows, that costs tokens on every turn, makes it harder to choose between near-duplicate tools, and widens the damage a bad call can do.

It's also a security problem. Tools need broad OAuth scopes to be useful, and once a user grants them, they're live for every call the model makes, including calls triggered by injected text in a tool's output.

The core case is a grant that's broader than the project needs. One set of grants often serves many projects. A meeting scheduler needs the calendar and read-only email, while the same user has also granted email sending, file sharing and Linear access for other work. Arcade would run any of those calls. The scheduler should be stopped from sending email, sharing files, touching Linear, inviting outsiders, or doing anything injected text asks for.

What a project may do isn't a fixed tool list. A support bot legitimately sends email, but only replies to customers who wrote in. So scope has to be judged call by call, against the project's purpose, the user's request and the call's arguments.

There are two goals:

1. **Vet** *(built)*: check every call against the project's purpose before it runs, then allow it, refuse it, or ask the user.
2. **Select** *(planned)*: for each prompt, show the model only the few tools it's likely to need. The model still chooses among them and fills in the arguments.

Selection narrows what the model can attempt, and vetting decides what actually runs. Replacing Arcade's auth or execution is out of scope, and so is a hosted service.

## Prior art

[jev-search](https://github.com/superagents-lab/jev-search) picks which of a dozen search sources to query by asking yes/no questions and keeping the answers whose probability clears a threshold. I borrowed that shape. Each tool gets a short card, the judge answers yes/no questions with probabilities, and code proposes the candidates so the model can never invent a tool. What I added on top is a retrieval step, since an Arcade catalog has hundreds of tools rather than twelve, and labelled evals from the start.

## What Arcade already provides

- **Gateways** with an allow-list of tools and free-text instructions. The instructions are where each project's purpose lives.
- **Tool metadata** that marks tools as read-only or destructive and lists their scopes. The risk levels come from this.
- **Per-project API keys and OAuth grants.** A key from another project is rejected.
- **Execution history** for every call Arcade runs.
- **Contextual Access hooks** that run before and after calls. They see the user, the tool and its inputs, but not the request or the purpose, so they can't judge scope. Closing that gap is the feature I propose in [WRITEUP.md](WRITEUP.md).

Nothing built in selects tools based on the prompt.

## Design

### Claude Code integration *(built)*

A local MCP proxy sits in front of the gateway and is the only thing that talks to Arcade. Two Claude Code hooks feed it. When you send a prompt, a hook passes it to the proxy as the evidence future calls are judged against. When the model makes a call, another hook asks the proxy for a verdict. "Deny" refuses the call, and "ask" becomes Claude Code's own permission prompt with the reason attached. "Allow" stays silent, so Claude Code's normal rules still apply. The proxy runs only calls the hook vetted and didn't deny, so a call that skips the hook never reaches Arcade.

The hooks and the proxy find each other through a local socket named after the Claude Code process: the proxy is its direct child, and its hooks get its pid as `CLAUDE_PID`. The repo itself is a ready Claude Code project. It registers the proxy, adds the hooks, and turns off claude.ai connectors so the gateway is the only route to your apps.

API keys stay in the repo's `.env`, which only the proxy reads. Claude Code's own config would store them in plain text.

There are known limits. Built-in tools like Bash aren't covered. Subagents get their prompts from the parent model, not from you. And anyone who adds a second route to the same apps bypasses the proxy entirely, which is the strongest argument for moving this check into Arcade.

### Configuration *(built)*

Every setting has exactly one home, and anything missing fails loudly:

- **Arcade** holds each role's purpose (in the gateway's instructions), its tools and its grants.
- **`.env`** holds the user, the org, the OpenAI key and the list of roles. The role to run as is chosen at launch.
- **Code** holds the policy itself: thresholds and what each risk level does. These values were set from the evals.
- **Eval datasets** carry their own sample projects. They're the only local config.

### Vetting a call *(built)*

Each call gets one Decisions API request with up to four yes/no questions:

- Is this within the project's purpose, given its arguments?
- Did the user ask for it? Writes must be asked for directly. Reads only need to be a sensible step toward the request.
- For writes: does it reach other people, such as an email, message, invite or share?
- For writes: does it reveal internal details to someone outside the organization?

The judge sees the purpose, the user's own messages, today's date and the call. Tool outputs are never evidence, which is what stops injected text from raising a score.

Risk sets the consequence. An out-of-scope read or write asks the user, and an out-of-scope send or destructive call is refused. Anything in scope but not clearly requested also asks. Destructive calls always ask. If the judge is down or the gateway has no purpose, every call is refused.

The audit log records the decisions and reasons, plus a key that links each executed call to Arcade's own history, rather than duplicating it.

### Tools and purpose *(built)*

The proxy reads every page of the gateway's tool list, and the purpose, at startup and every 15 minutes. When the tools change, Claude Code is told to reload them. If a refresh fails, every call is refused until the next one succeeds.

### Prompt-time selection *(planned, phase 4)*

It starts with a selection eval: prompts labelled with the tools they need, scored for recall and tokens per turn, with "send every tool" and plain keyword search (BM25) as the baselines to beat. Then, for each prompt:

1. A cheap keyword search builds a shortlist of about 40 candidate tools.
2. One batched judge request asks, for each candidate, whether it's relevant to the request and whether using it fits the purpose. A tool is shown only if both are yes, so "reply to the customer" can expose email sending while "forward all invoices to my personal email" doesn't.
3. The top few (eight by default) are kept, plus any pinned tools. If nothing qualifies, the project's defaults are used, never all tools.
4. The proxy swaps its advertised tool list and tells Claude Code.

A `request_more_tools` escape hatch lets the model ask for more when the set falls short. Anything it adds is still vetted. Calls to tools outside the current selection are refused. The target is under three seconds at p95.

### Other surfaces *(planned)*

- A Claude Code plugin with proper key storage.
- A Python library for other agent frameworks.
- A generic MCP mode for clients like Cursor.
- A Contextual Access hook server that shares the same policy code, once Arcade passes intent to hooks.

## Evaluation

**Built:**

- **Scope eval.** 65 cases across five sample projects on one broad gateway, each labelled allow, ask or deny. The bar is 100% of out-of-scope calls blocked, with friction as the second number. At the current threshold (0.65), it blocks 100%, holds 7% of legitimate calls, and matches exactly on 95%.
- **Live tests** that run the real gateway, judge, hooks and proxy end to end.

**Planned:**

- Labelling real decisions from the audit log.
- Per-project thresholds.
- Recorded judge responses so CI is free and deterministic.
- A report of OAuth scopes a project never uses, so teams can narrow their grants.

## Phases

| # | What | Status |
|---|---|---|
| 1 | Pass-through proxy, hook-to-proxy link, audit | done |
| 2 | Tool cards and risk levels | done |
| 3 | Per-call scope vetting and the scope eval | done |
| 4 | Selection eval with baselines, then prompt-time selection | next: must beat keyword search at the same set size |
| 5 | `request_more_tools`, sticky selections, unused-scopes report | |
| 6 | Claude Code plugin | |
| 7 | Better shortlist (keywords plus embeddings) | |
| 8 | Python library and other MCP clients | |
| 9 | Contextual Access hook server | |

## Decisions and open questions

**Decided:**

- Claude Code comes first.
- A project's boundary is its purpose, not its gateway.
- Scope is judged per call, with no tool lists in the vetter.
- The purpose lives only in Arcade, and the agent never sees it.
- There's one judge and no fallback model. If it fails, everything is refused.
- Out-of-scope calls must be blocked 100% of the time.

**Open:**

1. Should selection pick individual tools, or whole toolkits when most of one qualifies?
2. Is a locked-down `.env` good enough until the plugin moves keys to secure storage?
3. When is a small local judge, trained on Decisions API answers, worth building?
4. Is "tool selector" still the right name, now that the project mostly vets?
