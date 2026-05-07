"""Focused tests for the native chat backend foundation."""
from brain.app.mentions import classify_mention_intent
from brain.app.api.routers.chat import extract_mention_tokens, resolve_user_mentions
from brain.platform.db.models.org import User
from brain.platform.db.repositories.chat import build_dm_stable_key


def _user(user_id: str, name: str, email: str) -> User:
    return User(
        id=user_id,
        org_id="org-1",
        name=name,
        email=email,
        color="#6366f1",
        role="member",
        approved=True,
    )


def test_extract_mention_tokens_handles_start_and_punctuation():
    tokens = extract_mention_tokens("@illo please sync with @alex, and @redam.")
    assert tokens == {"illo", "alex", "redam"}


def test_mention_intent_invokes_illo_only_for_no_mentions_or_illo():
    assert classify_mention_intent("plain request").should_invoke_illo is True
    assert classify_mention_intent("@Illo please help").should_invoke_illo is True
    assert classify_mention_intent("@illo, loop in @Reda.").should_invoke_illo is True

    teammate_only = classify_mention_intent("@reda can you review?")
    assert teammate_only.should_invoke_illo is False
    assert teammate_only.skip_reason == "team_mention_without_illo"


def test_resolve_user_mentions_matches_first_name_and_email_local_part():
    users = [
        _user("user-1", "Alex Example", "alex@example.com"),
        _user("user-2", "Riley Example", "redam-test@example.com"),
    ]
    matches, illo_invoked = resolve_user_mentions(
        "Loop in @alex and @redam on this. @illo can wait.",
        users,
    )
    assert illo_invoked is True
    assert [str(user.id) for user in matches] == ["user-1", "user-2"]


def test_build_dm_stable_key_is_order_independent():
    left = build_dm_stable_key("user-b", "user-a")
    right = build_dm_stable_key("user-a", "user-b")
    assert left == right == "dm:user-a:user-b"


def test_resolve_user_mentions_deduplicates_alias_matches():
    users = [
        _user("user-1", "Alex Example", "alex@example.com"),
        _user("user-2", "Riley Example", "riley@example.com"),
    ]
    matches, illo_invoked = resolve_user_mentions(
        "@alex please pair with @alexhavard and ignore @unknown.",
        users,
    )
    assert illo_invoked is False
    assert [str(user.id) for user in matches] == ["user-1"]
