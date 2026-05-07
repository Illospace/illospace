"""Tests for core/cognition — strategy selection and cognitive frames."""

import pytest
from unittest.mock import patch, MagicMock


class TestSimpleTaskHeuristic:
    """Test the _is_simple_task heuristic (moved from strategy.py to pipeline.py)."""

    def test_simple_tasks(self):
        from brain.systems.runs.task_analysis import is_simple_task
        assert is_simple_task("fix typo in README") is True
        assert is_simple_task("rename the variable") is True

    def test_complex_tasks(self):
        from brain.systems.runs.task_analysis import is_simple_task
        assert is_simple_task("refactor the entire authentication system with new OAuth provider") is False
        assert is_simple_task("investigate the intermittent database connection failures") is False

    def test_task_hash_deterministic(self):
        from brain.systems.runs.task_analysis import task_hash
        h1 = task_hash("fix the bug in login page")
        h2 = task_hash("fix the bug in login page")
        assert h1 == h2

    def test_task_hash_different_tasks(self):
        from brain.systems.runs.task_analysis import task_hash
        h1 = task_hash("fix the login bug")
        h2 = task_hash("deploy to production")
        assert h1 != h2


class TestCognitiveFrames:
    """Test cognitive frame building."""

    def test_build_frame_minimal(self):
        from brain.systems.cognition.frame import build_frame
        frame = build_frame(task="fix the bug")
        assert frame.task_essence == "fix the bug"
        assert frame.confidence > 0
        assert isinstance(frame.heuristics, list)
        assert isinstance(frame.pitfalls, list)

    def test_build_frame_with_skill(self):
        from brain.systems.cognition.frame import build_frame
        skill = {
            "name": "debug",
            "procedure": "1. Read logs\n2. Reproduce bug\n3. Fix",
            "pitfalls": [{"text": "Don't skip log reading", "severity": "high"}],
            "maturity": "proficient",
            "confidence": 0.7,
        }
        frame = build_frame(task="fix the crash", skill=skill)
        assert len(frame.pitfalls) > 0
        assert frame.confidence > 0.3  # skill boosts confidence

    def test_build_frame_with_heuristics(self):
        from brain.systems.cognition.frame import build_frame
        heuristics = [
            {"condition": "when debugging crashes", "action": "check stack trace first", "confidence": 0.8},
        ]
        frame = build_frame(task="fix crash", heuristics=heuristics)
        assert len(frame.heuristics) > 0

    def test_frame_to_system_prompt(self):
        from brain.systems.cognition.frame import CognitiveFrame
        frame = CognitiveFrame(
            task_essence="fix bug",
            context="The app crashes on login",
            heuristics=["when debugging → check logs first"],
            pitfalls=["[high] Don't skip reproduction"],
            confidence=0.7,
        )
        prompt = frame.to_system_prompt()
        assert "check logs first" in prompt
        assert "Don't skip reproduction" in prompt

    def test_confidence_computation(self):
        from brain.systems.cognition.frame import _compute_confidence
        # No skill, no heuristics = low confidence
        c1 = _compute_confidence(None, None, None)
        assert c1 == 0.3

        # Expert skill = high confidence
        c2 = _compute_confidence(
            {"maturity": "expert", "confidence": 0.9},
            [{"confidence": 0.8}],
            [{"content": "test"}],
        )
        assert c2 > c1

    def test_heuristic_compress_fallback(self):
        from brain.systems.cognition.frame import _heuristic_compress
        raw = "# Header\nNEVER do this bad thing\nstep 1: read the file\nsome random text\ncritical warning here"
        compressed = _heuristic_compress(raw, budget_tokens=50)
        assert len(compressed) > 0
        # Should prioritize warnings and steps over random text
        assert "NEVER" in compressed or "critical" in compressed

    # ── Brain unavailability tests ──

    def test_brain_unavailable_injects_pitfall(self):
        from brain.systems.cognition.frame import build_frame
        frame = build_frame(task="fix bug", brain_available=False)
        assert any("brain context unavailable" in p.lower() for p in frame.pitfalls)
        assert frame.pitfalls[0].startswith("[critical]")

    def test_brain_unavailable_reduces_confidence(self):
        from brain.systems.cognition.frame import build_frame
        # Use a skill to get higher base confidence so penalty doesn't hit the floor
        skill = {"name": "debug", "maturity": "expert", "confidence": 0.9,
                 "procedure": "", "pitfalls": []}
        frame_ok = build_frame(task="fix bug", skill=skill, brain_available=True)
        frame_blind = build_frame(task="fix bug", skill=skill, brain_available=False)
        assert frame_blind.confidence < frame_ok.confidence
        # Confidence penalty is 0.3
        assert abs((frame_ok.confidence - frame_blind.confidence) - 0.3) < 0.01

    @patch("brain.app.mcp.server.tool_brain_recall")
    @patch("brain.app.mcp.server.tool_brain_guardrails")
    @patch("brain.systems.cognition.frame.observe_retrieval")
    def test_gather_frame_context_serializes_guardrails(self, mock_observe, mock_guardrails, mock_recall):
        from brain.systems.cognition.frame import gather_frame_context

        mock_recall.return_value = {"memories": []}
        mock_guardrails.return_value = {
            "guardrails": [{"skill": "deploy", "failure": "forgot migrations"}],
            "warnings": ["always verify prod state"],
            "pitfalls": [{"severity": "high", "text": "do not skip rollback plan"}],
        }
        mock_observe.return_value = {"retrieval_decision_id": 123, "stage": "frame_assembly"}

        ctx = gather_frame_context("deploy the fix", skill_name="deploy")

        assert ctx["memory_status"] == "empty"
        assert "[failure] deploy: forgot migrations" in ctx["guardrails"]
        assert "[warning] always verify prod state" in ctx["guardrails"]
        assert "[pitfall:high] do not skip rollback plan" in ctx["guardrails"]
        assert ctx["attention_decision"]["stage"] == "frame_assembly"
        mock_observe.assert_called_once()

    @patch("brain.systems.cognition.frame._lazy_load_enabled", return_value=True)
    @patch("brain.systems.cognition.frame.observe_retrieval")
    @patch("brain.app.mcp.server.tool_brain_guardrails")
    @patch("brain.app.mcp.server.tool_brain_recall")
    def test_gather_frame_context_can_expand_lazy_loads(self, mock_recall, mock_guardrails, mock_observe, _mock_lazy_enabled):
        from brain.systems.cognition.frame import gather_frame_context

        mock_recall.return_value = {
            "memories": [{"id": 1, "content": "selected"}],
            "candidate_memories": [{"id": 1, "content": "selected"}, {"id": 2, "content": "lazy"}],
            "suppressed_memories": [{"id": 2, "content": "lazy"}],
            "lazy_load_memories": [{"id": 2, "content": "lazy"}],
            "lazy_loaded_memories": [{"id": 2, "content": "lazy"}],
            "attention_decision": {"retrieval_decision_id": 55, "stage": "brain_recall"},
        }
        mock_guardrails.return_value = {"guardrails": [], "warnings": [], "pitfalls": []}
        mock_observe.return_value = {"retrieval_decision_id": 77, "stage": "frame_assembly"}

        ctx = gather_frame_context("fix bug", memory_limit=1)

        assert ctx["memory_status"] == "found"
        assert mock_recall.call_args.kwargs["expand_lazy_load"] is True
        assert ctx["attention_decision"]["stage"] == "frame_assembly"

    def test_brain_unavailable_confidence_floor(self):
        """Confidence should not go below 0.1 even with brain penalty."""
        from brain.systems.cognition.frame import build_frame
        # Minimal context → low base confidence (0.3) - 0.3 penalty = 0.1 floor
        frame = build_frame(task="x", brain_available=False)
        assert frame.confidence >= 0.1

    def test_brain_available_no_pitfall(self):
        from brain.systems.cognition.frame import build_frame
        frame = build_frame(task="fix bug", brain_available=True)
        assert not any("brain context unavailable" in p.lower() for p in frame.pitfalls)

    # ── Guardrail pre-injection tests ──

    def test_guardrails_injected_into_frame(self):
        from brain.systems.cognition.frame import build_frame
        guardrails = [
            "[failure] deploy: Migration step skipped",
            "[warning] Always check logs first",
        ]
        frame = build_frame(task="deploy the app", guardrails=guardrails)
        assert any("Migration step skipped" in p for p in frame.pitfalls)
        assert any("check logs" in p for p in frame.pitfalls)

    def test_guardrails_empty_list_no_effect(self):
        from brain.systems.cognition.frame import build_frame
        frame_no_guard = build_frame(task="fix bug", guardrails=[])
        frame_none = build_frame(task="fix bug", guardrails=None)
        assert len(frame_no_guard.pitfalls) == len(frame_none.pitfalls)

    def test_guardrails_appear_in_system_prompt(self):
        from brain.systems.cognition.frame import build_frame
        frame = build_frame(
            task="deploy",
            guardrails=["[failure] deploy: forgot migrations"],
        )
        prompt = frame.to_system_prompt()
        assert "forgot migrations" in prompt

    def test_guardrails_limited_to_5(self):
        from brain.systems.cognition.frame import build_frame
        many_guardrails = [f"[warning] warning {i}" for i in range(10)]
        frame = build_frame(task="test", guardrails=many_guardrails)
        # Should have at most 5 from guardrails (plus any from skill pitfalls)
        guardrail_count = sum(1 for p in frame.pitfalls if "[warning]" in p)
        assert guardrail_count <= 5


class TestPrediction:
    """Test prediction and reward computation."""

    def test_predict_default(self):
        from brain.systems.feedback.predict import predict_outcome
        pred = predict_outcome("fix the bug")
        assert pred.predicted_quality > 0
        assert pred.predicted_tokens > 0
        assert pred.confidence > 0
        assert pred.basis

    def test_predict_different_tasks(self):
        from brain.systems.feedback.predict import predict_outcome
        simple = predict_outcome("fix typo")
        complex_task = predict_outcome("refactor auth")
        assert simple.predicted_tokens <= complex_task.predicted_tokens

    def test_compute_reward_success(self):
        from brain.systems.feedback.predict import Prediction, compute_reward
        pred = Prediction(
            predicted_quality=0.8, predicted_tokens=10000,
            predicted_duration_sec=60, confidence=0.5,
            basis="test",
        )
        reward = compute_reward(pred, actual_tokens=9000, actual_status="completed")
        assert reward.quality == 1.0
        assert reward.efficiency > 0
        assert reward.prediction_error < 1.0

    def test_compute_reward_failure(self):
        from brain.systems.feedback.predict import Prediction, compute_reward
        pred = Prediction(
            predicted_quality=0.9, predicted_tokens=10000,
            predicted_duration_sec=60, confidence=0.7,
            basis="test",
        )
        reward = compute_reward(pred, actual_tokens=15000, actual_status="failed")
        assert reward.quality == 0.0
        assert reward.prediction_error > 0.3  # high error
        assert reward.should_encode is True  # worth learning from

    def test_compute_reward_token_overestimate(self):
        from brain.systems.feedback.predict import Prediction, compute_reward
        pred = Prediction(
            predicted_quality=0.7, predicted_tokens=30000,
            predicted_duration_sec=120, confidence=0.5,
            basis="test",
        )
        reward = compute_reward(pred, actual_tokens=5000, actual_status="completed")
        # Should flag overestimation
        assert reward.efficiency > 1.0  # used fewer tokens than predicted

    def test_insight_on_quality_drop(self):
        from brain.systems.feedback.predict import Prediction, compute_reward
        pred = Prediction(
            predicted_quality=0.9, predicted_tokens=10000,
            predicted_duration_sec=60, confidence=0.8,
            basis="test", skill_name="deploy",
        )
        reward = compute_reward(pred, actual_tokens=10000, actual_status="failed")
        assert reward.insight is not None
        assert "overestimated" in reward.insight.lower()
        assert reward.should_encode is True


class TestHeuristics:
    """Test heuristic extraction and validation logic."""

    def test_gpu_server_parse_json(self):
        """Test JSON parsing from GPU server responses."""
        from brain.systems.feedback.heuristics import _call_gpu_server
        assert callable(_call_gpu_server)

    @patch("brain.systems.feedback.heuristics._call_gpu_server")
    def test_extract_heuristics_success(self, mock_gpu):
        from brain.systems.feedback.heuristics import extract_heuristics
        mock_gpu.return_value = [
            {"condition": "when deploying to prod", "action": "always run smoke tests first"},
        ]
        results = extract_heuristics(
            task="deploy the app",
            skill_name="deploy",
            outcome="Successfully deployed with zero downtime",
            success=True,
        )
        assert len(results) == 1
        assert "deploying" in results[0]["condition"]

    @patch("brain.systems.feedback.heuristics._call_gpu_server")
    def test_extract_heuristics_filters_short(self, mock_gpu):
        from brain.systems.feedback.heuristics import extract_heuristics
        mock_gpu.return_value = [
            {"condition": "short", "action": "x"},  # too short
            {"condition": "when the database is slow and queries timeout", "action": "add index on the query column"},
        ]
        results = extract_heuristics(
            task="fix slow queries", skill_name="debug",
            outcome="Added index", success=True,
        )
        assert len(results) == 1  # short one filtered out

    @patch("brain.systems.feedback.heuristics._call_gpu_server")
    def test_extract_failure_heuristics(self, mock_gpu):
        from brain.systems.feedback.heuristics import extract_heuristics
        mock_gpu.return_value = [
            {"condition": "when running migrations", "action": "always backup first"},
        ]
        results = extract_heuristics(
            task="run migration", skill_name="deploy",
            outcome="Migration failed", success=False,
        )
        assert len(results) == 1


# ── GPU Server Integration ────────────────────────────────────

class TestGPUServerIntegration:
    def test_compress_with_gpu_server_calls_generate(self):
        from brain.systems.cognition.frame import _compress_with_gpu_server
        with patch("brain.platform.gpu_client.get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.generate.return_value = "compressed output"
            mock_get.return_value = mock_client
            result = _compress_with_gpu_server("task", "long context here")
            assert result == "compressed output"
            mock_client.generate.assert_called_once()

    def test_compress_returns_none_on_failure(self):
        from brain.systems.cognition.frame import _compress_with_gpu_server
        with patch("brain.platform.gpu_client.get_client") as mock_get:
            mock_get.return_value.generate.side_effect = RuntimeError("down")
            result = _compress_with_gpu_server("task", "context")
            assert result is None

    def test_call_gpu_server_parses_json(self):
        from brain.systems.feedback.heuristics import _call_gpu_server
        with patch("brain.platform.gpu_client.get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.generate.return_value = '[{"when": "X", "do": "Y", "confidence": 0.8}]'
            mock_get.return_value = mock_client
            result = _call_gpu_server("extract heuristics from...")
            assert isinstance(result, list)
            assert result[0]["when"] == "X"

    def test_call_gpu_server_handles_dirty_json(self):
        from brain.systems.feedback.heuristics import _call_gpu_server
        with patch("brain.platform.gpu_client.get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.generate.return_value = 'Here are results: [{"when": "X", "do": "Y"}] done.'
            mock_get.return_value = mock_client
            result = _call_gpu_server("extract")
            assert isinstance(result, list)
