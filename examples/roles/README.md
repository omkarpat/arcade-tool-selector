# Roles: one Arcade project per role

Each role is an Arcade project with its own gateway, API key and OAuth grants. Its **purpose lives only in
the gateway's instructions** (Arcade dashboard → the project → MCP Gateways → the gateway → Instructions).
The vetter judges every call against it; the agent never sees it.

Purposes used for the three roles in this repo:

## workspace-search

```
Personal work search assistant. Searches and reads email, calendar, Drive, Docs, Sheets, Slides, Linear and Slack to answer the user's questions. It can create and update Linear issues and comment on them. It never deletes or archives anything, and doesn't create or change anything outside Linear issues.
```

## meeting-scheduler

```
Meeting scheduler. Reads the user's calendar, finds free time, and creates, moves or cancels events. Reads email to find scheduling requests and replies only to emails about meetings. It only invites people inside the organization. It does not send other email, share files, or use any other app.
```

## support-bot

```
Customer support agent. Reads customer emails and replies to customers in the threads they started, about their own issue. Files and updates support tickets in the Linear team SUP only, and posts updates in the #support Slack channel only. Never archives or deletes tickets, and never deletes or trashes email.
```
