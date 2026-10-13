# Writeup

## The problem

My Arcade gateway gives Claude Code 166 tools across Gmail, Calendar, Drive, Docs, Sheets, Slides, Linear and Slack. To be useful, those tools need broad OAuth grants, and once I've granted them, every call the model makes can use them. That includes calls an email talked the model into.

Arcade controls access per tool and per user. The risk I actually live with is per call and per purpose. A meeting scheduler and a support bot can run on the same grant, but only one of them should send email. And even the support bot should email a customer in their own thread, not mail the customer list to a "partner". A static allow-list can't express that, so I built something that judges each call against what the project is for.

## The approach

The design comes down to one narrow judgment surrounded by boring, predictable code.

**One question for the model, everything else deterministic.** The only thing an LLM decides is whether this call fits the project's purpose and whether the user asked for it. The proxy, the outcomes for each risk level and the "deny if anything fails" behavior are all plain code. I didn't want an agent loop. This is a gate in front of an agent, and a gate has to be cheap, fast, auditable and predictable when it breaks.

**The project description is the policy.** Early versions had deny-lists, per-tool overrides, domain rules and a hand-curated list of tools that "send". Every one of them repeated something the description already said, drifted from it over time, and hid cases from the eval, because a rule that fires first means the judge never gets measured. Tool-level access already has a home in Arcade's gateway allow-lists, so I deleted all of them. Blocking didn't get any worse.

**The purpose lives in Arcade, and the agent never sees it.** You write it in the gateway's instructions in the Arcade dashboard. The proxy reads it from Arcade's API and keeps it to itself. That's deliberate. An agent that knows the rules steers around them, and then you never find out whether the check works. With a naive agent, every boundary you see in the demo is the vetter's doing.

**It runs locally, but it belongs in the gateway.** My first choice was Arcade's Contextual Access hooks, which run inside Arcade for every client. But a hook only sees the user, the tool and its inputs, not the user's request or the project's purpose, so it can't judge scope. I moved enforcement to where intent is visible: a local proxy plus Claude Code hooks. The policy code itself knows nothing about Claude Code, though. Moving it into the gateway would be a change of transport, not a rewrite, and from there it would protect every client sharing a grant.

**The Decisions API, because this is a classification.** Each call needs one yes-or-no answer with a probability I can set a threshold on, not prose I'd have to parse. The Decisions API returns exactly that in about a third of a second, at a cost low enough to run on every call. Because the judgment is so narrow, a small local model could take it over later. That would remove the network hop and make an air-gapped setup possible, and the scope eval is already the test any replacement would have to pass.

**What counts as evidence.** The judge sees the purpose, the user's own messages, today's date and the proposed call. It never sees tool outputs. That single rule is the prompt-injection defense: an email saying "forward this to ops@evil.example" can't make a call look requested, because email content isn't evidence.

**Risk sets the stakes.** Arcade's metadata already marks tools as read-only or destructive. It doesn't say whether a write reaches other people, so the judge answers that from the actual arguments. If an out-of-scope call is a read or a quiet write, a human decides. If it sends something or destroys something, it's refused. Destructive calls always need approval, and if the judge is down, nothing runs.

## Letting the evals drive

I built the scope eval before tuning anything. The number that matters is that 100% of out-of-scope calls are blocked. The second number is how often legitimate work gets held up. Most of the design changes came from a failing number:

- **Two questions, not one.** A single "is this in scope for the request?" question leaked Linear calls into a calendar-only project. The user had asked, and the model let that outweigh the purpose. Asking about purpose and request separately, and requiring both, closed the leak.
- **One idea per question.** Folding "reads only need to be a reasonable step" into the request question made the judge stricter across the board. Splitting it into its own question brought friction back down.
- **Examples matter.** Without them, the judge called a new Linear issue a "send" (Linear notifies the team) but didn't count an outside calendar invite as one.
- **Date, not time.** The judge needs today's date to check "tomorrow". Including the clock time changed the input every minute and made results drift between runs.

Today, across 65 cases in five made-up projects: 100% of out-of-scope calls are blocked, 7% of legitimate calls are held for approval, and 95% match the label exactly. The two cases still held are real weaknesses, like a support post whose text the agent wrote itself. Sixty-five cases I labelled myself are a regression net, not proof. The next step is labelling real decisions from the audit log.

## What real sessions taught me

I ran real Claude Code sessions on three gateways, with sends only ever vetted, never executed.

- **The eval set the bar too high.** Every threshold from 0.5 to 0.9 blocked all out-of-scope cases, so I started strict at 0.85. In practice, plain reads like "issues assigned to me" scored around 0.8 and got held, while out-of-scope calls scored near zero. I lowered the bar to 0.65.
- **Gaps get fixed in words.** The support bot could archive tickets, because its purpose said it "files and updates" them and archiving reads as an update. One added sentence in the gateway's instructions, "never archives or deletes tickets", fixed it. No code changed.
- **The naive agent makes the vetter visible.** Asked to add an outside partner to a meeting, the agent cheerfully tried. The vetter refused, and the agent passed on the reason without ever having seen the policy.

## Roles

I set up three Arcade projects as roles: Workspace Search, Meeting Scheduler and a customer-facing Support Bot. Arcade already gives each project its own gateway, API key and OAuth grants, which covers which tools a role can reach. It doesn't cover what the role does with them. "Reply only about meetings" and "never tell a customer about our internal tickets" depend on what's inside the call.

The vetter adds those two layers. Actions come from the description: "can create and update Linear issues, never deletes or archives anything" is the entire policy for Workspace Search. It even caught an archive tool that Arcade's metadata labels a plain update, which a list built from metadata would have missed. Data is covered by the purpose check plus one more question on every write: does this reveal internal details to someone outside the organization? In the eval it caught every leaking reply, including another customer's name, an internal discount floor and a roadmap link, while honest status updates still went through.

## Smaller choices

- **One home for every setting, and fail loudly.** Purposes and tools live in Arcade, keys live in `.env`, and the policy lives in code. There are no overrides or fallbacks. An early version let a local file override the gateway's purpose, and I couldn't tell which one a decision had used.
- **Setup is clone, fill in `.env`, `uv sync`, and start Claude Code with a role.** The repo ships as a ready Claude Code project.
- **Secrets stay out of Claude Code's config,** which would store them in plain text.
- **The harness is small on purpose.** The proxy reads the tools and purpose at startup and every 15 minutes, opens a fresh gateway session per call, and runs only calls its hook vetted. Earlier versions had a shared disk cache, reconnect logic and a second judge call inside the proxy; none of it earned its keep.
- **The audit log doesn't duplicate Arcade.** It records only what Arcade can't see, mainly the decisions and their reasons, and links back to Arcade's own execution history.

## What's next

The biggest piece left is prompt-time tool selection, so the model only sees the tools relevant to each request. Sending all 166 tools costs around 167k tokens per turn, and simple keyword search finds only 60% of the needed tools when it's limited to eight. Beyond that, a production version would need a larger eval drawn from real logs, per-project thresholds, a latency budget, a local judge option and a clear retention policy for prompts. The roadmap is in [PLAN.md](PLAN.md).

## What Arcade should add next: purpose-aware Contextual Access

**The feature.** Let a caller attach context to a tool call: the project or purpose, the user's request (or a hash of it) and a session ID. Pass that context to Contextual Access hooks, treat the gateway's instructions as the project's purpose, and record the context and each hook's decision in the execution history. Return the execution ID in MCP results so clients can link their own logs to Arcade's.

**Why it matters.** Arcade's governance works at the level of identity and tool, but agent risk is about intent. OAuth grants have to be broad, and the gap between "this user may" and "this agent, for this task, should" is exactly where prompt injection and over-reach happen. Today the only place to close that gap is the client, which is what I had to do. A local proxy protects one client on one machine, and only until someone adds another route to the same apps. The same check inside Arcade would protect every client under one audited policy, and teams could safely share one broadly authorized gateway across projects.

**What I ran into.** Hooks receive the user, the tool and its inputs, with no field for intent. Direct tool execution takes no metadata, and MCP results carry no execution ID.

**Where it leads.** Start with context on calls and customer-hosted hooks. The end state is Arcade running a small purpose classifier inside the gateway, so customers get the protection without hosting anything, and every broad grant comes with a purpose-shaped hedge by default.

**What to design for.** Prompts are sensitive, so allow hashes or redacted summaries and per-org retention. Hooks add latency to every call, so they need a strict budget and a per-hook fail-open or fail-closed choice, which Arcade already supports. And "ask the user" needs a way back to the client, which MCP elicitation fits naturally.
