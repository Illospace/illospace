"""Tests for task analysis, prediction, and learned heuristics."""

from unittest.mock import MagicMock, patch


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


class TestPrediction:
    """Test prediction and reward computation."""

    async def test_predict_default(self):
        from brain.systems.feedback.predict import predict_outcome
        pred = await predict_outcome("fix the bug")
        assert pred.predicted_quality > 0
        assert pred.predicted_tokens > 0
        assert pred.confidence > 0
        assert pred.basis

    async def test_predict_different_tasks(self):
        from brain.systems.feedback.predict import predict_outcome
        simple = await predict_outcome("fix typo")
        complex_task = await predict_outcome("refactor auth")
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
