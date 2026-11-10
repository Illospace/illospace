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
>
> **Ben** (in a DM): tell the agent to wait until 3 pm.
>
> **Illo:** A draft waits for your approval. `Send` `Do not send`
>
> An answer that the model writes is always a draft. Ben reads it and taps `Send`.

**Illo carries a question to a teammate and brings the answer back.**

> **Ada:** ask Ben if the new runner can take the GPU jobs this week.
>
> **Illo:** Asked Ben, with a link to engine#231. Later: "Ben answered: yes, from Wednesday."

**"What do we know about X?"**

> **Chloé:** what do we know about the Globex outage?
>
> **Illo:** One outage on 3 Nov, fixed the same day: a config change filled the worker queue (engine#198). Ben added a queue alert (engine#201).

**An alert becomes a ticket. A person assigns it.**

> **Monitor** (a bot in #alerts): The preview API returns HTTP 503.
>
> **Illo:** Filed engine#250. Nobody is assigned.
>
> **Illo** (DM to the default owner): New ticket from outside text, not assigned: engine#250. Assign it in GitHub, or tell me in a DM.

## Why it is different

- **One assistant for the team.** Not one for each person. It sees the work of each person, and it carries requests between people.
- **It follows your coding agents on every machine.** A small collector reports each stream of work. Illo needs no access to the machine.
- **Your team hosts the ledger.** One Postgres on your server. It stores the lanes, the asks, the saved items, and the messages of the public Slack channels that you configure. It also keeps each message to Illo and its answer. Illo searches the other sources where they live.
- **You choose the model provider.** That provider receives the prompts and the tool results of each run. Only a local model keeps everything on your server.
- **Any model.** The brain is a standard agent runtime behind one interface. Use the Claude Agent SDK with an Anthropic API key. An experimental runtime speaks the OpenAI-compatible API, for other providers and local models.
- **Safety rules in code.** The tool server checks each action. The prompt holds no authority.

## Install in 30 minutes

1. On a Linux host with Docker, run `git clone --branch next https://github.com/Illospace/illospace.git`. Then run `ILLO_DEPLOY_BRANCH=next deploy/install.sh` in the checkout. Use `next` until the rebuild merges into `main`.
2. Create the Slack app from `deploy/slack/manifest.yaml`. Give its two tokens to the install script.
3. Write your team in `people.yaml` and `illo.yaml` in the workspace folder.
4. Give the brain a model: an Anthropic API key, or an OpenAI-compatible endpoint.
5. Connect your first agent machine with `illo token create --person <key> --label <machine> --scope collector` and a collector.

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

Read [the architecture](docs/architecture.md) for the contract and [the direction](docs/direction.md) for the product. [The map of the docs](docs/README.md) lists every page.

## Safety rules

1. Each run acts for one principal. The gateway sets it. The model cannot change it.
2. A personal connector works only in the runs of its owner.
3. After a run reads personal data, it writes only to that person. Any other write is a draft.
4. Illo reads and files tickets alone. A message to an outsider is a draft. Release and delete are not tools.
5. Only the requester approves a draft, in a DM.
6. Text from outside the team is data, never a request.
7. An unknown sender gets no authority and no DM answer.
8. Illo answers an agent alone only with an option the agent proposed, under a rule of the owner. A model answer is a draft for the owner.
9. Illo holds no credential for the agent machines. It starts work with a GitHub ticket.
10. The brain has no shell, file, or web tool of its own.

[docs/safety.md](docs/safety.md) gives the reason, the code, and the test for each rule. It also lists what a hostile text can still do.

## Status

Illo is in a rebuild. All eleven areas of the new system are merged on the branch `next`, with the fixes of two reviews. The branch `main` still holds the old system until `next` merges into it.

On `next`:
- The ledger, the gateway, the tool server with its three checks, and the audit.
- The Slack channel, the cards, the drafts, and the digest.
- The asks service and the rules. The MCP endpoint for agents, with token scopes and caps.
- The GitHub tools and poller, search, notes, routines, email, the web tool, and the connectors.
- The brain service, the collectors, and the install scripts.
- A test in `tests/safety/` for each of the ten rules.

Tested only with fakes, never against the live service:
- Slack.
- GitHub writes.
- The mailbox (IMAP and SMTP).
- Google (mail, calendar, Drive).
- The `openai_compat` runtime.

## License and contributing

Illo uses the [Apache-2.0 license](LICENSE). Read [CONTRIBUTING.md](CONTRIBUTING.md) before a change.
