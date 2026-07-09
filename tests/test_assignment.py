"""Tests for brain/systems/inbound/assignment.py — deterministic owner resolution."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.task_domain import TaskDomain
from brain.systems.inbound.assignment import (
    AssignmentRules,
    OwnerBasis,
    default_rules,
    resolve_owner,
    _parse_repo_owners,
)


class TestResolveOwner:
    def test_domain_rule_wins_over_connection(self):
        rules = AssignmentRules(domain_owners={TaskDomain.BUSINESS: "reda"})
        d = resolve_owner(task_domain=TaskDomain.BUSINESS, connection_owner_id="conn", rules=rules)
        assert d.user_id == "reda"
        assert d.basis == OwnerBasis.RULE

    def test_product_routes_by_rule(self):
        rules = AssignmentRules(domain_owners={TaskDomain.PRODUCT: "reda"})
        assert resolve_owner(task_domain=TaskDomain.PRODUCT, rules=rules).user_id == "reda"

    def test_string_domain_is_coerced(self):
        rules = AssignmentRules(domain_owners={TaskDomain.BUSINESS: "reda"})
        assert resolve_owner(task_domain="business", rules=rules).user_id == "reda"

    def test_repo_rule_matches_full_slug_and_bare_name(self):
        rules = AssignmentRules(repo_owners={"uwear-backend": "axel"})
        assert resolve_owner(repo="Illospace/uwear-backend", rules=rules).user_id == "axel"
        assert resolve_owner(repo="uwear-backend", rules=rules).user_id == "axel"

    def test_domain_beats_repo(self):
        rules = AssignmentRules(
            domain_owners={TaskDomain.BUSINESS: "reda"},
            repo_owners={"site": "axel"},
        )
        assert resolve_owner(task_domain="business", repo="site", rules=rules).user_id == "reda"

    def test_connection_fallback_when_no_rule(self):
        d = resolve_owner(task_domain=TaskDomain.ENGINEERING, connection_owner_id="conn")
        assert d.user_id == "conn"
        assert d.basis == OwnerBasis.CONNECTION

    def test_unassigned_when_nothing_resolves(self):
        d = resolve_owner(task_domain=TaskDomain.ENGINEERING)
        assert d.user_id is None
        assert d.basis == OwnerBasis.UNASSIGNED
        assert d.is_assigned is False

    def test_engineering_is_not_auto_routed(self):
        # No engineering rule by default -> falls to connection, never guessed.
        rules = AssignmentRules(domain_owners={TaskDomain.BUSINESS: "reda"})
        d = resolve_owner(task_domain=TaskDomain.ENGINEERING, connection_owner_id="c", rules=rules)
        assert d.basis == OwnerBasis.CONNECTION


class TestDefaultRules:
    def test_business_owner_from_env_covers_product(self, monkeypatch):
        monkeypatch.setenv("ILLO_BUSINESS_OWNER_USER_ID", "reda-uid")
        monkeypatch.delenv("ILLO_PRODUCT_OWNER_USER_ID", raising=False)
        monkeypatch.delenv("ILLO_REPO_OWNERS", raising=False)
        rules = default_rules()
        assert rules.owner_for(task_domain=TaskDomain.BUSINESS) == "reda-uid"
        assert rules.owner_for(task_domain=TaskDomain.PRODUCT) == "reda-uid"

    def test_product_owner_override(self, monkeypatch):
        monkeypatch.setenv("ILLO_BUSINESS_OWNER_USER_ID", "reda-uid")
        monkeypatch.setenv("ILLO_PRODUCT_OWNER_USER_ID", "pm-uid")
        rules = default_rules()
        assert rules.owner_for(task_domain=TaskDomain.PRODUCT) == "pm-uid"

    def test_no_env_means_no_rule(self, monkeypatch):
        monkeypatch.delenv("ILLO_BUSINESS_OWNER_USER_ID", raising=False)
        monkeypatch.delenv("ILLO_PRODUCT_OWNER_USER_ID", raising=False)
        rules = default_rules()
        assert rules.owner_for(task_domain=TaskDomain.BUSINESS) is None


class TestParseRepoOwners:
    def test_parses_pairs(self):
        assert _parse_repo_owners("a=1,owner/b=2") == {"a": "1", "owner/b": "2"}

    def test_ignores_malformed(self):
        assert _parse_repo_owners("") == {}
        assert _parse_repo_owners("nope,x=,=y,ok=1") == {"ok": "1"}
