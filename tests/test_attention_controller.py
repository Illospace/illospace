"""Tests for the shadow attention controller."""

from unittest.mock import MagicMock, patch

import pytest


class TestAttentionRanking:
    def test_rank_candidates_is_deterministic(self):
        from brain.systems.memory.attention_controller import AttentionController

        controller = AttentionController(preload_item_limit=3)
        candidates = [
            {"id": 3, "content": "gamma", "similarity": 0.81, "salience": 7.0, "access_count": 2},
            {"id": 1, "content": "alpha", "similarity": 0.81, "salience": 7.0, "access_count": 2},
            {"id": 4, "content": "delta", "similarity": 0.72, "salience": 5.0, "access_count": 4},
            {"id": 2, "content": "beta", "similarity": 0.81, "salience": 7.0, "access_count": 2},
        ]

        shuffled = list(reversed(candidates))
        ranked_a = controller.rank_candidates(candidates, stage="brain_recall")
        ranked_b = controller.rank_candidates(shuffled, stage="brain_recall")

        assert [item.selected_key for item in ranked_a] == [item.selected_key for item in ranked_b]
        assert [item.selected_key for item in ranked_a] == [1, 2, 3, 4]


class TestAttentionLogging:
    def test_observe_retrieval_requires_user_or_service_context(self):
        from brain.systems.memory.attention_controller import observe_retrieval

        with pytest.raises(ValueError, match="requires user_id"):
            observe_retrieval(
                stage="brain_recall",
                query_text="database issues",
                candidates=[],
            )

    def test_visibility_filtered_candidates_do_not_leak_suppressed_ids(self):
        from brain.systems.memory.attention_controller import AttentionController

        controller = AttentionController(enabled=False)
        decision, ranked, selected, lazy_candidates = controller.evaluate(
            stage="brain_recall",
            query_text="database issues",
            user_id="user-1",
            org_id="org-1",
            candidates=[
                {
                    "id": 7,
                    "content": "visible",
                    "similarity": 0.91,
                    "salience": 8.0,
                    "visibility": "private",
                    "user_id": "user-1",
                    "org_id": "org-1",
                },
                {
                    "id": 8,
                    "content": "hidden",
                    "similarity": 0.99,
                    "salience": 9.0,
                    "visibility": "private",
                    "user_id": "user-2",
                    "org_id": "org-1",
                },
            ],
            selected_limit=3,
        )

        assert [item.selected_key for item in ranked] == [7]
        assert [item.selected_key for item in selected] == [7]
        assert lazy_candidates == []
        assert decision.selected_item_ids == [7]
        assert decision.suppressed_item_ids == []
        assert decision.debug["rationale"]["visibility_suppressed_count"] == 1
        selection = controller.materialize_selection(
            [
                {
                    "id": 7,
                    "content": "visible",
                    "similarity": 0.91,
                    "salience": 8.0,
                    "visibility": "private",
                    "user_id": "user-1",
                    "org_id": "org-1",
                },
                {
                    "id": 8,
                    "content": "hidden",
                    "similarity": 0.99,
                    "salience": 9.0,
                    "visibility": "private",
                    "user_id": "user-2",
                    "org_id": "org-1",
                },
            ],
            decision,
        )
        assert selection.suppressed == []

    def test_observe_retrieval_persists_decision_and_feedback_rows(self, mock_uow):
        from brain.systems.memory.attention_controller import observe_retrieval
        from brain.platform.db.models.system import RetrievalDecision, RetrievalItemFeedback

        mock_uow.session.add = MagicMock()
        mock_uow.session.flush = MagicMock()

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            decision = observe_retrieval(
                stage="brain_recall",
                query_text="database issues",
                user_id="user-1",
                org_id="org-1",
                candidates=[
                    {"id": 7, "content": "alpha", "similarity": 0.91, "salience": 8.0},
                    {"id": 8, "content": "beta", "similarity": 0.88, "salience": 7.0},
                    {"id": 9, "content": "gamma", "similarity": 0.83, "salience": 6.0},
                    {"id": 10, "content": "delta", "similarity": 0.81, "salience": 5.0},
                ],
                preload_budget_tokens=480,
                lazy_budget_tokens=120,
            )

        assert decision["stage"] == "brain_recall"
        assert decision["selected_item_ids"] == [7, 8, 9]
        assert decision["suppressed_item_ids"] == [10]
        assert decision["user_id"] == "user-1"
        assert decision["org_id"] == "org-1"
        assert decision["debug"]["tenant_context"]["user_id"] == "user-1"
        assert decision["query_fingerprint"]

        added_types = [type(call.args[0]) for call in mock_uow.session.add.call_args_list]
        assert RetrievalDecision in added_types
        assert added_types.count(RetrievalItemFeedback) == 4
        decision_row = next(
            call.args[0]
            for call in mock_uow.session.add.call_args_list
            if isinstance(call.args[0], RetrievalDecision)
        )
        feedback_rows = [
            call.args[0]
            for call in mock_uow.session.add.call_args_list
            if isinstance(call.args[0], RetrievalItemFeedback)
        ]
        assert decision_row.user_id == "user-1"
        assert decision_row.org_id == "org-1"
        assert all(row.user_id == "user-1" and row.org_id == "org-1" for row in feedback_rows)

    def test_observe_retrieval_marks_lazy_load_eligible_candidates(self, mock_uow):
        from brain.systems.memory.attention_controller import observe_retrieval
        from brain.platform.db.models.system import RetrievalItemFeedback

        mock_uow.session.add = MagicMock()
        mock_uow.session.flush = MagicMock()

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            decision = observe_retrieval(
                stage="brain_recall",
                query_text="database issues",
                user_id="user-1",
                org_id="org-1",
                candidates=[
                    {"id": 7, "content": "alpha", "similarity": 0.97, "salience": 8.0},
                    {"id": 8, "content": "beta", "similarity": 0.94, "salience": 7.5},
                    {"id": 9, "content": "gamma", "similarity": 0.91, "salience": 7.0},
                    {"id": 10, "content": "delta", "similarity": 0.62, "salience": 4.0},
                ],
                preload_budget_tokens=480,
                lazy_budget_tokens=120,
            )

        assert decision["selected_item_ids"] == [7, 8, 9]
        assert decision["suppressed_item_ids"] == [10]
        assert decision["lazy_load_item_ids"] == [10]

        feedback_rows = [
            call.args[0]
            for call in mock_uow.session.add.call_args_list
            if isinstance(call.args[0], RetrievalItemFeedback)
        ]
        assert any(row.lazy_load_eligible for row in feedback_rows)
        assert any(row.preload_decision is False and row.lazy_load_eligible is True for row in feedback_rows)

    def test_record_usefulness_updates_existing_feedback_row(self, mock_uow):
        from brain.systems.memory.attention_controller import AttentionController
        from brain.platform.db.models.system import RetrievalItemFeedback

        feedback_row = RetrievalItemFeedback(
            retrieval_decision_id=17,
            memory_id=23,
            candidate_source="memory",
            user_id="user-1",
            org_id="org-1",
        )
        mock_uow.session.scalars.return_value.first.return_value = feedback_row

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            controller = AttentionController()
            ok = controller.record_usefulness(
                retrieval_decision_id=17,
                user_id="user-1",
                org_id="org-1",
                item_id=23,
                actually_used=True,
                cited_in_output=True,
                correlated_with_success=True,
                retry_delta=-1,
                verifier_helped=True,
                user_feedback_signal="helpful",
            )

        assert ok is True
        assert feedback_row.actually_used is True
        assert feedback_row.cited_in_output is True
        assert feedback_row.correlated_with_success is True
        assert feedback_row.retry_delta == -1
        assert feedback_row.verifier_helped is True
        assert feedback_row.user_feedback_signal == "helpful"
        assert feedback_row.feedback_at is not None
        stmt = mock_uow.session.scalars.call_args.args[0]
        assert "retrieval_item_feedback.user_id" in str(stmt)
        assert "retrieval_item_feedback.org_id" in str(stmt)

    def test_record_lazy_load_updates_existing_feedback_row(self, mock_uow):
        from brain.systems.memory.attention_controller import AttentionController
        from brain.platform.db.models.system import RetrievalItemFeedback

        feedback_row = RetrievalItemFeedback(
            retrieval_decision_id=18,
            memory_id=24,
            candidate_source="memory",
            user_id="user-1",
            org_id="org-1",
        )
        mock_uow.session.scalars.return_value.first.return_value = feedback_row

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            controller = AttentionController()
            ok = controller.record_lazy_load(
                retrieval_decision_id=18,
                user_id="user-1",
                org_id="org-1",
                item_id=24,
            )

        assert ok is True
        assert feedback_row.lazy_loaded is True
        assert feedback_row.feedback_at is not None

    def test_record_attention_usefulness_helper_updates_existing_feedback_row(self, mock_uow):
        from brain.platform.db.models.system import RetrievalItemFeedback
        from brain.systems.memory.retrieval_feedback import record_attention_usefulness

        feedback_row = RetrievalItemFeedback(
            retrieval_decision_id=19,
            memory_id=25,
            candidate_source="memory",
            user_id="user-1",
            org_id="org-1",
        )
        mock_uow.session.scalars.return_value.first.return_value = feedback_row

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            ok = record_attention_usefulness(
                19,
                user_id="user-1",
                org_id="org-1",
                item_id=25,
                actually_used=True,
                cited_in_output=True,
                correlated_with_success=True,
                lazy_loaded=True,
                retry_delta=-2,
                verifier_helped=True,
                user_feedback_signal="helpful",
            )

        assert ok is True
        assert feedback_row.actually_used is True
        assert feedback_row.cited_in_output is True
        assert feedback_row.correlated_with_success is True
        assert feedback_row.lazy_loaded is True
        assert feedback_row.retry_delta == -2
        assert feedback_row.verifier_helped is True
        assert feedback_row.user_feedback_signal == "helpful"

    def test_load_lazy_candidates_fetches_and_marks_loaded(self, mock_uow):
        from brain.platform.db.models.system import RetrievalItemFeedback
        from brain.systems.memory.attention_controller import AttentionController

        feedback_row = RetrievalItemFeedback(
            retrieval_decision_id=21,
            memory_id=42,
            candidate_source="memory",
            user_id="user-1",
            org_id="org-1",
            lazy_load_eligible=True,
            lazy_loaded=False,
        )
        lazy_memory = MagicMock()
        lazy_memory.id = 42
        lazy_memory.content = "lazy payload"
        lazy_memory.memory_type = "lesson"
        lazy_memory.memory_tier = "semantic"
        lazy_memory.salience = 8.0
        lazy_memory.visibility = "private"
        lazy_memory.user_id = "user-1"
        lazy_memory.org_id = "org-1"

        mock_uow.session.scalars.return_value.all.return_value = [feedback_row]
        mock_uow.session.get.side_effect = lambda model, pk: lazy_memory if pk == 42 else None

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            controller = AttentionController()
            loaded = controller.load_lazy_candidates(
                retrieval_decision_id=21,
                user_id="user-1",
                org_id="org-1",
                limit=1,
            )

        assert len(loaded) == 1
        assert loaded[0]["id"] == 42
        assert loaded[0]["tenant_context"]["user_id"] == "user-1"
        assert feedback_row.lazy_loaded is True
        assert feedback_row.feedback_at is not None

    def test_lazy_load_cannot_expose_another_users_private_memory(self, mock_uow):
        from brain.platform.db.models.system import RetrievalItemFeedback
        from brain.systems.memory.attention_controller import AttentionController

        feedback_row = RetrievalItemFeedback(
            retrieval_decision_id=22,
            memory_id=43,
            candidate_source="memory",
            user_id="user-1",
            org_id="org-1",
            lazy_load_eligible=True,
            lazy_loaded=False,
        )
        lazy_memory = MagicMock()
        lazy_memory.id = 43
        lazy_memory.content = "private payload"
        lazy_memory.memory_type = "lesson"
        lazy_memory.memory_tier = "semantic"
        lazy_memory.salience = 8.0
        lazy_memory.visibility = "private"
        lazy_memory.user_id = "user-2"
        lazy_memory.org_id = "org-1"

        mock_uow.session.scalars.return_value.all.return_value = [feedback_row]
        mock_uow.session.get.side_effect = lambda model, pk: lazy_memory if pk == 43 else None

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            controller = AttentionController()
            loaded = controller.load_lazy_candidates(
                retrieval_decision_id=22,
                user_id="user-1",
                org_id="org-1",
                limit=1,
            )

        assert loaded == []
        assert feedback_row.lazy_loaded is False
        assert feedback_row.feedback_at is None
