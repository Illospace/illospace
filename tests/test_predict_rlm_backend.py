from types import SimpleNamespace
import inspect
import types
import sys
import pytest


def _fake_openai_llm(*, auth_mode: str, headers: dict[str, str] | None = None):
    base_headers = dict(headers or {})

    def build_request_headers(*, session_id=None, extra_headers=None):
        from brain.platform.integrations.openai_cache import build_openai_extra_headers
        return build_openai_extra_headers(
            base_headers,
            auth_mode=auth_mode,
            session_id=session_id,
            extra_headers=extra_headers,
        )

    return SimpleNamespace(
        provider="openai",
        auth_mode=auth_mode,
        client=object(),
        get_extra_headers=lambda: dict(base_headers),
        build_request_headers=build_request_headers,
    )


def test_get_agent_worker_backend_settings_prefers_predict_rlm_when_ready(monkeypatch):
    from brain.systems.runs.predict_rlm_backend import get_agent_worker_backend_settings

    org = SimpleNamespace(memory_model_config={
        "agent_worker_backend": "auto",
        "predict_rlm_sub_lm": "openai/gpt-5-mini",
        "predict_rlm_max_iterations": 12,
        "predict_rlm_max_llm_calls": 20,
    })

    class FakeSession:
        def get(self, model, identifier):
            name = getattr(model, "__name__", "")
            if name == "Org":
                return org if identifier == "org-1" else None
            if name == "User":
                return None
            return None

    class FakeUoW:
        def __enter__(self):
            self.session = FakeSession()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.systems.runs.predict_rlm_backend.UnitOfWork", FakeUoW)
    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend._predict_rlm_support",
        lambda: {
            "package_available": True,
            "deno_available": True,
            "ready": True,
            "version": "0.2.2",
        },
    )

    settings = get_agent_worker_backend_settings(org_id="org-1", provider="openai")

    assert settings.requested_backend == "auto"
    assert settings.effective_backend == "predict_rlm"
    assert settings.predict_rlm_ready is True
    assert settings.predict_rlm_sub_lm == "openai/gpt-5-mini"
    assert settings.predict_rlm_max_iterations == 12
    assert settings.predict_rlm_max_llm_calls == 20


def test_get_agent_worker_backend_settings_remaps_cross_provider_sub_lm(monkeypatch):
    from brain.systems.runs.predict_rlm_backend import get_agent_worker_backend_settings

    org = SimpleNamespace(memory_model_config={
        "agent_worker_backend": "auto",
        "predict_rlm_sub_lm": "anthropic/claude-haiku-4-5",
    })

    class FakeSession:
        def get(self, model, identifier):
            return org if getattr(model, "__name__", "") == "Org" and identifier == "org-1" else None

    class FakeUoW:
        def __enter__(self):
            self.session = FakeSession()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.systems.runs.predict_rlm_backend.UnitOfWork", FakeUoW)
    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend._predict_rlm_support",
        lambda: {
            "package_available": True,
            "deno_available": True,
            "ready": True,
            "version": "0.2.2",
        },
    )

    settings = get_agent_worker_backend_settings(org_id="org-1", provider="openai")

    assert settings.predict_rlm_sub_lm == "openai/gpt-5-mini"


def test_get_agent_worker_backend_settings_falls_back_when_deno_missing(monkeypatch):
    from brain.systems.runs.predict_rlm_backend import get_agent_worker_backend_settings

    org = SimpleNamespace(memory_model_config={
        "agent_worker_backend": "predict_rlm",
    })

    class FakeSession:
        def get(self, model, identifier):
            name = getattr(model, "__name__", "")
            if name == "Org":
                return org if identifier == "org-1" else None
            if name == "User":
                return None
            return None

    class FakeUoW:
        def __enter__(self):
            self.session = FakeSession()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.systems.runs.predict_rlm_backend.UnitOfWork", FakeUoW)
    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend._predict_rlm_support",
        lambda: {
            "package_available": True,
            "deno_available": False,
            "ready": False,
            "version": "0.2.2",
        },
    )

    settings = get_agent_worker_backend_settings(org_id="org-1")

    assert settings.requested_backend == "predict_rlm"
    assert settings.effective_backend == "native"
    assert settings.predict_rlm_ready is False
    assert settings.fallback_reason == "deno is not installed on the server"


async def test_make_async_tool_wrapper_puts_required_params_before_defaulted_ones():
    from brain.systems.runs.predict_rlm_backend import _make_async_tool_wrapper

    captured = {}

    def handler(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    wrapper = _make_async_tool_wrapper(
        tool_name="mixed_order_tool",
        handler=handler,
        definition={
            "name": "mixed_order_tool",
            "description": "Tool with optional field declared before required field.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "optional_first": {"type": "integer", "default": 7},
                    "required_second": {"type": "string"},
                },
                "required": ["required_second"],
            },
        },
        threadlocal_context={},
        on_tool_call=None,
        run_id=None,
        idea_id=None,
        tool_call_source="worker:test",
    )

    signature = inspect.signature(wrapper)
    assert list(signature.parameters) == ["required_second", "optional_first"]

    result = await wrapper(required_second="hello")

    assert result == {"ok": True}
    assert captured == {
        "optional_first": 7,
        "required_second": "hello",
    }


def test_instrument_predict_rlm_lm_records_internal_calls(monkeypatch):
    from types import SimpleNamespace

    from brain.systems.runs.predict_rlm_backend import _instrument_predict_rlm_lm

    recorded = []

    class FakeLM:
        model = "openai/gpt-5.4"

        def forward(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=11,
                    output_tokens=5,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                )
            )

    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend._record_api_call",
        lambda **kwargs: recorded.append(kwargs),
    )

    lm = _instrument_predict_rlm_lm(
        FakeLM(),
        session_id="sess-123",
        run_id=77,
        label="main",
        call_counter={"turn": 0},
    )

    response = lm.forward(messages=[{"role": "user", "content": "hello"}])

    assert response.usage.input_tokens == 11
    assert len(recorded) == 1
    call = recorded[0]
    assert call["session_id"] == "sess-123"
    assert call["run_id"] == 77
    assert call["turn"] == 1
    assert call["model"] == "openai/gpt-5.4"
    assert call["tokens_input"] == 11
    assert call["tokens_output"] == 5
    assert call["cache_read"] == 0
    assert call["cache_write"] == 0
    assert call["context_messages"] == 1
    assert call["status"] == "success"
    assert call["stop_reason"] == "predict_rlm_main"
    assert call["error"] is None
    assert call["latency_ms"] >= 0


def test_invoke_predict_rlm_agent_returns_internal_usage_totals(monkeypatch):
    from brain.systems.runs.predict_rlm_backend import (
        WorkerBackendSettings,
        invoke_predict_rlm_agent,
    )

    recorded = []
    activities = []

    class FakeInterpreter:
        def __init__(self, **_kwargs):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    class FakeSkill:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeLM:
        def __init__(self, model, usage):
            self.model = model
            self._usage = usage

        def forward(self, **_kwargs):
            return SimpleNamespace(usage=SimpleNamespace(**self._usage))

    class FakePredictRLM:
        def __init__(self, *_args, lm, sub_lm, **_kwargs):
            self.lm = lm
            self.sub_lm = sub_lm

        def __call__(self, *, task):
            self.lm.forward(messages=[{"role": "user", "content": task}])
            self.sub_lm.forward(prompt="inspect")
            return SimpleNamespace(output="done")

    package = types.ModuleType("predict_rlm")
    interpreter_mod = types.ModuleType("predict_rlm.interpreter")
    predict_mod = types.ModuleType("predict_rlm.predict_rlm")
    skills_mod = types.ModuleType("predict_rlm.rlm_skills")
    interpreter_mod.JspiInterpreter = FakeInterpreter
    predict_mod.PredictRLM = FakePredictRLM
    skills_mod.Skill = FakeSkill
    monkeypatch.setitem(sys.modules, "predict_rlm", package)
    monkeypatch.setitem(sys.modules, "predict_rlm.interpreter", interpreter_mod)
    monkeypatch.setitem(sys.modules, "predict_rlm.predict_rlm", predict_mod)
    monkeypatch.setitem(sys.modules, "predict_rlm.rlm_skills", skills_mod)

    def fake_build_lm(*, model, **_kwargs):
        if model.endswith("mini"):
            return FakeLM(model, {
                "input_tokens": 7,
                "output_tokens": 3,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 0,
            })
        return FakeLM(model, {
            "input_tokens": 11,
            "output_tokens": 5,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 1,
        })

    monkeypatch.setattr("brain.systems.runs.predict_rlm_backend._build_predict_rlm_lm", fake_build_lm)
    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend._record_api_call",
        lambda **kwargs: recorded.append(kwargs),
    )

    spec = SimpleNamespace(
        message="task",
        system_prompt="system",
        session_id="sess-123",
        model="openai/gpt-5.5",
        workspace_root=None,
        tools=[],
        tool_handlers={},
        on_tool_call=None,
        on_stream_activity=activities.append,
        run_id=77,
        idea_id="idea-77",
        tool_call_source="worker:test",
    )
    settings = WorkerBackendSettings(
        requested_backend="predict_rlm",
        effective_backend="predict_rlm",
        predict_rlm_package_available=True,
        predict_rlm_deno_available=True,
        predict_rlm_ready=True,
        predict_rlm_version="0.2.2",
        predict_rlm_sub_lm="openai/gpt-5-mini",
        predict_rlm_max_iterations=4,
        predict_rlm_max_llm_calls=6,
    )

    result = invoke_predict_rlm_agent(
        spec,
        provider="openai",
        backend_settings=settings,
        user_id="user-1",
        org_id="org-1",
    )

    assert result.success is True
    assert result.output == "done"
    assert result.tokens_input == 18
    assert result.tokens_output == 8
    assert result.tokens_cache_read == 6
    assert result.tokens_cache_creation == 1
    assert [call["tokens_input"] for call in recorded[:2]] == [11, 7]
    assert recorded[-1]["stop_reason"] == "predict_rlm_summary"
    assert "tokens_input" not in recorded[-1]
    assert len(activities) == 2


def test_build_predict_rlm_lm_uses_openai_api_key_auth(monkeypatch):
    from types import SimpleNamespace

    from brain.systems.runs.predict_rlm_backend import _build_predict_rlm_lm

    captured = {"translated_requests": []}

    class FakeDSPY:
        class LM:
            def __init__(self, model, model_type="chat", cache=True, max_tokens=1000, **kwargs):
                captured["lm_init"] = {
                    "model": model,
                    "model_type": model_type,
                    "cache": cache,
                    "max_tokens": max_tokens,
                    "kwargs": dict(kwargs),
                }
                self.model = model
                self.kwargs = dict(kwargs)

    monkeypatch.setitem(sys.modules, "dspy", FakeDSPY)

    fake_llm = _fake_openai_llm(auth_mode="api_key")

    class FakeProvider:
        def _translate_request(self, request):
            captured["translated_requests"].append(request)
            return {"model": request.normalized_model, "input": request.messages}

        def _create_with_fallback(self, payload):
            captured["payload"] = dict(payload)
            return iter([
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "api-key output"}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "model": "gpt-5.4",
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                        "output": [],
                    },
                },
            ])

    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend.resolve_llm_client",
        lambda **kwargs: fake_llm,
    )
    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend.get_provider",
        lambda provider_name, client: FakeProvider(),
    )

    lm = _build_predict_rlm_lm(
        model="openai/gpt-5.4",
        provider="openai",
        user_id="user-1",
        org_id="org-1",
    )

    response = lm.forward(
        messages=[{"role": "user", "content": "hello"}],
        response_format={"type": "json_schema", "name": "Answer", "schema": {"type": "object"}},
        reasoning_effort="high",
    )

    assert captured["lm_init"] == {
        "model": "openai/gpt-5.4",
        "model_type": "responses",
        "cache": False,
        "max_tokens": None,
        "kwargs": {},
    }
    request = captured["translated_requests"][0]
    assert request.model == "openai/gpt-5.4"
    assert request.messages == [{"role": "user", "content": "hello"}]
    assert request.reasoning_effort == "high"
    assert request.response_format == {"type": "json_schema", "name": "Answer", "schema": {"type": "object"}}
    assert captured["payload"]["stream"] is True
    assert response.output[0].content[0].text == "api-key output"


async def test_build_predict_rlm_lm_uses_provider_backed_codex_auth(monkeypatch):
    from types import SimpleNamespace

    from brain.systems.runs.predict_rlm_backend import _build_predict_rlm_lm

    captured = {"translated_requests": []}

    class FakeDSPY:
        class LM:
            def __init__(self, model, model_type="chat", cache=True, max_tokens=1000, **kwargs):
                captured["lm_init"] = {
                    "model": model,
                    "model_type": model_type,
                    "cache": cache,
                    "max_tokens": max_tokens,
                    "kwargs": dict(kwargs),
                }
                self.model = model
                self.kwargs = dict(kwargs)

    monkeypatch.setitem(sys.modules, "dspy", FakeDSPY)

    fake_llm = _fake_openai_llm(
        auth_mode="chatgpt",
        headers={
            "chatgpt-account-id": "acct_123",
            "originator": "illo-brain",
        },
    )

    class FakeProvider:
        def _translate_request(self, request):
            captured["translated_requests"].append(request)
            return {
                "model": request.normalized_model,
                "input": request.messages,
                "instructions": request.system[0]["text"],
            }

        def _create_with_fallback(self, payload):
            captured["payload"] = dict(payload)
            return iter([
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "codex output"},
                        ],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "model": "gpt-5.4",
                        "usage": {"input_tokens": 11, "output_tokens": 7},
                        "output": [],
                    },
                },
            ])

    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend.resolve_llm_client",
        lambda **kwargs: fake_llm,
    )
    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend.get_provider",
        lambda provider_name, client: FakeProvider(),
    )

    lm = _build_predict_rlm_lm(
        model="openai/gpt-5.4",
        provider="openai",
        user_id="user-1",
        org_id="org-1",
        session_id="sess_123",
    )

    response = lm.forward(
        messages=[
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "hello"},
        ],
        response_format={"type": "json_schema", "name": "Answer", "schema": {"type": "object"}},
        reasoning_effort="high",
    )

    assert captured["lm_init"] == {
        "model": "openai/gpt-5.4",
        "model_type": "responses",
        "cache": False,
        "max_tokens": None,
        "kwargs": {},
    }
    request = captured["translated_requests"][0]
    assert request.messages == [{"role": "user", "content": "hello"}]
    assert request.system == [{"type": "text", "text": "Be precise."}]
    assert request.extra_headers == {
        "chatgpt-account-id": "acct_123",
        "originator": "illo-brain",
        "session_id": "sess_123",
    }
    assert request.response_format == {"type": "json_schema", "name": "Answer", "schema": {"type": "object"}}
    assert captured["payload"]["model"] == "gpt-5.4"
    assert captured["payload"]["instructions"] == "Be precise."
    assert captured["payload"]["stream"] is True
    assert response.output[0].content[0].text == "codex output"
    assert dict(response.usage) == {"input_tokens": 11, "output_tokens": 7}

    async_response = await lm.aforward(messages=[{"role": "user", "content": "async"}])
    assert async_response.output[0].content[0].text == "codex output"


def test_build_predict_rlm_lm_caps_long_codex_session_header(monkeypatch):
    from types import SimpleNamespace

    from brain.platform.integrations.openai_cache import normalize_openai_session_id
    from brain.systems.runs.predict_rlm_backend import _build_predict_rlm_lm

    captured = {"translated_requests": []}

    class FakeDSPY:
        class LM:
            def __init__(self, model, model_type="chat", cache=True, max_tokens=1000, **kwargs):
                self.model = model
                self.kwargs = dict(kwargs)

    monkeypatch.setitem(sys.modules, "dspy", FakeDSPY)

    fake_llm = _fake_openai_llm(
        auth_mode="chatgpt",
        headers={
            "chatgpt-account-id": "acct_123",
            "originator": "illo-brain",
        },
    )

    class FakeProvider:
        def _translate_request(self, request):
            captured["translated_requests"].append(request)
            return {"model": request.normalized_model, "input": request.messages}

        def _create_with_fallback(self, payload):
            return iter([
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "codex output"}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "model": "gpt-5.4",
                        "usage": {"input_tokens": 11, "output_tokens": 7},
                        "output": [],
                    },
                },
            ])

    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend.resolve_llm_client",
        lambda **kwargs: fake_llm,
    )
    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend.get_provider",
        lambda provider_name, client: FakeProvider(),
    )

    session_id = "coordinator-idea-12345678-1234-5678-90ab-cdef12345678:final-reply-checker"
    lm = _build_predict_rlm_lm(
        model="openai/gpt-5.4",
        provider="openai",
        user_id="user-1",
        org_id="org-1",
        session_id=session_id,
    )

    lm.forward(messages=[{"role": "user", "content": "hello"}])

    request = captured["translated_requests"][0]
    assert request.extra_headers["session_id"] == normalize_openai_session_id(session_id)
    assert len(request.extra_headers["session_id"]) <= 64


def test_extract_system_blocks_and_messages_uses_system_messages():
    from brain.systems.runs.predict_rlm_backend import _extract_system_blocks_and_messages

    system_blocks, input_messages = _extract_system_blocks_and_messages(
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "hello"},
        ],
        None,
    )

    assert system_blocks == [{"type": "text", "text": "Be precise."}]
    assert input_messages == [{"role": "user", "content": "hello"}]


def test_build_predict_rlm_lm_uses_anthropic_api_key_auth(monkeypatch):
    from brain.systems.runs.predict_rlm_backend import _build_predict_rlm_lm

    captured = {}

    class FakeDSPY:
        class LM:
            def __init__(self, model, api_key=None, cache=None):
                captured["model"] = model
                captured["api_key"] = api_key
                captured["cache"] = cache

    monkeypatch.setattr(
        "brain.systems.runs.predict_rlm_backend._resolve_key_from_env",
        lambda **kwargs: ("sk-ant-test", "env"),
    )
    monkeypatch.setitem(sys.modules, "dspy", FakeDSPY)

    _build_predict_rlm_lm(
        model="anthropic/claude-sonnet-4-5-20250929",
        provider="anthropic",
        user_id="user-1",
        org_id="org-1",
    )

    assert captured == {
        "model": "anthropic/claude-sonnet-4-5-20250929",
        "api_key": "sk-ant-test",
        "cache": False,
    }
