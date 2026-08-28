# Meetbot — Illo joins Google Meet, listens, and follows up

*Closed 2026-08-19. Shipped 2026-08-03 (PR #644 + same-day fixes #646–#649,
#651, #655), hardened through 2026-08-12 (#704, #800/#801, #810). E2E-verified
live on a real meeting (run 14442). Ops guide: [`docs/meetings.md`](../../../docs/meetings.md).*

## What it is

A teammate sends Illo a Google Meet link in Slack and asks it to join. Illo
joins as a visible participant named `Illo (notetaker)`, listens (it never
transmits audio or video), and captures a speaker-attributed transcript by
scraping Meet's live captions. One in-meeting action shipped: the
operator-driven `send_meeting_chat` tool posts visible text into the in-call
Meet chat (the stretch goal from the plan). When the meeting ends, a normal
agent run wakes in the same Slack thread: it summarizes, asks clarifying
questions, and files tickets per its standing runtime policy.

Two halves, deliberately separated:

- **`meetbot/`** — its own top-level package, own image (Playwright + headful
  Chromium under Xvfb, compose profile `meetbot`). Entry: `meetbot/app.py`
  (`create_app`, routes `/join`, `/sessions/{id}`, `/sessions/{id}/leave`,
  `/sessions/{id}/chat`, `/healthz`), `meetbot/session.py` (`SessionManager`),
  `meetbot/engine.py` (`PlaywrightMeetEngine`), `meetbot/caption_control.py`
  (`CaptionController`), `meetbot/captions.py` (`RollingCaptionBuffer`),
  `meetbot/session_health.py`, `meetbot/transcript.py`, `meetbot/callback.py`.
- **Brain wiring** — `brain/systems/meetings/` (`client.py`, `inbound.py`,
  `message.py`, `session_record.py`), tools in
  `brain/systems/runs/tool_catalog/{definitions,handlers}/meetings.py`,
  audit table `meetbot_sessions`
  (`brain/platform/db/models/meetbot_session.py`, migration 0061), bridge
  provisioning `brain/app/cli/meetbot_provision.py`.

The seam between the halves is the `MeetEngine`/`SessionEvents` Protocol pair
(`meetbot/models.py`) one way and the `meeting_transcript` /
`meeting_session_health` webhook envelopes the other — which is what lets the
whole service be tested with a faked engine and no browser.

## Why this shape

- **Caption scraping, no STT.** Both prior arts (OpenClaw
  `@openclaw/google-meet`, Hermes `plugins/google_meet`) converged on it for
  v1: no audio pipeline, no virtual devices, and the transcript arrives
  already speaker-attributed.
- **Brain never imports meetbot.** Playwright and Chromium stay out of the api
  image; the halves talk HTTP + webhooks only. Enforced by an AST scan in
  `tests/test_meetbot_session_record.py`, with the `MeetbotSessionOutcome`
  enum duplicated on both sides on purpose and pinned equal by test.
- **UI locale ≠ caption language.** The browser UI defaults to `en-US`
  (`ILLO_MEETBOT_UI_LOCALE`) because
  the DOM selectors are English-text contracts; captions default `fr-FR`
  (`ILLO_MEETBOT_CAPTION_LANGUAGE`) because meetings are French and Meet
  silently *translates* otherwise. The two knobs are independent.
- **Config-with-safe-defaults, no feature gates.** Unset `ILLO_MEETBOT_URL`
  means the tool returns a clear "not configured" error; the compose service
  rides profile `meetbot`.

## Non-goals (v1)

- No speaking, no TTS, no audio processing of any kind.
- No calendar scanning, no auto-join. Explicit links only, one meeting at a
  time.
- No Zoom / Teams.
- **Capture-only on the read side:** nothing reads captions mid-meeting —
  though the transcript jsonl grows live on the shared volume, so a
  Slack-asked run *can* read partial state. On the write side, only the
  operator-driven `send_meeting_chat` exists. The v2 AUTONOMOUS live-action
  design (wake phrase on the caption path → reply in-call) was PARKED by Reda
  2026-08-03 ("use this a bit before going further"); it is preserved in
  closed issue #653 — do not re-propose unprompted.

## Invariants (what must stay true)

1. **Silence is layered, not a single switch.** The browser context is
   created with `permissions=[]` (`engine.py`), the container has no real
   capture hardware, and the launch flags
   `--use-fake-ui-for-media-stream`/`--use-fake-device-for-media-stream` mean
   any stream that did open would carry a synthetic test source, never a
   microphone. The in-code comment calls `permissions=[]` alone "the real
   silence guarantee" — that overstates it, because the fake-UI flag bypasses
   the media prompt; the layering is the guarantee. The mute pass is
   best-effort logging only. Corollary (from live failure #648): **absence of
   a Meet control is a normal state, never an error** — a permissionless bot
   gets no mute button.
2. **Never claim success on page load.** `captions_flowing` is reachable only
   from an observed caption DOM mutation (`session.py` raises otherwise); the
   status graph is a legality table (`_ALLOWED_TRANSITIONS`). Admitted with no
   captions after `ILLO_MEETBOT_CAPTION_WARNING_SECONDS` warns; a zero-caption
   terminal session routes to a **degraded** message that asks no questions
   and never runs the ticket pipeline (`message.py`).
3. **Visible identity is the consent signal.** Default display name
   `Illo (notetaker)`; never masquerade as a human.
4. **One meeting at a time** (`SessionManager` lock → 409 with the active
   session id), **explicit URL only** (regex-validated at the service *and*
   again at ingress `inbound.py`).
5. **A meeting completion is output, not a question.** Meeting metadata
   declares `"obligation": "none"` and open-ask anchors are shape-validated
   (#651 — the open_asks varchar overflow; SQLite tests cannot enforce
   Postgres column widths, a recurring blind spot).
6. **Nothing silently truncates or silently dies.** Transcript inlining is
   bounded (`MAX_TRANSCRIPT_INLINE_CHARS`) with a loud pointer to the full
   file; callbacks retry 3× then dead-letter to disk; unknown engine end
   reasons are a hard `ValueError`, not a shrug; failed envelopes are
   replayable (re-POST `/webhooks` with a bumped idempotency key).
7. **Transcript paths are never caller-controlled** — ingress requires exact
   equality with `brain/uploads/meetings/<session_id>/transcript.{jsonl,md}`.

## Divergences from the plan (worth knowing, not re-deriving)

- **The brain mints `session_id`**, not meetbot (spec said the reverse). The
  durable request row is written before `/join` and the response must echo
  the id (#800/#801) — so a crashed join still leaves an audit row.
- **Signed-in join is REQUIRED in practice**: the Uwear Workspace blocks
  anonymous guests entirely (#649, live discovery). This is an environment
  fact, not a code precondition — the anonymous guest path stays in the code
  on purpose, so a blocked join fails naming Meet's real block instead of a
  local guess. Storage state lives at
  `/data/private/meetbot/google-storage-state.json`, installed via
  `python -m meetbot.auth`.
- **`xvfb-run` was replaced** by an entrypoint that starts Xvfb directly and
  execs uvicorn — its readiness handshake hung silently on the first deploy
  (#647): Xvfb up, uvicorn never started, empty logs.
- **A second envelope, `meeting_session_health`** (#704), was added post-ship:
  periodic health snapshots, stale-session warnings, and a degraded/failed
  distinction the original plan lacked.
- **#645 (engine split) shipped** — `caption_control.py` + `browser_control.py`
  extracted from a 862-line engine.py (pure structural move, in the #810
  train). **#652 closed as diagnostics, not a selector fix**: fr-FR selection
  runs three ordered strategies and, when all miss, emits "Could not confirm
  the caption language" — **live fr-FR confirmation remains unproven**; the
  per-strategy failure evidence (`browser_diagnostics.py`) exists to settle it
  from the next real French meeting.
- Caption dedupe grew identity: `RollingCaptionBuffer` keys on DOM-node
  `line_id` with a `SequenceMatcher` shape fallback — still browser-free
  (pinned by `test_caption_module_does_not_import_playwright`).

## Deployment contract (pinned by `tests/test_safe_deploy.py`)

Enable with `COMPOSE_PROFILES=meetbot` in `deploy/compose/.env`; normal
`./illo update --mode compose` and `./illo deploy upgrade` include meetbot
whenever its profile is enabled or its container is already running.
`/healthz` reports the build commit for the container healthcheck;
`./illo deploy doctor` reads `ILLO_BUILD_COMMIT` off the running container
(not over HTTP) and fails when it does not match the checkout.

## Tests

Service: `meetbot/tests/` (api, captions, engine, session_health, callback,
transcript, config, auth — run inside the backend suite per
`.github/workflows/brain-ci.yml`). Brain: `tests/test_meeting_transcript_inbound.py`,
`tests/test_meeting_tool_handlers.py`, `tests/test_meeting_run_message.py`,
`tests/test_meetbot_session_record.py`, plus the deploy-shape tests in
`tests/test_safe_deploy.py` and `tests/test_deploy_doctor.py`.
