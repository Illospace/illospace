from brain.systems.inbound.attribution import collect_result_refs


def test_raw_github_artifact_shape_does_not_emit_a_ref_without_a_tool_boundary():
    assert collect_result_refs(
        {
            "repo": "Illospace/illospace",
            "issue": {"type": "issue", "number": 857},
        },
        source="raw_mapping",
    ) == []
