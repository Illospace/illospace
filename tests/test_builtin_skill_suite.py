"""Regression tests for product-owned built-in skills."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
from typing import Any


CORE_BUILTINS = {
    "coordinate",
    "orchestrate",
    "report-workspace-blocker",
    "skill-authoring",
    "conversation-audit",
    "build-workspace-app",
    "manage-domains",
    "manage-projects",
}


def test_builtin_skills_are_limited_to_product_primitives():
    import brain.systems.skills.builtin as module

    assert set(module.BUILTIN_SKILLS) == CORE_BUILTINS
    assert not hasattr(module, "SKILL_RETIREMENTS")
    assert not hasattr(module, "BUILTIN_SKILL_RETIREMENTS")


def test_filesystem_bundle_discovery_includes_private_team_bundles():
    from brain.systems.skills.builtin import (
        BUILTIN_SKILL_BUNDLE_ROOT,
        BUILTIN_SKILLS,
        _filesystem_skill_bundle_names,
    )
    from brain.systems.skills.bundles import load_skill_bundle

    names = set(_filesystem_skill_bundle_names())
    assert CORE_BUILTINS <= names
    assert "uwear-engineering-triage" in names
    assert "uwear-engineering-triage" not in BUILTIN_SKILLS

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    assert bundle.manifest.source == "self_hosted"
    assert bundle.manifest.visibility == "private_local"


def test_uwear_engineering_triage_includes_dependency_monitor():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")

    assert bundle.manifest.version == "1.9.0"
    procedure = bundle.skill_markdown
    for expected in (
        "## Dependency Monitor",
        "selected priority issues or PRs",
        "GitHub Ticket Tracker Domain",
        "generic coordination ticket",
        "only high-confidence dependency blockers or missing companion work",
        "dependency check",
    ):
        assert expected in procedure


def test_uwear_triage_bundle_packages_chantier_record_contract():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    contract = _uwear_triage_asset(bundle, "references/chantier-record-contract.md")

    assert "# Domain 1 Chantier Record Contract" in contract
    assert "`feature`, `incident`, `quality`, `gtm`, or `sunset`" in contract
    assert "`exploring`, `building`, `shipping`, `verifying`, `done`, or `paused`" in contract
    assert contract.count("```json") == 1
    assert '"parent_issue": "github:Illospace/illospace:issue:326"' in contract


def test_uwear_triage_bundle_packages_chantier_operations_v2():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown
    playbook = _uwear_triage_asset(bundle, "references/chantier-operations.md")

    assert "slug `uwear-engineering-triage-chantier-operations`" in procedure
    assert "before every scheduled digest" in procedure
    assert "before filing or recording a new work item" in procedure
    for expected in (
        "## Chantier-primary Digest Contract v2",
        "## Attach at Triage",
        "## Induction",
        "## Propose a Chantier",
        "## Freshness and Close-out",
        "## Declare Flow",
        "Part of chantier: <slug>",
        "at least three related items",
        "stated goal or PRD",
        "incident family",
        "Never auto-create a chantier",
        "ordinary mention without the `chantier` keyword",
        "second record",
        "builder-first owner suggestion",
        "`mirror pending tooling`",
        "add_github_sub_issue",
    ):
        assert expected in playbook


def test_uwear_triage_chantier_primary_digest_keeps_person_coverage():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown
    digest = procedure.split("## Team Digest Contract", 1)[1].split("## Workflow", 1)[0]

    for expected in (
        "chantier count",
        "one goal-progress line",
        "Quiet chantiers",
        "`Loose items`",
        "Per-person recap",
        "Reda, Axel, and JB",
        "tracker records with that person as exact `assignee`",
        "GitHub issues assigned to their handle",
        "PRs they authored",
        "builder-first engineering candidates",
        "rebalancing recommendation",
        "changed state, gained or lost",
        "members, or hit/cleared a blocker",
    ):
        assert expected in digest
    assert "active chantiers with slug, state, member refs, blockers, and next step" in procedure
    assert "no-silent-departure" in procedure
    assert "untouched for 3+ days or missing" in procedure
    assert "outcome summary in the goal's language" in procedure


def test_uwear_triage_scheduled_memory_contract():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown
    memory = _uwear_triage_asset(bundle, "references/memory.md")

    for expected in (
        "## Memory",
        "At run START",
        "memory_reconstruct",
        "load-bearing claim against live sources",
        "At run END",
        "memory_ingest_source",
        "What future runs need:",
        "confidence=0.9",
        "cap inferences at `0.7`",
        "slug `uwear-engineering-triage-memory`",
        "`references/memory.md`",
    ):
        assert expected in procedure

    for expected in (
        "## Run Start — Recall on Subject",
        "## Run End — Select One Durable Outcome",
        "### What Makes a Good Memory",
        "## Stable Phrasing and Dedup",
        "`normalized_key`",
        "memory_supersede",
        "memory_archive",
        "memory_link",
        "concrete reason grounded in the current authoritative source",
        "Bad — ephemeral count",
        "Bad — delivery receipt",
    ):
        assert expected in memory


def test_uwear_triage_memory_delta_fingerprints_exact_bundle_mirrors():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    note = (Path(__file__).parents[1] / "docs" / "329-live-delta.md").read_text()
    mirrors = (
        (bundle.skill_markdown, "v9"),
        (
            _uwear_triage_asset(bundle, "references/memory.md"),
            "content",
        ),
    )

    for content, label in mirrors:
        encoded = content.encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        assert f"characters: `{len(content)}`" in note, label
        assert f"bytes: `{len(encoded)}`" in note, label
        assert f"SHA-256: `{fingerprint}`" in note, label


def test_uwear_triage_declare_delta_fingerprints_exact_bundle_mirrors():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    note = (Path(__file__).parents[1] / "docs" / "331-live-delta.md").read_text()
    content = _uwear_triage_asset(bundle, "references/chantier-operations.md")
    encoded = content.encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()

    assert f"characters: `{len(content)}`" in note
    assert f"bytes: `{len(encoded)}`" in note
    assert f"SHA-256: `{fingerprint}`" in note


def test_uwear_triage_skill_distinguishes_internal_tracker_from_real_github_issue():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown

    # The core keeps the always-on invariants; the filing decision tree is an
    # on-demand playbook (see test_uwear_triage_on_demand_run_mode_split).
    assert "create_github_issue" in procedure
    unwrapped = " ".join(procedure.split())
    assert "never describe an internal tracker record as a GitHub issue" in unwrapped

    playbook = _uwear_triage_asset(bundle, "references/creating-work-items.md")
    assert "## Creating Work Items" in playbook
    assert "Never describe an internal tracker record as a GitHub issue" in playbook
    # The graceful-degradation branch must be spelled out, not just the happy path.
    assert "no_write_token" in playbook


def test_uwear_triage_skill_keeps_slack_formatting_contract():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown

    assert "## Slack Formatting" in procedure
    assert "Prefer Slack-native links" in procedure
    assert "Never invent Slack user ids" in procedure
    assert "Fall back gracefully" in procedure


def test_uwear_triage_harvests_alert_thread_resolution_before_digesting():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown
    playbook = _uwear_triage_asset(bundle, "references/chantier-operations.md")

    for expected in (
        "Resolution harvest",
        "alert_slack_channel",
        "alert_slack_thread_ts",
        "resolution_confirmed_ts",
        "later human reply says the problem still reproduces",
        "read-only on Slack",
        "movement/outcome",
    ):
        assert expected in playbook
    assert "alert-resolution replies" in procedure


def test_uwear_triage_skill_keeps_backlog_hygiene_contract():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown

    # The core pointer names the three hygiene modes; the full contract is an
    # on-demand playbook.
    assert "`process-design`" in procedure
    assert "`no-write-audit`" in procedure
    assert "`live-hygiene-run`" in procedure

    playbook = _uwear_triage_asset(bundle, "references/backlog-maintenance.md")
    assert "## Backlog Seed" in playbook
    assert "## Backlog Hygiene" in playbook
    assert "cleanup:close-candidate" in playbook
    assert "Do not create" in playbook
    assert "Uwear-specific backlog objects" in playbook
    assert "do not close them" in playbook
    assert "silently" in playbook


def _uwear_triage_asset(bundle: Any, path: str) -> str:
    asset = next((item for item in bundle.assets if item.path == path), None)
    assert asset is not None, f"missing bundle asset: {path}"
    assert asset.content_text, f"bundle asset has no inline text: {path}"
    return asset.content_text


def test_uwear_triage_on_demand_run_mode_split():
    """The coordinator core doc must stay small enough to read untruncated.

    The live mirror (Domain 37 record 1155) is fetched whole through
    manage_domain's output budget (40K chars, JSON-wrapped) on every scheduled
    coordinator run. Rarely-needed run modes live in on-demand playbooks —
    bundle `references/` assets mirrored as separate Domain 37 records — so
    growth goes there, not into the core. If this size gate trips, move
    content into an on-demand playbook instead of raising budgets.
    """
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown

    assert len(procedure) < 34_000
    assert "## On-demand Run Modes" in procedure

    # The moved sections must not silently grow back into the core.
    for moved_heading in (
        "## Direct Customer Support",
        "## Creating Work Items",
        "## Backlog Seed",
        "## Backlog Hygiene",
        "## Attach at Triage",
        "## Induction",
        "## Propose a Chantier",
        "## Freshness and Close-out",
        "## Declare Flow",
    ):
        assert moved_heading not in procedure

    for path in (
        "references/customer-support.md",
        "references/creating-work-items.md",
        "references/backlog-maintenance.md",
        "references/chantier-operations.md",
        "references/memory.md",
    ):
        # The core names each playbook asset path, and every playbook carries
        # the provenance preamble pointing back at the core doc.
        assert f"`{path}`" in procedure
        playbook = _uwear_triage_asset(bundle, path)
        assert "> On-demand mode playbook" in playbook
        assert "record `1155`" in playbook


def test_builtin_skills_have_structured_routing_metadata():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    for name, skill in BUILTIN_SKILLS.items():
        assert skill["name"] == name
        assert skill["description"]
        assert skill["procedure"]
        assert skill["source_kind"] == "illo-core"
        assert skill["trust_level"] == "illo_core"
        assert skill["thinking_tier"] in {"none", "low", "medium", "high", "xhigh"}
        assert _has_text_items(skill["triggers"], "pattern")
        assert _has_text_items(skill["guardrails"], "text")
        assert all(item.get("severity") for item in skill["guardrails"])


def test_uwear_triage_resolves_every_on_demand_playbook_by_exact_slug():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / "uwear-engineering-triage")
    procedure = bundle.skill_markdown

    for slug in (
        "uwear-engineering-triage-customer-support",
        "uwear-engineering-triage-creating-work-items",
        "uwear-engineering-triage-backlog-maintenance",
        "uwear-engineering-triage-chantier-operations",
        "uwear-engineering-triage-memory",
    ):
        assert f"slug `{slug}`" in procedure

    normalized = " ".join(procedure.split())
    assert "whose `data.slug` exactly matches" in normalized
    assert "require exactly one active match" in normalized
    assert "action=get_record" in normalized
    for record_id in range(1271, 1276):
        assert f"record `{record_id}`" not in procedure


def test_builtin_skills_have_explicit_role_boundaries():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    required_sections = (
        "## Role",
        "## Use When",
        "## Do Not Use When",
        "## Context To Load",
        "## Operating Loop",
        "## Output Contract",
        "## Failure Modes",
    )

    for skill in BUILTIN_SKILLS.values():
        procedure = skill["procedure"]
        for section in required_sections:
            assert section in procedure
        assert _has_text_items(skill["pitfalls"], "text")
        assert _has_text_items(skill["refinements"], "text")


def test_builtin_skill_bundles_parse_and_mirror_bootstrap_procedures():
    from brain.systems.skills.builtin import (
        BUILTIN_SKILL_BUNDLE_ROOT,
        BUILTIN_SKILLS,
    )
    from brain.systems.skills.bundles import load_skill_bundle

    for name, skill in BUILTIN_SKILLS.items():
        bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / name)
        assert bundle.manifest.name == name
        assert bundle.manifest.source == "illo-core"
        assert bundle.skill_markdown == skill["procedure"]
        assert bundle.manifest.runtime.default_thinking_tier == skill["thinking_tier"]
        assert bundle.manifest.routing.triggers == skill["triggers"]
        assert bundle.assets


def test_tool_heavy_builtin_bundles_have_progressive_assets():
    from brain.systems.skills.builtin import BUILTIN_SKILL_BUNDLE_ROOT
    from brain.systems.skills.bundles import load_skill_bundle

    expected_assets = {
        "skill-authoring": {
            "templates/SKILL.md",
            "templates/skill.toml",
            "schemas/skill-authoring-output.schema.json",
            "examples/private-db-skill.md",
            "references/versioning.md",
        },
        "build-workspace-app": {
            "templates/sandboxed-html-app.html",
            "templates/thumbnail.html",
            "schemas/workspace-app-output.schema.json",
            "examples/app-local-state.md",
            "examples/domain-backed-app.md",
            "references/host-bridge.md",
        },
        "manage-domains": {
            "templates/domain-schema.json",
            "schemas/domain-change-output.schema.json",
            "examples/good-crm-domain.md",
            "examples/overmodeled-domain.md",
            "references/versioning-conflicts.md",
        },
    }

    for name, required_paths in expected_assets.items():
        bundle = load_skill_bundle(BUILTIN_SKILL_BUNDLE_ROOT / name)
        actual_paths = {asset.path for asset in bundle.assets}
        assert required_paths <= actual_paths
        for asset in bundle.assets:
            if asset.path.startswith("schemas/"):
                assert asset.content_text is not None
                json.loads(asset.content_text)


def test_coordinate_owns_routing_before_orchestration():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    procedure = BUILTIN_SKILLS["coordinate"]["procedure"]
    for expected in (
        "## Routing Ladder",
        "brain_skills",
        "skill_view",
        "memory as stale",
        "single tool",
        "internal orchestration protocol",
        "external state",
    ):
        assert expected in procedure


def test_orchestrate_is_internal_protocol_not_default_coordinator():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    skill = BUILTIN_SKILLS["orchestrate"]
    assert "Internal orchestration protocol" in skill["description"]
    assert "You are not a general conversation skill" in skill["procedure"]
    assert any(
        trigger.get("direction") == "against"
        for trigger in skill["triggers"]
    )


def test_orchestrate_keeps_runtime_contract_and_memory_lifecycle():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    procedure = BUILTIN_SKILLS["orchestrate"]["procedure"]
    for expected in (
        "brain_skills",
        "AgentRun graph",
        "OBJECTIVE",
        "SCOPE",
        "INPUT",
        "OUTPUT",
        "DONE WHEN",
        "AgentRun graph started",
        "AgentRun graph completed/failed",
        "session_promote",
        "session_close",
    ):
        assert expected in procedure


def test_report_workspace_blocker_routes_to_headless_worker():
    from brain.systems.skills.builtin import BUILTIN_SKILLS

    skill = BUILTIN_SKILLS["report-workspace-blocker"]
    assert "tickets" in skill["description"]
    assert "spawn_worker" in skill["procedure"]
    assert "headless=true" in skill["procedure"]
    assert any(trigger["pattern"] == "report this bug" for trigger in skill["triggers"])
    assert any("Search for duplicates" in guardrail["text"] for guardrail in skill["guardrails"])


def test_uwear_generation_investigation_bundle_uses_canonical_join():
    from brain.systems.skills.builtin import (
        BUILTIN_SKILL_BUNDLE_ROOT,
        BUILTIN_SKILLS,
        _filesystem_skill_bundle_names,
    )
    from brain.systems.skills.bundles import load_skill_bundle

    names = set(_filesystem_skill_bundle_names())
    assert "uwear-generation-investigation" in names
    # Filesystem team bundle, not a core product primitive.
    assert "uwear-generation-investigation" not in BUILTIN_SKILLS

    bundle = load_skill_bundle(
        BUILTIN_SKILL_BUNDLE_ROOT / "uwear-generation-investigation"
    )
    assert bundle.manifest.source == "self_hosted"
    assert bundle.manifest.visibility == "private_local"

    procedure = bundle.skill_markdown
    # The validated CANONICAL owner join, read-only credential, and the payload
    # fields the hypothesis depends on.
    assert "user_type = 'profile'" in procedure
    assert "PROD_POSTGRES_READONLY_URL" in procedure
    assert "tryon_prompt" in procedure
    assert "generation_result_origin" in procedure
    # Must actively warn off the legacy batch path that silently misses recent
    # profiles (the trap that would have produced a wrong recipe).
    assert "legacy" in procedure.lower()
    assert "batch" in procedure.lower()
    # Read-only safety must be spelled out, not assumed.
    assert "read-only" in procedure.lower()


def _has_text_items(items: Any, key: str) -> bool:
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, Mapping) or not str(item.get(key) or "").strip():
            return False
    return True
