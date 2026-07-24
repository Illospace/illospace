from brain.platform.db.schemas import skills as skill_schemas
from brain.platform.providers.model_policy import EFFORT_TIERS, EFFORT_TIER_SET
from brain.systems.runs.tool_catalog.definitions.brain import BRAIN_TOOLS
from brain.systems.runs.tool_catalog.definitions.workers import WORKER_SPAWN_TOOLS
from brain.systems.runs.tool_catalog.handlers import common as handler_common
from brain.systems.skills import bundles


def _tool(name: str) -> dict:
    return next(
        tool
        for tool in (*BRAIN_TOOLS, *WORKER_SPAWN_TOOLS)
        if tool["name"] == name
    )


def test_effort_vocabulary_contract_matches_all_schema_boundaries():
    canonical = set(EFFORT_TIER_SET)
    assert tuple(EFFORT_TIERS) == ("none", "low", "medium", "high", "xhigh")

    manage_skill = _tool("manage_skill")["input_schema"]["properties"]
    manage_cycle = _tool("manage_cycle")["input_schema"]["properties"]
    spawn_worker = _tool("spawn_worker")["input_schema"]["properties"]
    schema_enums = (
        manage_skill["thinking_tier"]["enum"],
        manage_skill["skills"]["items"]["properties"]["thinking_tier"]["enum"],
        manage_cycle["thinking_override"]["enum"],
        spawn_worker["effort"]["enum"],
    )
    for enum in schema_enums:
        assert set(enum) == canonical

    assert spawn_worker["model"]["type"] == "string"
    assert set(skill_schemas._REASONING_EFFORTS) == canonical
    assert set(handler_common._REASONING_EFFORTS) == canonical
    assert set(bundles._REASONING_EFFORTS) == canonical


def test_spawn_worker_description_teaches_routing_patterns():
    description = _tool("spawn_worker")["description"].lower()

    assert "xhigh" in description and "judgment" in description
    assert "high" in description and "standard" in description
    assert "medium" in description and "execution" in description
    assert "low" in description and "reflex" in description
    assert "director" in description and "workhorse" in description
    assert "cross-provider verifier" in description
