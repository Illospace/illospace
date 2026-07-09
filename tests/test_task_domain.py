"""Tests for brain/systems/task_domain.py and self_assess domain-aware checklists."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.task_domain import TaskDomain, classify_task_domain


class TestClassifyTaskDomain:
    def test_engineering_signals(self):
        assert classify_task_domain("fix the null deref in the auth handler") == TaskDomain.ENGINEERING
        assert classify_task_domain("refactor the payment module") == TaskDomain.ENGINEERING
        assert classify_task_domain("the API endpoint returns a 500") == TaskDomain.ENGINEERING
        assert classify_task_domain("write a database migration") == TaskDomain.ENGINEERING

    def test_business_signals_survive_greedy_verbs(self):
        # "create"/"update"/"draft"/"write" must NOT pull these into engineering.
        assert classify_task_domain("create the Q3 launch plan") == TaskDomain.BUSINESS
        assert classify_task_domain("update our pricing page") == TaskDomain.BUSINESS
        assert classify_task_domain("draft the GTM campaign brief") == TaskDomain.BUSINESS
        assert classify_task_domain("write the SEO content calendar") == TaskDomain.BUSINESS

    def test_product_signals(self):
        assert classify_task_domain("write the PRD for onboarding") == TaskDomain.PRODUCT
        assert classify_task_domain("prioritize the backlog") == TaskDomain.PRODUCT
        assert classify_task_domain("add acceptance criteria to the user story") == TaskDomain.PRODUCT

    def test_ops_signals(self):
        assert classify_task_domain("update the production incident runbook") == TaskDomain.OPS
        assert classify_task_domain("rotate the leaked credentials") == TaskDomain.OPS

    def test_ambiguous_resolves_to_other_not_engineering(self):
        assert classify_task_domain("do something vague") == TaskDomain.OTHER
        assert classify_task_domain("hello world") == TaskDomain.OTHER
        assert classify_task_domain("") == TaskDomain.OTHER

    def test_policy_prior_wins_over_text(self):
        # Engineering-looking text, but the policy pins business.
        assert classify_task_domain("fix the bug", policy="business") == TaskDomain.BUSINESS

    def test_repo_prior_used_when_no_policy(self):
        assert classify_task_domain("something ambiguous", repo="product") == TaskDomain.PRODUCT

    def test_policy_beats_repo(self):
        assert classify_task_domain("x", repo="ops", policy="business") == TaskDomain.BUSINESS

    def test_prior_accepts_enum(self):
        assert classify_task_domain("x", policy=TaskDomain.OPS) == TaskDomain.OPS

    def test_bad_prior_ignored_falls_to_heuristic(self):
        assert classify_task_domain("pricing and revenue forecast", policy="NONSENSE") == TaskDomain.BUSINESS

    def test_prior_is_case_and_space_tolerant(self):
        assert classify_task_domain("fix the bug", policy="  Engineering  ") == TaskDomain.ENGINEERING


class TestSelfAssessDomainAware:
    """The reported bug: non-engineering work must not get the engineering TDD bar."""

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_business_task_gets_business_checklist_not_tdd(self, mock_brain):
        mock_brain.return_value = {"memories": [], "warnings": [], "guardrails": []}
        from brain.app.hooks.self_assess import assess_quality
        result = assess_quality("create the Q3 launch plan", "drafted the plan")
        assert result["task_domain"] == "business"
        checklist = result["checklist"]
        assert not any("paste output" in i.lower() for i in checklist)  # not the code bar
        assert any("deliverable" in i.lower() for i in checklist)       # the business bar

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_engineering_task_still_gets_code_checklist(self, mock_brain):
        mock_brain.return_value = {"memories": [], "warnings": [], "guardrails": []}
        from brain.app.hooks.self_assess import assess_quality
        result = assess_quality("fix the null deref in api.py", "added guard")
        assert result["task_domain"] == "engineering"
        assert any("test" in i.lower() for i in result["checklist"])

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_ambiguous_engineering_keeps_work_mode_checklist(self, mock_brain):
        # "investigate login failures" has no domain noun -> OTHER, but must keep
        # the investigation work-mode checklist, not fall to a bare minimal bar.
        mock_brain.return_value = {"memories": [], "warnings": [], "guardrails": []}
        from brain.app.hooks.self_assess import assess_quality
        result = assess_quality("investigate login failures", "found root cause")
        assert result["task_type"] == "investigation"
        assert any("data" in i.lower() for i in result["checklist"])
