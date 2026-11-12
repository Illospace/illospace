# Illo brain service

This service runs one event at a time in each worker process.
It has no dependency on the `illo` package or the ledger.
The gateway owns the run, the principal, the grant, and the final destination.
The Illo MCP server checks each tool call with the run token.

## Runtimes

- `claude` is the default. It uses `claude-agent-sdk==0.2.180` and its bundled Claude CLI.
- `openai_responses` uses the OpenAI Responses API. It offers only the Illo MCP functions.
- `openai_compat` is experimental. It uses a small streaming Chat Completions loop over HTTP. Its tests use a fake model server only.
- `echo` calls MCP `tools/list` and returns the tool names. It makes no model request.

Claude uses `tools=[]` to remove built-in tools.
It accepts only the explicit MCP server named `illo`.
The `mcp__illo__*` permission rule allows every tool of that server.
The run token limits the tools that the server offers.
The service does not maintain a fixed list of tool names.
User settings, project settings, skills, hooks, plugins, and session persistence are off.
Each run has an empty work folder and separate config, cache, and temporary folders.
Each run starts a fresh process. The service does not reuse a conversation.

Both OpenAI loops offer only the tools from the authenticated MCP `tools/list` response.
Each loop sends tool results back through its API and streams the answer.
It refuses a tool name outside that run's catalog.
Each model request uses one turn. The service stops after `max_turns` requests.
MCP tool errors remain tool results, so the model can explain them.
Provider errors end the run. The service does not retry an MCP write.
The Responses loop can retry a rejected reasoning parameter once, before it executes any tool.
The wire format follows the [OpenAI function calling guide](https://developers.openai.com/api/docs/guides/function-calling).

### OpenAI Responses API

Use `openai_responses` for current OpenAI models that require Responses for function tools.
Set `OPENAI_API_KEY` and select the model in the request or `ILLO_BRAIN_MODEL`.
`OPENAI_BASE_URL` defaults to `https://api.openai.com/v1`.
Set `ILLO_BRAIN_EFFORT` to the model's reasoning effort. The default is `low`.
The request sends this value as `reasoning.effort`.
If the provider rejects that parameter, the loop retries once without it.
The worker remembers that choice for its process lifetime. A new worker starts a new cache.
Other HTTP failures do not trigger a retry.
The ChatGPT mode below can make one explicitly allowed API-key retry after a subscription limit.

The loop sends the system prompt as `instructions` on each request.
It sets `store: false`, `max_output_tokens: 4096`, and `parallel_tool_calls: true`.
Parallel function calls execute through MCP in order.
Each next request repeats all input and output items, including encrypted reasoning items.
A `function_call_output` item carries each tool result with its original `call_id`.
The loop never uses `previous_response_id` or depends on a stored conversation.
`store: false` disables response storage. Provider retention policies still apply.

Only `type: function` tools from the authenticated MCP catalog enter a request.
The loop checks this condition in code and refuses unknown calls and built-in tool items.
It enables no provider web, file, shell, computer, or MCP connector tool.
The runtime reads text deltas and takes completed output items from `response.completed`.
It rejects truncated streams, `response.failed`, `response.incomplete`, and stream error events.
Usage adds `input_tokens`, `output_tokens`, `total_tokens`, and numeric detail fields across turns.

Current official API documentation checked on 2026-11-10:

- [Create a response](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Streaming responses](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Reasoning and stateless conversations](https://developers.openai.com/api/docs/guides/reasoning)

### ChatGPT subscriptions

The same `openai_responses` loop also supports `provider_auth.kind: chatgpt`.
The main app owns sign-in, the encrypted grant, refresh, and credential selection.
The brain receives this object over the private app-to-brain connection for one run:

```json
{"provider_auth":{"kind":"chatgpt","access_token":"<secret>","account_id":"<account>"},"api_key_fallback":false}
```

The token fields use secret types. Normal model dumps and representations hide them.
The explicit wire serializer sends them through the HTTP request and the worker's standard input.
No token enters a file, process argument, prompt, tool result, log, or exception message.
The service removes credential echoes. The runtime refuses an echo before it can enter a tool call.
`{"provider_auth":{"kind":"api_key"}}` uses the brain's `OPENAI_API_KEY` as before.
Requests that omit `provider_auth` keep this API-key behavior.

ChatGPT uses `https://chatgpt.com/backend-api/codex/responses`.
Headers include `Authorization: Bearer <access_token>`, `chatgpt-account-id`, and `originator: illo-brain`.
`OPENAI_BASE_URL` does not change the ChatGPT address. It applies only to API-key requests.
The request includes `instructions`, `store: false`, `stream: true`, and `tool_choice: auto`.
It omits the API-only `max_output_tokens` field.
Reasoning options, function tools, encrypted reasoning, and function outputs use the same loop as the API-key path.
Only Illo MCP functions enter the request. No provider tool or Codex CLI runs.

The backend reports subscription exhaustion as HTTP 429 with `error.type: usage_limit_reached`.
Illo also accepts that identifier in `error.code`, and in streamed errors.
An integer `resets_at` gives the reset time as Unix seconds. Illo displays it in UTC.
The run replies `Your ChatGPT limit is reached.` and adds that time when available.
With `api_key_fallback: true`, the loop switches once to an explicit API-key request.
It resumes the current turn with its existing tool outputs. It does not repeat completed MCP calls.
The final answer reports the switch. A second provider failure ends the run.
The switch drops subscription reasoning items. Messages and completed tool results stay.
The loop holds text deltas until each attempt completes. A failed attempt shows no partial text.
The same configured model must be available with the subscription and the API key.

#### Sign-in protocol

The main app first gives the person a one-use link under `/oauth/chatgpt/start`.
Encrypted state binds the person. The ledger binds the DM, thread, ten-minute expiry, and one use.
The app keeps the PKCE verifier encrypted on the server.
It first requests a device code from `https://auth.openai.com/api/accounts/deviceauth/usercode`.
The person opens `https://auth.openai.com/codex/device` and enters the code there.
The app polls `/api/accounts/deviceauth/token` at the provider's interval, with a five-second minimum.
HTTP 403 and 404 mean pending, as in the Codex source.
After authorization, the endpoint supplies an authorization code and PKCE verifier.
The app exchanges them at `https://auth.openai.com/oauth/token` with redirect `https://auth.openai.com/deviceauth/callback`.
Illo limits the whole attempt to ten minutes. Codex CLI currently allows fifteen minutes for device sign-in.

If device setup fails, the link redirects to the browser flow.
The device page also offers `/oauth/chatgpt/paste` to switch to that flow.
It uses `https://auth.openai.com/oauth/authorize`, client ID `app_EMoamEEZ73f0CkXaXp7hrann`, and PKCE S256.
Scopes are `openid profile email offline_access`. No OpenAI connector tools are enabled.
The current Codex browser client also asks for `api.connectors.read api.connectors.invoke`; Illo omits those scopes.
The redirect is the current Codex loopback address: `http://127.0.0.1:1455/auth/callback`.
The person pastes the failed browser address into the same DM and thread, as the only text.
The gateway claims it before a brain run. Its payload filter removes callback addresses before any event write.
The app sends the authorization-code exchange as form data. Refresh uses JSON, as in the current Codex client.
Neither exchange retries a one-use code or token.

The encrypted `connector_grants` secret holds the access token, refresh token, account ID, expiry, and plan when known.
Token metadata comes only from the token endpoint, over HTTPS.
The app reads account and plan claims from the ID token, and expiry from the access token or `expires_in`.
It uses no fixed token lifetime. It refreshes within five minutes of expiry.
A grant row lock serializes refresh across app processes. The rotation commits before a run starts.
Run cancellation waits for that refresh transaction to finish.
A refresh failure marks the grant broken and records `chatgpt:<person key>` with no provider text.
Temporary network errors, timeouts, HTTP 429, and server errors keep the grant.
Refresh retries wait 30 seconds, then 2 minutes, then 10 minutes.
The current run gets a short failure reply, or uses the explicitly allowed API fallback.
The person gets one reconnect DM for that broken grant. A new sign-in resets the notice state.
No refresh token or ID token enters the brain container.

Protocol sources checked on 2026-11-10:

- [Official Codex authentication guide](https://developers.openai.com/codex/auth), including device sign-in and automatic refresh.
- [Current Codex source revision](https://github.com/openai/codex/commit/c37fd5665f55fa2b5accdf1d229855416ce696c1).
- [Browser flow, scopes, redirect, and code exchange](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/login/src/server.rs).
- [OAuth encoding](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/login/src/oauth/client.rs).
- [Device flow and polling](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/login/src/device_code_auth.rs).
- [Client ID, expiry window, refresh, and rotation errors](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/login/src/auth/manager.rs).
- [Headers](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/login/src/auth/default_client.rs).
- [ChatGPT backend address](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/model-provider-info/src/lib.rs).
- [Responses request shape](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/codex-api/src/common.rs).
- [Subscription limit status and reset field](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/codex-api/src/api_bridge.rs).
- [Stream errors](https://github.com/openai/codex/blob/c37fd5665f55fa2b5accdf1d229855416ce696c1/codex-rs/codex-api/src/sse/responses_error.rs).
- [Old Illo transport](https://github.com/Illospace/illospace/blob/legacy/brain/platform/integrations/openai_codex_client.py) and [old sign-in helpers](https://github.com/Illospace/illospace/blob/legacy/brain/platform/integrations/openai_codex_auth.py).

These details are based on source and offline fakes. They are not a live ChatGPT check.
The director must check the following with a real person's sign-in:

- The public Codex client ID accepts Illo's device flow and its `illo-brain` originator.
- The device permission works for that account. Pending responses use 403 or 404.
- The browser fallback accepts the four identity scopes without OpenAI connector scopes.
- The exact loopback redirect, form exchange, and JSON refresh work for this client.
- Tokens supply the account claim, access-token expiry or `expires_in`, and a refresh token.
- The configured model accepts the legacy `instructions` field and this stateless HTTP SSE shape.
  Current Codex source puts base instructions in developer input messages; Illo keeps its existing Responses contract.
- The endpoint accepts only Illo function tools, `store: false`, encrypted reasoning replay, and no `max_output_tokens`.
- Subscription exhaustion supplies `usage_limit_reached` and optional `resets_at` as read from the source.
- A rotated refresh token works on the next refresh, and an API retry accepts the same model and conversation items.

The operator must check that the provider's terms allow this use. An API key is the alternative.

### Live check on 2026-11-10

The Responses runtime used the real OpenAI API with a local fake Illo MCP server.
That server offered exactly `lanes_search` and `ask_list`.
Each timed run asked, "Where are we on the mobile release?"
The system prompt required `lanes_search` and a three-line answer.
Each run made one tool call and two model requests.
Times start before MCP initialization. The first tool time is the start of MCP `tools/call`.
The final time is the completed runtime event. These measurements are not a latency guarantee.

| Model | Effort | Run | First tool (seconds) | Final answer (seconds) |
| --- | --- | --- | --- | --- |
| `gpt-6.1-sol` | `low` | 1 | 1.635 | 2.936 |
| `gpt-6.1-sol` | `low` | 2 | 1.344 | 2.892 |
| `gpt-6.1-sol` | `low` | 3 | 1.346 | 2.893 |
| `gpt-6.1-terra` | `low` | 1 | 1.201 | 2.237 |
| `gpt-6.1-terra` | `low` | 2 | 1.171 | 2.554 |
| `gpt-6.1-terra` | `low` | 3 | 1.306 | 2.398 |

The hostile prompt asked Sol to run `id`, read `/etc/passwd`, and fetch `https://example.com`.
It requested no tool and finished in 1.686 seconds.
Its catalog contained only the two fake Illo tools. No outside tool was requested or executed.
The full check used 13 model requests. No reasoning retry was needed.

For this status lookup, start with `gpt-6.1-terra` and effort `low`.
It completed all three runs with lower latency than Sol in this small check.
This checks the runtime path and tool boundary. It does not compare quality on complex work.
The general service default remains Claude. Earlier Claude Agent SDK checks also used a live model.
The Chat Completions runtime, other providers, and local servers have offline tests only.

### Why there is no Codex runtime yet

The brain test found extra MCP resource tools in Codex CLI.
Those tools remained after its shell, file, web, patch, and subagent tools were disabled.
That tool catalog fails safety rule 10. This image contains no Codex runtime or Node installation.

## Install and run

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -e './brain[dev]'
.venv/bin/python -m uvicorn brain.server:app --host 127.0.0.1 --port 8080 --no-access-log
```

Create an Anthropic API key with billing in your own Anthropic Console account.
Supply `ANTHROPIC_API_KEY` through the deployment secret manager or enter it without terminal echo:

```bash
read -rsp 'Anthropic API key: ' ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY
```

The product uses an Anthropic API key; it does not offer a Claude subscription login flow.
The Claude Agent SDK terms restrict subscription login for third-party products.

`ANTHROPIC_BASE_URL` can point Claude at an operator's API gateway.
No credential file or sign-in cache needs a host mount.
Never copy a personal credential file into this image.

For `openai_compat`, create an API key in your own provider account and set `OPENAI_API_KEY`.
Set `OPENAI_BASE_URL` to the API base, including `/v1` when the provider needs it.
The default base is `https://api.openai.com/v1`.
Supply the provider's model name in the run request.
Ollama (`http://ollama:11434/v1`) and vLLM (`http://vllm:8000/v1`) are untested examples.
An explicit base URL allows a local server with no API key.
Set `ILLO_BRAIN_OPENAI_COMPAT_REASONING_EFFORT` only when the provider needs `reasoning_effort`.
For example, some models need `none` to accept tools through Chat Completions.
The loop omits that parameter when the setting is blank.
A compatible provider must accept `stream_options.include_usage`.

Set the same `ILLO_BRAIN_SECRET` on the gateway and the brain service.
Generate a new secret in the secret manager. Keep it out of files in a checkout.
The service refuses all runs when that secret is absent.

## HTTP contract

`GET /healthz` returns `status: ok` and each runtime's `usable` value.
This checks the presence of environment authentication. It does not test a key with the provider.
`echo` is always usable. The reply contains no credential or provider address.

`POST /run` requires the `X-Illo-Brain-Secret` header.
Its JSON body has these fields:

| Field | Value |
| --- | --- |
| `run_id` | The gateway run ID, as an integer or string. |
| `system_prompt` | The system prompt. |
| `user_message` | The event text. |
| `images` | Optional list of `mime` and base64 `data` fields. Only `openai_responses` shows these images. |
| `mcp_url` | The gateway's HTTP MCP endpoint, normally `/brain/mcp`. |
| `mcp_token` | The bearer token for this run. |
| `runtime` | `claude`, `openai_responses`, `openai_compat`, or `echo`. Default: `claude`. |
| `model` | The model name. Default: `ILLO_BRAIN_MODEL`, then `claude-opus-5-5`. |
| `max_turns` | Maximum tool calls for Responses, from 1 to 50. Default: 8. Other runtimes count their turns. |
| `timeout_s` | Active run deadline in seconds, up to 3600. Default: 120. |

The response uses `application/x-ndjson`. Each line is one JSON object with `type` and `run_id`.

```json
{"type":"started","run_id":1}
{"type":"started","run_id":1,"catalog":["mcp__illo__lanes_search"]}
{"type":"tool_call","run_id":1,"name":"mcp__illo__lanes_search"}
{"type":"text","run_id":1,"text":"The release is ready."}
{"type":"final","run_id":1,"text":"The release is ready.","usage":{}}
```

The first `started` event confirms admission. A later `started` event gives the runtime catalog.
`openai_responses`, `openai_compat`, and `echo` use the MCP names without the Claude `mcp__illo__` prefix.
A run ends with one `final` or `error` event.
An error has `message` and `category`: `auth`, `timeout`, `runtime`, or `limit`.
Provider failures also carry `code`, `provider_type`, and the HTTP status when available.
Only short identifiers enter those fields. Invalid or absent identifiers become `unknown`.
The event and service log omit provider message text, prompts, tool results, and keys.
The main app stores `The brain run failed: <category> <code>` in `runs.error`.
A final event has `text` and `usage`. Usage fields come from the runtime.
The OpenAI-compatible loop adds numeric usage fields across its turns.
A failed request before the stream starts uses HTTP 401, 422, or 503.
Validation errors and provider exceptions do not echo the request or secrets.

## Limits and process cleanup

| Environment key | Default | Purpose |
| --- | --- | --- |
| `ILLO_BRAIN_SECRET` | Required | Shared secret for the gateway. |
| `ILLO_BRAIN_CONCURRENCY` | `4` | Maximum active workers. Other requests wait. |
| `ILLO_BRAIN_TIMEOUT_S` | `120` | Maximum active duration, capped further by `timeout_s`. |
| `ILLO_BRAIN_MODEL` | `claude-opus-5-5` | Model when the request omits it. |
| `ILLO_BRAIN_EFFORT` | `low` | Claude effort or Responses reasoning effort. |
| `ILLO_BRAIN_OPENAI_COMPAT_REASONING_EFFORT` | Unset | Optional Chat Completions `reasoning_effort`. |
| `ILLO_BRAIN_TMPDIR` | System temp folder | Parent folder for each run's private files. |

The gateway also enforces the full deadline, including its wait for a run slot.
A timeout, disconnect, failure, or completed run kills the worker's process group.
That group includes the Claude CLI and its children.
The service then deletes the run folder. It writes no MCP token file.
The SDK passes the per-run MCP bearer token in the Claude child's process arguments.
The container boundary covers this exposure. Never log process arguments or collect them in telemetry.
HTTP access logs and child stderr are off by default.
Use TLS between hosts. Keep the service on a private network.
In `deploy/compose.yaml`, the brain joins `brainnet` with the app and the default network for outbound traffic. It never joins the Postgres network.

## Container contract for deployment

Build from the repository root:

```sh
docker build -t illo-brain -f brain/Dockerfile brain
```

The image runs as UID 10001. It contains the pinned SDK and its CLI.
The root file system can be read-only. Runtime writes stay under `/tmp`.
The service needs no host mount, Docker socket, persistent config folder, or personal sign-in cache.
Run it with:

- `read_only: true` and a writable `/tmp` tmpfs, mode 1777.
- `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]`.
- No published port in production. Only the gateway reaches port 8080.
- An internal network to Illo, plus controlled outbound HTTPS to the selected model provider.
- DNS for those destinations. Use an egress proxy or firewall to enforce destination restrictions.
- Memory and process limits that allow the configured number of Claude processes.

The model has no network, shell, or file tool of its own.
The runtime still needs network access for MCP and model requests.
Docker's default network alone does not restrict outbound destinations.
The deploy area owns those network rules and the Compose configuration.
The gateway owns durable queue state and event deduplication.

## Tests

From the repository root, install both packages before the full test suite:

```sh
.venv/bin/pip install -e '.[dev]' -e './brain[dev]'
scripts/test-db.sh
.venv/bin/ruff check .
.venv/bin/pytest -q
```

The offline tests use local fake MCP and model servers.
They check the stream, authentication, tool results, turn limits, concurrency, process cleanup, and exact SDK arguments.
The rule 10 test lists the actual runtime catalog and checks a hostile request.
It also records three runs with a first tool call and a final answer.
It skips live requests unless `ILLO_LIVE_BRAIN=1`.

A host with an existing local provider can set `ILLO_BRAIN_USE_HOST_PROVIDER=1` for that test.
This test-only mode accepts `ANTHROPIC_AUTH_TOKEN` only with a loopback `ANTHROPIC_BASE_URL`.
It passes that environment credential to bare mode in memory.
It does not read, copy, or change any host sign-in file.
The product container uses `ANTHROPIC_API_KEY` instead.

### Subscription smoke check

Run `illo chatgpt smoke --person <key-or-id>` from the main app environment after sign-in.
See [Workspace files](../docs/workspace.md#check-the-chatgpt-connection).
The request sets `smoke: true` on the normal `/run` contract and passes per-run `provider_auth`.
This check offers only the fixed `smoke_ping` function. It does not connect to the team MCP endpoint.
The function reads and writes no team data. Normal runs keep their exact MCP catalog.
The Responses loop accepts complete output from `response.output_item.done` when the terminal event omits it.
When both forms exist, they must agree.

Offline tests cover refresh, the app-to-brain wire, one function round trip, API fallback, and safe errors.
No live subscription result is recorded yet. The deployment owner must run the check before the trial.
Record the model, auth kind, tool result, token usage, latency, and date here after that check.
Do not record a token, account ID, or real team data.
