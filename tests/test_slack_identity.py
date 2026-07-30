"""Canonical Slack identity normalization contracts."""

from __future__ import annotations


def test_normalize_slack_identities_applies_documented_source_precedence():
    from brain.systems.slack.identity import normalize_slack_identities

    normalization = normalize_slack_identities(
        {
            "slack": {
                "identity_map": {
                    "UMAP": "user-map",
                    "UMAPWITHLINK": "user-map-with-link",
                    "UAGREE": "user-agree",
                }
            },
            "identity_links": {
                "slack": {
                    "ULINK": {
                        "user_id": "user-link",
                        "display_name": "Linked only",
                    },
                    "UAGREE": {
                        "user_id": "user-agree",
                        "display_name": "Both agree",
                    },
                    "UMAPWITHLINK": {
                        "user_id": " ",
                        "display_name": "Linked display",
                    },
                }
            },
        }
    )

    assert normalization.diagnostics == ()
    assert {
        record.slack_user_id: (record.display_name, record.user_id)
        for record in normalization.records
    } == {
        "ULINK": ("Linked only", "user-link"),
        "UAGREE": ("Both agree", "user-agree"),
        "UMAPWITHLINK": ("Linked display", "user-map-with-link"),
        "UMAP": ("UMAP", "user-map"),
    }


def test_normalize_slack_identities_returns_typed_conflict_without_raising():
    from brain.systems.slack.identity import (
        SlackIdentityMappingError,
        normalize_slack_identities,
    )

    normalization = normalize_slack_identities(
        {
            "slack": {
                "identity_map": {
                    "UCONFLICT": "user-mapped",
                }
            },
            "identity_links": {
                "slack": {
                    "UCONFLICT": {
                        "user_id": "user-linked",
                        "display_name": "Conflicted",
                    }
                }
            },
        }
    )

    record = normalization.record_for_slack_user_id("UCONFLICT")
    assert record is not None
    assert record.user_id is None
    assert len(normalization.diagnostics) == 1
    diagnostic = normalization.diagnostics[0]
    assert isinstance(diagnostic, SlackIdentityMappingError)
    assert diagnostic.code == "linked_mapped_user_id_conflict"
    assert diagnostic.slack_user_id == "UCONFLICT"
    assert diagnostic.linked_user_id == "user-linked"
    assert diagnostic.mapped_user_id == "user-mapped"
