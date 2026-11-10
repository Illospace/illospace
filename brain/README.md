# Illo brain service

This service runs one event at a time in each worker process.
It has no dependency on the `illo` package or the ledger.
The gateway owns the run, the principal, the grant, and the final destination.
The Illo MCP server checks each tool call with the run token.

## Runtimes

- `claude` is the default. It uses `claude-agent-sdk==0.2.180` and its bundled Claude CLI.
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

The OpenAI-compatible loop offers only the tools from the authenticated MCP `tools/list` response.
It sends tool results back through Chat Completions and streams the answer.
It refuses a tool name outside that run's catalog.
Each model request uses one turn. The service stops after `max_turns` requests.
MCP tool errors remain tool results, so the model can explain them.
Provider errors end the run. The service does not retry a write.
The wire format follows the [OpenAI function calling guide](https://developers.openai.com/api/docs/guides/function-calling).

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
The service does not add provider-specific parameters beyond streaming usage.
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
| `mcp_url` | The gateway's HTTP MCP endpoint, normally `/brain/mcp`. |
| `mcp_token` | The bearer token for this run. |
| `runtime` | `claude`, `openai_compat`, or `echo`. Default: `claude`. |
| `model` | The model name. Default: `ILLO_BRAIN_MODEL`, then `claude-opus-5-5`. |
| `max_turns` | Maximum model turns, from 1 to 50. Default: 8. |
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
`openai_compat` and `echo` use the MCP names without the Claude `mcp__illo__` prefix.
A run ends with one `final` or `error` event.
An error has `message` and `category`: `auth`, `timeout`, `runtime`, or `limit`.
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
| `ILLO_BRAIN_EFFORT` | `low` | Claude effort. |
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
