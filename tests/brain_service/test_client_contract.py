"""The foundation client and the service must agree on auth and events."""

import pytest

from brain.server import create_app
from illo.brain.client import BrainClient, RunRequest
from tests.brain_service.conftest import serve


async def test_foundation_client_echo_contract(fake_mcp, monkeypatch):
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "acme-service-secret")
    request = RunRequest(
        run_id=42,
        system_prompt="Acme",
        user_message="List the tools",
        mcp_url=fake_mcp.url,
        mcp_token="fake-run-token",
        runtime="echo",
        model="none",
        max_turns=1,
        timeout_s=5,
    )
    async with serve(create_app()) as base:
        client = BrainClient(base)
        events = [item async for item in client.run(request)]
    assert events[0].type == "started"
    assert events[-1].type == "final"
    assert events[-1].run_id == 42
    assert events[-1].usage == {}
    assert "lanes_search" in events[-1].text


async def test_foundation_client_wrong_secret(fake_mcp, monkeypatch):
    import httpx

    monkeypatch.setenv("ILLO_BRAIN_SECRET", "acme-service-secret")
    request = RunRequest(
        run_id=42,
        system_prompt="Acme",
        user_message="List the tools",
        mcp_url=fake_mcp.url,
        mcp_token="fake-run-token",
        runtime="echo",
        model="none",
        max_turns=1,
        timeout_s=5,
    )
    async with serve(create_app()) as base:
        with pytest.raises(httpx.HTTPStatusError) as error:
            _ = [item async for item in BrainClient(base, secret="wrong").run(request)]
    assert error.value.response.status_code == 401


async def test_real_gateway_mcp_catalog(app, rpc_token, monkeypatch):
    import json

    monkeypatch.setenv("ILLO_BRAIN_SECRET", "acme-service-secret")
    run, token = rpc_token
    from illo.tools.registry import ToolContext

    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        expected = [tool["name"] for tool in await ctx.registry.list_tools_for(ToolContext(conn, run, ctx))]
    async with serve(app) as gateway_base, serve(create_app()) as brain_base:
        request = RunRequest(
            run_id=run["id"],
            system_prompt="Acme",
            user_message="List tools",
            mcp_url=gateway_base + "/brain/mcp",
            mcp_token=token,
            runtime="echo",
            model="none",
            max_turns=1,
            timeout_s=5,
        )
        events = [item async for item in BrainClient(brain_base).run(request)]
    assert events[-1].type == "final"
    assert json.loads(events[-1].text) == expected


@pytest.mark.parametrize(
    "category,code,expected",
    [
        ("runtime", "unsupported_parameter", "runtime unsupported_parameter"),
        ("auth", "invalid_api_key", "auth invalid_api_key"),
        ("timeout", "unknown", "timeout unknown"),
        ("runtime", "PRIVATE PROMPT", "runtime unknown"),
    ],
)
async def test_run_manager_keeps_safe_failure(app, replay, category, code, expected):
    from illo.brain.client import BrainEvent

    class FailedBrain:
        async def run(self, request):
            yield BrainEvent(
                type="error", category=category, code=code, message="PRIVATE PROVIDER MESSAGE"
            )

    app.state.ctx.brain_client = FailedBrain()
    await replay()
    async with app.state.ctx.pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")).fetchone()
    assert row["error"] == "The brain run failed: " + expected
    assert row["status"] == ("timeout" if category == "timeout" else "failed")
    assert row["token_hash"] is None
    assert "PRIVATE" not in row["error"]


def test_responses_workspace_and_env_override(monkeypatch):
    from illo.config import Config
    from illo.workspace import BrainConfig

    assert BrainConfig(runtime="openai_responses").runtime == "openai_responses"
    monkeypatch.setenv("ILLO_BRAIN_RUNTIME", "openai_responses")
    assert Config.from_env().brain_runtime == "openai_responses"


def test_s1_default_wire_accepts_a_brain_without_smoke():
    from pydantic import create_model

    from brain.models import RunRequest as ServiceRequest

    request = RunRequest(
        run_id=1, system_prompt="Acme", user_message="Hello", mcp_url="http://test/brain/mcp",
        mcp_token="fake", runtime="openai_responses", model="test", max_turns=1, timeout_s=5,
    )
    fields = {
        name: (field.annotation, field)
        for name, field in ServiceRequest.model_fields.items() if name != "smoke"
    }
    old_request = create_model("OldRequest", __config__=ServiceRequest.model_config, **fields)
    assert old_request.model_validate(request.wire()).run_id == 1
    request.smoke = True
    assert request.wire()["smoke"] is True


TEXT = {"type": "string"}
SECRET = {"format": "password", "type": "string", "writeOnly": True}
OPTIONAL_SECRET = {"anyOf": [SECRET, {"type": "null"}], "default": None}

# The differences between the two copies that exist today. Each entry pins the exact schema
# of each side, so a change on one side still fails. Entry: (client, service, reason).
KNOWN = {
    "run_id": ({"type": "integer"}, {"anyOf": [TEXT, {"type": "integer"}]},
               "The app sends ledger IDs. The service also takes a text ID (its tests use one)."),
    "system_prompt": (TEXT, {**TEXT, "maxLength": 100_000},
                      "The service limits the size at its trust boundary."),
    "user_message": (TEXT, {**TEXT, "maxLength": 100_000},
                     "The service limits the size at its trust boundary."),
    "mcp_url": (TEXT, {**TEXT, "format": "uri", "maxLength": 2083, "minLength": 1},
                "The service checks the URL format."),
    "mcp_token": (TEXT, SECRET, "The service keeps the token out of logs and errors."),
    "runtime": (TEXT, {**TEXT, "default": "claude",
                       "enum": ["claude", "openai_responses", "openai_compat", "echo"]},
                "The service refuses an unknown runtime; the client sends any name."),
    "model": (TEXT, {**TEXT, "maxLength": 128, "minLength": 1},
              "Only the service has a default model (see test_effective_defaults)."),
    "max_turns": ({"type": "integer"},
                  {"type": "integer", "default": 8, "minimum": 1, "maximum": 200},
                  "The app always sends the limit; the service has a default and bounds."),
    "timeout_s": ({"type": "number"},
                  {"type": "number", "default": 300, "exclusiveMinimum": 0, "maximum": 3600},
                  "The app always sends the timeout; the service has a default and bounds."),
    "provider_auth": (
        {"type": "object", "additionalProperties": False, "properties": {
            "kind": {"default": "api_key", "enum": ["api_key", "chatgpt"], "type": "string"},
            "access_token": OPTIONAL_SECRET, "account_id": OPTIONAL_SECRET}},
        {"discriminator": {"propertyName": "kind", "mapping": {
            "api_key": "#/$defs/ApiKeyAuth", "chatgpt": "#/$defs/ChatGPTAuth"}},
         "oneOf": [
            {"type": "object", "additionalProperties": False, "properties": {
                "kind": {"const": "api_key", "default": "api_key", "type": "string"}}},
            {"type": "object", "additionalProperties": False, "properties": {
                "kind": {"const": "chatgpt", "default": "chatgpt", "type": "string"},
                "access_token": SECRET, "account_id": SECRET},
             "required": ["access_token", "account_id"]},
         ]},
        "One client model, two service models. test_wire_inputs.py pins the inputs."),
}
# The client must send these settings. The service has its own defaults for them.
REQUIRED_BY_CLIENT = {"runtime", "model", "max_turns", "timeout_s"}
REQUIRED = {"run_id", "system_prompt", "user_message", "mcp_url", "mcp_token"}


def contract(model) -> tuple[dict, dict]:
    return schema_contract(model.model_json_schema(), client=model is RunRequest)


def schema_contract(schema: dict, *, client: bool) -> tuple[dict, dict]:
    """Give the top-level schema keys, and each field with its references resolved.

    Pydantic makes each title from a name, so the titles are not compared.
    """
    schema = dict(schema)
    definitions = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        keywords = {key: {name: walk(item) for name, item in value.items()}
                    if key == "properties" else walk(value)
                    for key, value in node.items() if key not in ("$ref", "title")}
        if "$ref" not in node:
            return keywords
        # Keep the keywords beside a reference too, for example a bound or a default.
        resolved = walk(definitions[node["$ref"].removeprefix("#/$defs/")])
        return {"$ref": resolved, **keywords} if keywords else resolved

    fields = {name: walk(field) for name, field in schema.pop("properties").items()}
    required = set(schema.pop("required"))
    assert required == REQUIRED | (REQUIRED_BY_CLIENT if client else set())
    return walk(schema), fields


def test_contract_sees_a_keyword_beside_a_reference():
    schema = RunRequest.model_json_schema()
    name, field = next((name, field) for name, field in schema["properties"].items() if "$ref" in field)
    changed = {**schema, "properties": {**schema["properties"], name: {**field, "maxProperties": 1}}}
    assert contract(RunRequest)[1][name] != schema_contract(changed, client=True)[1][name]
    # A property named "title" beside a reference is a field, not a schema title.
    titled = {**field, "properties": {"title": {"type": "string"}}}
    other = {**field, "properties": {"title": {"type": "integer"}}}
    fields = [schema_contract({**schema, "properties": {**schema["properties"], name: value}}, client=True)[1][name]
              for value in (titled, other)]
    assert fields[0] != fields[1]


def test_one_wire_contract_in_two_copies():
    from brain.models import RunRequest as ServiceRequest

    (client_top, client), (service_top, service) = contract(RunRequest), contract(ServiceRequest)
    assert client_top == service_top
    assert list(client) == list(service)
    for name in client:
        expected = KNOWN.get(name, (client[name], client[name], ""))[:2]
        assert (client[name], service[name]) == expected, name


@pytest.mark.parametrize("model_env", [None, "fake-env-model"])
def test_effective_defaults(monkeypatch, model_env):
    """Default factories are not in the schema. Compare the values that the models make."""
    from pydantic import BaseModel

    from brain.models import RunRequest as ServiceRequest

    if model_env is None:
        monkeypatch.delenv("ILLO_BRAIN_MODEL", raising=False)
    else:
        monkeypatch.setenv("ILLO_BRAIN_MODEL", model_env)
    sent = {"run_id": 1, "system_prompt": "Acme", "user_message": "Hello",
            "mcp_url": "http://illo.example/brain/mcp", "mcp_token": "fake-token",
            "runtime": "echo", "model": "fake-model", "max_turns": 3, "timeout_s": 5}
    client = RunRequest(**sent)
    service = ServiceRequest(**{name: sent[name] for name in REQUIRED})

    def effective(request, name):
        value = getattr(request, name)
        return value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value

    for name in set(RunRequest.model_fields) - set(sent):
        assert effective(client, name) == effective(service, name), name
    assert {name: effective(service, name) for name in REQUIRED_BY_CLIENT} == {
        "runtime": "claude", "model": model_env or "claude-opus-5-5", "max_turns": 8,
        "timeout_s": 300}
