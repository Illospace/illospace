# Meetbot — Illo joins Google Meet, listens, and follows up

Status: v1 build spec. Owner: Reda. Directed by Claude, implemented by Codex.

## Goal

A teammate sends Illo a Google Meet link in Slack and asks it to join. Illo joins the
meeting as a visible participant, listens (never speaks), and captures a
speaker-attributed transcript by scraping Meet's live captions. When the meeting ends,
a normal agent run wakes in the same Slack thread: it summarizes, asks clarifying
questions, announces the tickets it will file and for whom, then files them.

Prior art (both converged on caption scraping for v1): OpenClaw `@openclaw/google-meet`
(transcribe mode) and Hermes `plugins/google_meet` (v1 transcribe-only: Playwright joins,
enables captions, scrapes to a transcript file, session-end hook hands off to agent tools).

## Non-goals (v1)

- No speaking, no TTS, no virtual audio devices.
- No STT / audio processing of any kind. Captions are the transcript source.
- No calendar scanning, no auto-join. Explicit links only, one meeting at a time.
- No Zoom / Teams.

Stretch (in scope only because it is genuinely cheap): posting text into the Meet
in-call chat from the same browser session.

## Architecture

Two halves, connected by HTTP one way and the existing webhook ingress the other way:

```
Slack mention with Meet link
  → normal AgentRun (existing admission)
  → agent calls join_meeting tool ────────HTTP───▶ meetbot service (new container)
                                                     Playwright + headful Chromium + Xvfb
                                                     joins, enables captions, scrapes DOM
                                                     writes transcript to shared uploads volume
  post-meeting AgentRun in same thread ◀─POST /webhooks (kind=meeting_transcript)──┘
  summarizes → asks → announces tickets → files them (all existing tools)
```

### Component 1 — `meetbot/` service (new top-level package, own image)

- Python 3.12, FastAPI + uvicorn, `playwright` (Chromium), runs headful under Xvfb
  inside the container. Own `meetbot/requirements.txt`; Playwright and its Chromium do
  NOT enter the main `requirements.txt` or the api image.
- `deploy/docker/meetbot.Dockerfile`: python base, `playwright install --with-deps chromium`,
  xvfb. Entrypoint runs uvicorn under `xvfb-run`.
- Compose service `meetbot` in `deploy/compose/docker-compose.yml`, profile `["meetbot"]`
  (mirror the `slack-connector` service shape). Mounts:
  - `illo_uploads:/app/brain/uploads` (transcript output, same path convention as api)
  - `illo_private:/data/private` (Google auth storage state, logs)

#### HTTP API (compose-internal, shared-secret auth via `X-Meetbot-Token`)

- `POST /join` `{meeting_url, display_name?, origin: {channel, thread_ts}, requested_by?}`
  → 202 `{session_id, status}`. 409 `{active_session_id}` if a session is active
  (one bot, one meeting — both prior arts enforce this). 422 unless the URL matches
  `https://meet.google.com/[a-z]{3}-[a-z]{4}-[a-z]{3}` (query params tolerated).
- `GET /sessions/{session_id}` → `{session_id, status, meeting_url, joined_at,
  caption_lines, transcript_path, error}`.
- `POST /sessions/{session_id}/leave` → 202.
- `POST /sessions/{session_id}/chat` `{text}` → 202 (stretch; omit cleanly if it
  threatens the schedule).
- `GET /healthz` → 200 (no auth).

#### Session lifecycle — states are load-bearing, copy the lesson from OpenClaw #72478

`starting → lobby → admitted → captions_flowing → ended | failed`

- "Joined" must never be reported on page-load alone. `captions_flowing` requires at
  least one caption DOM mutation observed. If admitted but no caption node appears
  within 90 s, surface that in status (`admitted` + warning field), do not fake success.
- Auth: if a Playwright `storage_state` file exists at
  `/data/private/meetbot/google-storage-state.json`, join authenticated as that Google
  account; else join as anonymous guest with `display_name` (requires host admission).
  Default display name: `Illo (notetaker)` — the bot announces itself by being visibly
  named; never masquerade as a human.
- Join mechanics: launch args include `--use-fake-ui-for-media-stream`
  `--use-fake-device-for-media-stream`; mute mic and camera before joining; fill the
  name field (anonymous path); click "Ask to join" / "Join now"; detect admission
  (in-call UI selector); enable live captions (the "c" keyboard shortcut, with a
  button-click fallback); attach a MutationObserver through `page.expose_function`
  that streams caption updates (speaker name + text) to Python.
- Caption dedupe: Meet progressively rewrites the current caption line. Keep a
  rolling-replace of the in-flight line per speaker and only commit a line when Meet
  starts the next one (the OpenUtter approach). This logic must be a pure function
  with unit tests.
- End detection: leave requested, meeting UI gone (removed / call ended screen), bot
  alone in the call for 5 minutes, or hard cap `ILLO_MEETBOT_MAX_SESSION_SECONDS`
  (default 7200). On end: finalize transcript, then POST the completion envelope
  (below). Callback failures retry with backoff (3 attempts) and land the payload in
  a local dead-letter file under `/data/private/meetbot/` if all fail.

#### Transcript output (shared volume)

`brain/uploads/meetings/<session_id>/`:
- `transcript.jsonl` — one object per committed caption line: `{ts, speaker, text}`.
- `transcript.md` — rendered: header (meeting URL, start/end, participants seen),
  then `**speaker**: text` lines with a timestamp marker every ~5 minutes.
- `session.json` — the session record (status history, counts, origin).

#### Completion callback

POST to `${ILLO_MEETBOT_CALLBACK_URL:-http://api:8000}/webhooks`, bearer token from
`ILLO_MEETBOT_BRIDGE_TOKEN`, envelope kind `meeting_transcript`, payload:
`{session_id, meeting_url, status, transcript_path, transcript_md_path, started_at,
ended_at, caption_lines, participants, origin: {channel, thread_ts}, requested_by,
warning?, error?}`. Idempotency key = `meeting-<session_id>`.

### Component 2 — brain-side wiring (`brain/systems/meetings/` + tool catalog)

1. **Tools** (definitions + handlers + registry entries, mirror the browser/github tool
   pattern): 
   - `join_meeting(meeting_url, display_name?)` — resolves the run's Slack origin
     (channel + thread_ts) from run metadata and passes it as `origin`; calls meetbot
     `POST /join`; then polls status briefly (≤ 10 s) and returns
     `{session_id, status}` with an honest human-readable state. When
     `ILLO_MEETBOT_URL` is unset the tool returns a clear "meetbot service is not
     configured" error (config-with-safe-default, not a feature gate).
   - `meeting_status(session_id)`, `leave_meeting(session_id)`, and (stretch)
     `send_meeting_chat(session_id, text)`.
   - Runs without a Slack origin (e.g. cycles) may still join: `origin` is then empty
     and the post-meeting run routes to the fallback below.
2. **Inbound handler**: register kind `meeting_transcript` in
   `brain/systems/inbound/handlers.py` → `brain.systems.meetings.inbound:process_meeting_transcript_envelope`.
   The handler admits a post-meeting AgentRun routed to `origin.channel/thread_ts`
   (fallback: the configured default/alerts channel when origin is empty). Study
   `brain/systems/app_report/inbound.py` and `brain/systems/slack/inbound.py` for the
   admission pattern. `failed` sessions admit a short run that reports the failure in
   the thread instead of staying silent.
3. **Post-meeting run message**: header (meeting URL, duration, participant list,
   caption-line count), the transcript inlined up to a bounded budget (follow the
   repo's existing truncation-with-pointer conventions — never silently truncate; state
   the full path `transcript.md` for the rest), then instructions: (a) post a summary
   in the thread; (b) ask clarifying questions where decisions or owners are unclear;
   (c) announce the tickets it will file and for whom; (d) file them with
   `create_github_issue` / `add_github_sub_issue` following its standing triage
   playbook. Do NOT re-encode triage/ownership rules in this template — those live in
   runtime policy (doc 1155); the template only sequences the steps.
4. **Bridge-token provisioning**: a small CLI
   (`python -m brain.app.cli.meetbot_provision`) that ensures an external-agent
   connection of kind `meetbot` with scope `signal:submit` and prints a bridge token
   for the meetbot env. Mirror `ensure_slack_connection_for_config`
   (`brain/systems/slack/connector.py:257`) and the external_agents service.

### Config (all safe-by-default: unset = feature dormant, no gates)

| Var | Where | Default | Meaning |
|---|---|---|---|
| `ILLO_MEETBOT_URL` | api/worker | unset | meetbot base URL (`http://meetbot:8010`) |
| `ILLO_MEETBOT_TOKEN` | api/worker + meetbot | unset | shared secret for meetbot HTTP API |
| `ILLO_MEETBOT_BRIDGE_TOKEN` | meetbot | unset | bearer for `POST /webhooks` callback |
| `ILLO_MEETBOT_CALLBACK_URL` | meetbot | `http://api:8000` | brain API base |
| `ILLO_MEETBOT_DISPLAY_NAME` | meetbot | `Illo (notetaker)` | participant name |
| `ILLO_MEETBOT_MAX_SESSION_SECONDS` | meetbot | `7200` | hard session cap |
| `ILLO_MEETBOT_CAPTION_LANGUAGE` | meetbot | `fr-FR` | spoken language used for live-caption recognition |
| `ILLO_MEETBOT_UI_LOCALE` | meetbot | `en-US` | browser UI locale required by English-text selectors |
| `ILLO_MEETBOT_LOBBY_TIMEOUT_SECONDS` | meetbot | `600` | maximum wait for a host to admit the bot |

The UI locale and caption language are independent. Meet's controls stay in English so
the text-based DOM selectors remain deterministic, while captions use the meeting's
spoken language (French by default) to prevent translation, garbage, or empty output.

## Slices

- **S-A (meetbot service)**: everything under "Component 1" + Dockerfile + compose
  entry + `.env.example` additions. Unit tests: caption dedupe (pure function), transcript
  writer, API surface with a faked engine (FastAPI TestClient). No real browser in tests.
- **S-B (brain wiring)**: everything under "Component 2". Unit tests: tool handlers
  (mock HTTP), inbound handler admission (existing test harness patterns in `tests/`).
- **S-C (docs + ops)**: `docs/meetings.md` — Google account bootstrap (create
  `illo@uwear.ai`, generate storage state with a local headed run of
  `python -m meetbot.auth`, copy it to `/data/private/meetbot/` on illo-dev), admission
  gotchas (invite the account to the calendar event to skip the waiting room), consent
  note, provisioning steps (mint bridge token, set env, `--profile meetbot`), and a
  live-verification recipe.

## Acceptance (v1)

1. Mention Illo with a Meet link → Illo replies in thread that it is joining; the bot
   appears in the call named `Illo (notetaker)`; after admission the thread gets a
   "listening, captions flowing" confirmation (via the tool result path).
2. Meeting ends → within ~2 min the same thread gets: summary, clarifying questions,
   ticket plan (title + owner), then filed tickets.
3. A join that never gets admitted, or gets no captions, reports honestly in the
   thread — no silent success.
4. All new unit tests pass; nothing outside the listed files changes behavior.
