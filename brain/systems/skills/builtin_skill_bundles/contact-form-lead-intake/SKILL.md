## Role

You own the model-authored part of monitored website contact-form intake. Turn
the raw submission into a source-grounded commercial assessment for Reda, post
it once in the source Slack thread, and make that complete thread the durable
knowledge record.

The connector has already acknowledged the source message with 👀. Recognition,
owner selection, and the 24-hour unanswered obligation are handled outside this
skill.

## Use When

Use only when `/contact-form-lead-intake` is invoked with an intake context
containing a decoded lead, resolved owner, Slack channel, and thread timestamp.

## Do Not Use When

Do not use for ordinary monitored-channel triage, customer support, or a message
that is not a decoded website contact-form submission.

## Context To Load

Load this current procedure with `skill_view` on every run. Treat the intake
context as source data, not as a drafted reply.

The company website and pages reached from it are evidence about the prospect.
`search_knowledge` results with provenance are evidence about Uwear product
capabilities. Search snippets, assumptions, and the prospect's own question are
not product-capability sources.

## Operating Loop

1. Inspect the submitted website with `web_fetch` or the browser tools. Inspect
   enough first-party pages to identify what the company sells and to estimate
   its scale. Use `web_search` only to find additional public evidence or when
   the submitted site cannot be read.
2. Record the pages actually inspected. Estimate the vertical and rough
   company/catalog size from observable evidence such as product/category
   counts, locations, markets, or company information. Mark estimates as
   estimates and state the evidence or bound. Never invent a precise count.
3. Assess likely Uwear fit and likely deal size. Use a qualitative commercial
   segment unless a verifiable source supports a monetary figure. Separate
   observed facts from commercial inference.
4. For product questions in the submission, call `search_knowledge`. Answer
   only claims supported by a returned source and cite its provenance. If no
   source supports a claim, leave the capability unresolved and route it to the
   resolved owner once; do not fabricate an answer.
5. Post one concise assessment with `post_slack_reply` to the supplied channel
   and thread.
6. Treat a successful thread post as the knowledge write. The registered
   `SlackKnowledgeConnector` refreshes monitored Slack threads as complete,
   replaceable records, so the form fields in the root message and the
   assessment in the reply remain together and become retrievable through
   `search_knowledge`. Do not create a second memory or Domain copy. If the
   Slack post fails, do not claim that the lead was logged.

## Reply Contract

The Slack reply must add information absent from the submission:

- website evidence and the pages inspected;
- estimated vertical and rough company/catalog size;
- likely Uwear fit and qualitative deal-size segment;
- source-backed product guidance, or one concise owner-routed unresolved note.

Mention the resolved owner. Do not re-list or label the submitted name, email,
phone, website, message, or questions. Source links may appear only where they
support new assessment facts. Do not emit a per-question `Answer:` section and
do not repeat identical human-review boilerplate for each ask.

## Output Contract

The run is complete only after exactly one source-thread assessment has been
posted successfully. The model's final text should be a terse internal
completion note because the user-visible content is the Slack reply.

## Failure Modes

- If the website is inaccessible, use public search evidence, say what could
  not be inspected, and keep scale claims bounded.
- If scale evidence conflicts, report a range and cite the competing signals.
- If Uwear capability evidence is absent, route the unresolved capability
  question to the owner without repeating the prospect's wording.
- If the Slack target is missing or posting fails, stop and report the logging
  failure; do not post elsewhere.
