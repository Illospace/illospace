"""Tests for brain/systems/feedback/meta_evolution — meta-evolution metrics and tuning.

Migrated to SQLAlchemy ORM: functions use UnitOfWork internally,
no raw cursor is passed in.
"""

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


class TestEvolutionMetrics:
    """Test evolution metrics dataclass and basic logic."""

    def test_metrics_dataclass_fields(self):
        from brain.systems.feedback.meta_evolution import EvolutionMetrics
        m = EvolutionMetrics(period_start=date.today(), period_end=date.today())
        assert hasattr(m, "prediction_accuracy")
        assert hasattr(m, "heuristic_survival_rate")
        assert hasattr(m, "strategy_success_rates")
        assert hasattr(m, "skill_fitness_trend")
        assert hasattr(m, "memory_retrieval_quality")

    def test_metrics_defaults(self):
        from brain.systems.feedback.meta_evolution import EvolutionMetrics
        m = EvolutionMetrics(period_start=date.today(), period_end=date.today())
        assert m.prediction_accuracy == 0.0
        assert m.total_runs == 0
        assert m.strategy_success_rates == {}

    def test_insight_dataclass(self):
        from brain.systems.feedback.meta_evolution import MetaInsight
        i = MetaInsight(
            category="prediction", severity="regression",
            message="Accuracy dropped", metric_name="prediction_accuracy",
            current_value=0.5, previous_value=0.8,
            suggested_action="Recalibrate",
        )
        assert i.category == "prediction"
        assert i.suggested_action == "Recalibrate"


class TestComputeEvolutionMetrics:
    """Test compute_evolution_metrics uses UnitOfWork (no cur param)."""

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_uses_unit_of_work_internally(self, mock_uow_cls):
        """Function should create its own UnitOfWork, not accept a cursor."""
        from brain.systems.feedback.meta_evolution import compute_evolution_metrics

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        # All queries return empty results
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_result.mappings.return_value.all.return_value = []
        mock_uow.session.execute.return_value = mock_result

        result = compute_evolution_metrics()
        assert result.period_end == date.today()
        assert result.total_runs == 0
        mock_uow.session.execute.assert_called()

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_prediction_accuracy_from_db(self, mock_uow_cls):
        """Test that prediction metrics are extracted from query results."""
        from brain.systems.feedback.meta_evolution import compute_evolution_metrics

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        call_num = [0]
        def side_effect(*args, **kwargs):
            call_num[0] += 1
            mock_result = MagicMock()
            if call_num[0] == 1:
                # Prediction query
                mock_result.mappings.return_value.first.return_value = {
                    "total": 10, "avg_error": 0.2, "avg_quality_error": 0.1,
                    "calibration_gap": 0.15,
                }
            else:
                mock_result.mappings.return_value.first.return_value = None
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_uow.session.execute.side_effect = side_effect

        result = compute_evolution_metrics()
        assert result.prediction_accuracy == pytest.approx(0.8)
        assert result.prediction_calibration == pytest.approx(0.85)
        assert result.total_runs == 10

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_uses_named_params_not_percent_s(self, mock_uow_cls):
        """Verify SQL uses :named_params instead of %s."""
        from brain.systems.feedback.meta_evolution import compute_evolution_metrics

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_result.mappings.return_value.all.return_value = []
        mock_uow.session.execute.return_value = mock_result

        compute_evolution_metrics()

        # Check that execute calls use dict params (named), not tuple params (%s)
        for c in mock_uow.session.execute.call_args_list:
            args, kwargs = c
            if len(args) > 1:
                assert isinstance(args[1], dict), \
                    f"Expected dict params (named), got {type(args[1])}: {args[1]}"

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_accepts_period_end_and_window(self, mock_uow_cls):
        """Function should still accept period_end and window_days kwargs."""
        from brain.systems.feedback.meta_evolution import compute_evolution_metrics

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_result.mappings.return_value.all.return_value = []
        mock_uow.session.execute.return_value = mock_result

        end_date = date(2026, 3, 20)
        result = compute_evolution_metrics(period_end=end_date, window_days=14)
        assert result.period_end == end_date
        assert result.period_start == date(2026, 3, 6)


class TestComparePeriods:
    """Test period comparison and insight generation."""

    def test_detects_prediction_regression(self):
        from brain.systems.feedback.meta_evolution import compare_periods, EvolutionMetrics

        call_count = [0]
        def mock_compute(period_end=None, window_days=7):
            call_count[0] += 1
            if call_count[0] == 1:  # current period
                return EvolutionMetrics(
                    period_start=date.today() - timedelta(days=7),
                    period_end=date.today(),
                    prediction_accuracy=0.5,
                    total_runs=10,
                )
            else:  # previous period
                return EvolutionMetrics(
                    period_start=date.today() - timedelta(days=14),
                    period_end=date.today() - timedelta(days=7),
                    prediction_accuracy=0.8,
                    total_runs=10,
                )

        with patch("brain.systems.feedback.meta_evolution.compute_evolution_metrics", side_effect=mock_compute):
            insights = compare_periods()

        pred_insights = [i for i in insights if i.category == "prediction"]
        assert len(pred_insights) >= 1
        assert pred_insights[0].severity == "regression"

    def test_detects_strategy_regression(self):
        from brain.systems.feedback.meta_evolution import compare_periods, EvolutionMetrics

        call_count = [0]
        def mock_compute(period_end=None, window_days=7):
            call_count[0] += 1
            if call_count[0] == 1:
                return EvolutionMetrics(
                    period_start=date.today() - timedelta(days=7),
                    period_end=date.today(),
                    total_runs=10,
                    strategy_success_rates={"habitual": 0.4},
                )
            else:
                return EvolutionMetrics(
                    period_start=date.today() - timedelta(days=14),
                    period_end=date.today() - timedelta(days=7),
                    total_runs=10,
                    strategy_success_rates={"habitual": 0.8},
                )

        with patch("brain.systems.feedback.meta_evolution.compute_evolution_metrics", side_effect=mock_compute):
            insights = compare_periods()

        strat_insights = [i for i in insights if i.category == "strategy"]
        assert len(strat_insights) >= 1
        assert "habitual" in strat_insights[0].message

    def test_no_insights_when_stable(self):
        from brain.systems.feedback.meta_evolution import compare_periods, EvolutionMetrics

        def mock_compute(period_end=None, window_days=7):
            return EvolutionMetrics(
                period_start=date.today() - timedelta(days=7),
                period_end=date.today(),
                prediction_accuracy=0.8,
                total_runs=10,
                strategy_success_rates={"deliberative": 0.8},
                heuristic_survival_rate=0.9,
                total_heuristics_active=20,
            )

        with patch("brain.systems.feedback.meta_evolution.compute_evolution_metrics", side_effect=mock_compute):
            insights = compare_periods()

        assert len(insights) == 0

    def test_no_cursor_param(self):
        """compare_periods() should not accept a cur parameter."""
        import inspect
        from brain.systems.feedback.meta_evolution import compare_periods
        sig = inspect.signature(compare_periods)
        assert "cur" not in sig.parameters


class TestAutoTune:
    """Test automatic parameter tuning uses UnitOfWork."""

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_relaxes_pruning_on_low_survival(self, mock_uow_cls):
        from brain.systems.feedback.meta_evolution import auto_tune_parameters, MetaInsight

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        insight = MetaInsight(
            category="heuristic", severity="warning",
            message="Low survival", metric_name="heuristic_survival_rate",
            current_value=0.3, previous_value=0.8,
        )
        adjustments = auto_tune_parameters([insight])
        assert "heuristic_prune_threshold" in adjustments

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_escalates_failing_strategy(self, mock_uow_cls):
        from brain.systems.feedback.meta_evolution import auto_tune_parameters, MetaInsight

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        insight = MetaInsight(
            category="strategy", severity="regression",
            message="Dropping", metric_name="strategy_habitual_success",
            current_value=0.4, previous_value=0.8,
        )
        adjustments = auto_tune_parameters([insight])
        assert any("habitual" in k for k in adjustments)

    def test_no_adjustments_on_info_only(self):
        from brain.systems.feedback.meta_evolution import auto_tune_parameters, MetaInsight

        insight = MetaInsight(
            category="prediction", severity="info",
            message="Improving", metric_name="prediction_accuracy",
            current_value=0.9, previous_value=0.8,
        )
        adjustments = auto_tune_parameters([insight])
        assert adjustments == {}

    def test_no_cursor_param(self):
        """auto_tune_parameters() should not accept a cur parameter."""
        import inspect
        from brain.systems.feedback.meta_evolution import auto_tune_parameters
        sig = inspect.signature(auto_tune_parameters)
        assert "cur" not in sig.parameters


class TestRunMetaEvolution:
    """Test full meta-evolution pipeline uses UnitOfWork."""

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    @patch("brain.systems.feedback.meta_evolution.compare_periods")
    @patch("brain.systems.feedback.meta_evolution.compute_evolution_metrics")
    def test_full_pipeline(self, mock_metrics, mock_compare, mock_uow_cls):
        from brain.systems.feedback.meta_evolution import run_meta_evolution, MetaInsight, EvolutionMetrics

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        mock_compare.return_value = [
            MetaInsight(
                category="prediction", severity="regression",
                message="Accuracy dropped", metric_name="prediction_accuracy",
                current_value=0.5, previous_value=0.8,
                suggested_action="Recalibrate",
            ),
        ]
        mock_metrics.return_value = EvolutionMetrics(
            period_start=date.today() - timedelta(days=7),
            period_end=date.today(),
            prediction_accuracy=0.5,
            total_runs=10,
        )

        stats = run_meta_evolution()

        assert stats["insights_total"] == 1
        assert stats["regressions"] == 1
        assert stats["insights_stored"] >= 1
        assert "prediction_accuracy" in stats["metrics"]

    @patch("brain.systems.feedback.meta_evolution.compare_periods")
    @patch("brain.systems.feedback.meta_evolution.compute_evolution_metrics")
    def test_no_regressions(self, mock_metrics, mock_compare):
        from brain.systems.feedback.meta_evolution import run_meta_evolution, EvolutionMetrics

        mock_compare.return_value = []
        mock_metrics.return_value = EvolutionMetrics(
            period_start=date.today() - timedelta(days=7),
            period_end=date.today(),
        )

        stats = run_meta_evolution()
        assert stats["regressions"] == 0
        assert stats["adjustments"] == {}

    def test_no_cursor_param(self):
        """run_meta_evolution() should not accept a cur parameter."""
        import inspect
        from brain.systems.feedback.meta_evolution import run_meta_evolution
        sig = inspect.signature(run_meta_evolution)
        assert "cur" not in sig.parameters


class TestGetTunedParameter:
    """Test parameter retrieval uses UnitOfWork."""

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_returns_stored_value(self, mock_uow_cls):
        from brain.systems.feedback.meta_evolution import get_tuned_parameter

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = {
            "content": "[auto-tune] heuristic_prune_threshold = 0.15"
        }
        mock_uow.session.execute.return_value = mock_result

        result = get_tuned_parameter("heuristic_prune_threshold", default=0.2)
        assert result == 0.15

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_returns_default_when_no_stored(self, mock_uow_cls):
        from brain.systems.feedback.meta_evolution import get_tuned_parameter

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_uow.session.execute.return_value = mock_result

        result = get_tuned_parameter("missing_param", default=0.5)
        assert result == 0.5

    def test_no_cursor_param(self):
        """get_tuned_parameter() should not accept a cur parameter."""
        import inspect
        from brain.systems.feedback.meta_evolution import get_tuned_parameter
        sig = inspect.signature(get_tuned_parameter)
        assert "cur" not in sig.parameters


class TestStoreParameter:
    """Test _store_parameter helper uses UnitOfWork."""

    @patch("brain.systems.feedback.meta_evolution.UnitOfWork")
    def test_stores_via_session(self, mock_uow_cls):
        from brain.systems.feedback.meta_evolution import _store_parameter

        mock_uow = MagicMock()
        mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)

        _store_parameter("test_param", 0.42)
        mock_uow.session.execute.assert_called_once()

    def test_no_cursor_param(self):
        """_store_parameter() should not accept a cur parameter."""
        import inspect
        from brain.systems.feedback.meta_evolution import _store_parameter
        sig = inspect.signature(_store_parameter)
        assert "cur" not in sig.parameters
