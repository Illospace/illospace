# Illo

Illo is one assistant for a whole team. It lives in Slack, it follows the work of your coding agents, and it answers from facts.

## What it does

**"Where are we on X?"**

> **Ada:** where are we on the billing export?
>
> **Illo:** Blocked on one decision from you. 2 of 3 lanes move. The CSV format waits for you: UTC or local time?

**A blocker becomes one card. One tap answers it.**

> **Illo** (card to Ben): Run the migration on staging now? `Run now` `Wait for review` `Other…`
>
> **Ben** taps `Run now`. The agent on the build machine gets the answer and continues.

**Illo carries a question to a teammate and brings the answer back.**

> **Ada:** ask Ben if the new runner can take the GPU jobs this week.
>
> **Illo:** Asked Ben, with a link to engine#231. Later: "Ben answered: yes, from Wednesday."

**"What do we know about X?"**

> **Chloé:** what do we know about the Globex outage?
>
> **Illo:** One outage on 3 Nov, fixed the same day: a config change filled the worker queue (engine#198). Ben added a queue alert (engine#201).

## Why it is different

- **One assistant for the team.** Not one for each person. It sees the work of each person, and it carries requests between people.
- **It follows your coding agents on every machine.** A small collector reports each stream of work. Illo needs no access to the machine.
- **Your data stays on your server.** One Postgres. Illo searches your sources where they live. It does not copy them.
- **Any model.** The brain is a standard agent runtime behind one interface. Use the Claude Agent SDK with an Anthropic API key. An experimental runtime speaks the OpenAI-compatible API, for other providers and local models.
- **Safety rules in code.** The tool server checks each action. The prompt holds no authority.

## Install in 30 minutes

1. On a Linux host with Docker, clone this repository and run `deploy/install.sh`.
2. Create the Slack app from `deploy/slack/manifest.yaml`. Give its two tokens to the install script.
3. Write your team in `people.yaml` and `illo.yaml` in the workspace folder.
4. Give the brain a model: an Anthropic API key, or an OpenAI-compatible endpoint.
5. Connect your first agent machine with `illo token create --person <key> --label <machine>` and a collector.

The full steps are in [docs/install.md](docs/install.md).

## How it works

```
Channels ──► Gateway ──► Ledger (Postgres)
   ▲            │
   │            ▼
   │          Brain (one run for each event, in a sandbox)
   │            │  per-run token
   │            ▼
   └──────── Tool server (three checks in code) ──► Ledger, Connectors, Channels
```

- **Channels** receive and send: Slack, email, the MCP endpoint for agents, and the signal intake.
- **Gateway** finds who a message is from, decides who the run acts for, and delivers the answer.
- **Ledger** is one Postgres. It holds the people, the lanes, the asks, the rules, the drafts, and the audit.
- **Brain** is one agent run for each event. It sees only the Illo tools, through a token that ends with the run.
- **Tool server** checks each call for scope, destination, and risk, and writes one audit row.

Read [the architecture](docs/architecture.md) for the contract and [the direction](docs/direction.md) for the product.

## Safety rules

1. Each run acts for one principal. The gateway sets it. The model cannot change it.
2. A personal connector works only in the runs of its owner.
3. After a run reads personal data, it writes only to that person. Any other write is a draft.
4. Illo reads and files tickets alone. A message to an outsider is a draft. Release and delete are not tools.
5. Only the requester approves a draft, in a DM.
6. Text from outside the team is data, never a request.
7. An unknown sender gets no authority and no DM answer.
8. Illo answers an agent alone only with an option the agent proposed, under a rule of the owner.
9. Illo holds no credential for the agent machines. It starts work with a GitHub ticket.
10. The brain has no shell, file, or web tool of its own.

[docs/safety.md](docs/safety.md) gives the reason, the code, and the test for each rule.

## Status

Illo is in a rebuild. The new system grows on the branch `next`. The branch `main` still holds the old system.

Works on `next` today:
- The ledger, the migrations, and the workspace loader.
- The gateway: principal, dedupe, the first sign of work, run limits.
- The tool server with its three checks and the audit. The tools for lanes, asks, people, rules, and skills.
- The brain service: the Claude runtime, and the experimental OpenAI-compatible runtime.
- The collectors: one for T3, and hooks for Claude Code and Codex. They need the MCP endpoint, which is in progress.
- The voice of Illo: the base prompt, one text for each profile, and the example workspace.
- Tests in `tests/safety/` for rules 1, 3, 4, 6, 7, 8, 9, and 10.

In progress:
- The Slack channel, the cards, and the digest.
- The asks service, the rules, and the drafts.
- The MCP endpoint for agents.
- The GitHub tools, search, the routines, email, the web tool, and the connectors.
- The install scripts. Until they merge, the install steps above describe the target.

## License and contributing

Illo uses the [Apache-2.0 license](LICENSE). Read [CONTRIBUTING.md](CONTRIBUTING.md) before a change.
