# Direction

This page says what Illo is for, and what it will not be.

## Who it is for

A small team that works with coding agents. Each person runs several agents on several machines. The team talks in Slack and keeps its work in GitHub.

## The problem

- **A person cannot follow many agents.** Each agent works in its own terminal, on its own machine. A blocked agent waits in silence until someone looks.
- **The team context is spread out.** It lives in repositories, chat, a drive, and mail. Nobody can search all of it at once.
- **Each personal assistant sees one person.** It does not know what a teammate decided, or that a teammate waits for an answer.

## What Illo does

- It knows each lane of agent work, on each machine, and its state.
- It answers "where are we on X?" from the ledger, with links.
- It sends a card only for a real blocker. One tap answers it. The answer goes back to the agent.
- It carries a request from one person to another as an ask.
- It starts clear work through a GitHub issue.
- It searches the team context where it lives, and it keeps the decisions.

## How it must feel

People like a personal assistant that is fast, short, and quiet. Illo keeps that for a team.

- **Answer first.** The first line is the answer. Then a few short lines and links.
- **Fast.** The first sign of work comes from code, not from the model. With the Claude runtime, a simple turn shows work in about 2 seconds and answers in about 4 seconds.
- **Quiet by default.** A person gets a message for an ask, an answer, or the digest. Nothing else.
- **Honest limits.** When the ledger does not show a fact, Illo says "I do not know" and where it looked.
- **Phone first.** Short lines, no tables, no long text.

## Design choices

- **One assistant for the team.** The principal of each run is a person, the team, or an outsider. The gateway decides it in code.
- **Safe by code, not by prompt.** The tool server checks scope, destination, and risk on each call. The prompt holds no authority. See [safety.md](safety.md).
- **Truth comes from the source system.** A lane state and an ask state are rows in the ledger. The model reads them through tools.
- **Small.** One Python process, one Postgres, one brain service. No ORM, no queue service.
- **Any model.** The brain runs the Claude Agent SDK, one fresh process for each run, with only the Illo tools. A second, experimental runtime speaks the OpenAI-compatible API for other providers and local models.
- **The workspace is a folder of files.** People, rules, skills, routines, and notes live in a repository of the team. A person can read and change each one.

## What Illo will not be

- **Not a web app.** Slack, email, and the MCP endpoint are the only ways in.
- **Not a coding agent.** Illo does not write code, run commands, release, merge, or delete. It files a ticket, and an agent or a person does the work.
- **Not a copy of your data.** Illo searches a source where it lives. It stores summaries, decisions, and notes, with links.
- **Not a second inbox.** It does not send progress messages.
- **Not a remote control for your machines.** Illo holds no credential for them. A collector on each machine pulls the answers.

## The order of the work

1. **The foundation.** The ledger, the gateway, the tool server, and the first safety tests. It is on `next`.
2. **The areas, in parallel.** Each area is built on its own branch and merges into `next` with its tests: the brain service, the collectors, Slack, the asks, the MCP endpoint, GitHub, search, the routines and email, the connectors, the install, and the voice.
3. **A first team.** One team runs Illo from `next` and reports what fails.
4. **The switch.** The branch `main` gets the new system when it can replace the old one.

The README lists what is on `next` today.
