from contextlib import nullcontext
from types import SimpleNamespace


def _make_worker(monkeypatch):
    from brain.platform.gpu.workers import llm as llm_worker
    from brain.platform.gpu.config import WorkerManifest

    monkeypatch.setattr(
        llm_worker,
        "torch",
        SimpleNamespace(no_grad=nullcontext),
    )

    m = WorkerManifest(name="llm", model_path="Qwen/Qwen3.5-4B", vram_mb=3000)
    w = llm_worker.LLMWorker(m)

    class FakeInputIds:
        shape = [1, 3]
        def to(self, device):
            return self

    fake_input_ids = FakeInputIds()

    class FakeTokenizer:
        def __call__(self, text, return_tensors=None, truncation=None, max_length=None):
            return {"input_ids": fake_input_ids}
        def decode(self, ids, **kw):
            return self._decode_text
        @property
        def eos_token_id(self):
            return 0
        _decode_text = "generated text"

    class FakeModel:
        def generate(self, **kw):
            return [[1, 2, 3, 4, 5]]

    w.tokenizer = FakeTokenizer()
    w.model = FakeModel()
    return w


class TestLLMWorker:
    def test_worker_instantiates(self):
        from brain.platform.gpu.workers.llm import LLMWorker
        from brain.platform.gpu.config import WorkerManifest
        m = WorkerManifest(name="llm", model_path="Qwen/Qwen3.5-4B", vram_mb=3000)
        w = LLMWorker(m)
        assert w.manifest.name == "llm"

    async def test_handle_request_returns_text(self, monkeypatch):
        w = _make_worker(monkeypatch)
        result = await w.handle_request({
            "prompt": "test prompt",
            "max_tokens": 100,
            "temperature": 0.3,
        })
        assert "text" in result
        assert result["text"] == "generated text"
        assert "elapsed_ms" in result

    async def test_think_false_strips_thinking_tags(self, monkeypatch):
        """When think=False, <think>...</think> blocks are stripped from output."""
        w = _make_worker(monkeypatch)
        w.tokenizer._decode_text = "<think>internal reasoning here</think>Clean title output"
        result = await w.handle_request({
            "prompt": "generate a title",
            "max_tokens": 20,
            "temperature": 0.3,
            "think": False,
        })
        assert result["text"] == "Clean title output"
        assert "<think>" not in result["text"]

    async def test_think_true_preserves_thinking_tags(self, monkeypatch):
        """When think=True, thinking tags are preserved."""
        w = _make_worker(monkeypatch)
        w.tokenizer._decode_text = "<think>reasoning</think>Answer"
        result = await w.handle_request({
            "prompt": "test",
            "max_tokens": 100,
            "think": True,
        })
        assert "<think>" in result["text"]
        assert "reasoning" in result["text"]

    async def test_think_false_no_tags_passthrough(self, monkeypatch):
        """When think=False but no tags present, output is unchanged."""
        w = _make_worker(monkeypatch)
        w.tokenizer._decode_text = "Just a clean response"
        result = await w.handle_request({
            "prompt": "test",
            "max_tokens": 100,
            "think": False,
        })
        assert result["text"] == "Just a clean response"

    async def test_think_false_multiline_thinking(self, monkeypatch):
        """Multiline thinking blocks are fully stripped."""
        w = _make_worker(monkeypatch)
        w.tokenizer._decode_text = "<think>\nline1\nline2\nline3\n</think>\nFinal answer"
        result = await w.handle_request({
            "prompt": "test",
            "max_tokens": 100,
            "think": False,
        })
        assert result["text"] == "Final answer"

    async def test_empty_prompt_returns_error(self, monkeypatch):
        w = _make_worker(monkeypatch)
        result = await w.handle_request({"prompt": ""})
        assert result == {"error": "prompt required"}
