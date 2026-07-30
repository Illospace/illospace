"""Canonical Slack identity normalization contracts."""

from __future__ import annotations


def test_normalize_slack_identities_applies_documented_source_precedence():
    from brain.systems.slack.identity import (
        SlackIdentitySource,
        normalize_slack_identities,
    )

    records, conflicts = normalize_slack_identities(
        {
            "slack": {
                "team_id": "T123",
                "bot_user_id": "B123",
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
                        "metadata": {"source": "manual"},
                    },
                    "UMAPWITHLINK": {
                        "user_id": " ",
                        "display_name": "Linked display",
                    },
                }
            },
        }
    )

    assert conflicts == ()
    assert {
        record.slack_user_id: (record.display_name, record.user_id)
        for record in records.values()
    } == {
        "ULINK": ("Linked only", "user-link"),
        "UAGREE": ("Both agree", "user-agree"),
        "UMAPWITHLINK": ("Linked display", "user-map-with-link"),
        "UMAP": ("UMAP", "user-map"),
    }
    agreed = records["UAGREE"]
    assert agreed.sources == {
        SlackIdentitySource.LINK,
        SlackIdentitySource.MAP,
    }
    assert agreed.linked_user_id == "user-agree"
    assert agreed.mapped_user_id == "user-agree"
    assert agreed.link_display_name == "Both agree"
    assert agreed.link_metadata == {"source": "manual"}
    assert agreed.map_metadata == {
        "team_id": "T123",
        "bot_user_id": "B123",
    }


def test_normalize_slack_identities_returns_typed_conflict_without_raising():
    from brain.systems.slack.identity import (
        SlackIdentityConflict,
        normalize_slack_identities,
    )

    records, conflicts = normalize_slack_identities(
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

    record = records["UCONFLICT"]
    assert record.user_id is None
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert isinstance(conflict, SlackIdentityConflict)
    assert not isinstance(conflict, Exception)
    assert conflict.code == "linked_mapped_user_id_conflict"
    assert conflict.slack_user_id == "UCONFLICT"
    assert conflict.linked_user_id == "user-linked"
    assert conflict.mapped_user_id == "user-mapped"
