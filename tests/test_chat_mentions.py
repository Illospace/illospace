from brain.app.api.services.chat import resolve_user_mentions
from brain.platform.db.models.org import User


ORG_ID = "00000000-0000-0000-0000-000000000001"


def _user(user_id: str, name: str, email: str) -> User:
    return User(
        id=user_id,
        org_id=ORG_ID,
        name=name,
        email=email,
        approved=True,
    )


def test_resolve_user_mentions_matches_initials_and_illo() -> None:
    jean_baptiste = _user(
        "00000000-0000-0000-0000-000000000011",
        "Jean Baptiste Keller",
        "jean.baptiste@example.com",
    )
    maya = _user(
        "00000000-0000-0000-0000-000000000012",
        "Maya Chen",
        "maya@example.com",
    )

    mentioned, illo_invoked = resolve_user_mentions(
        "Can @jb? sync with @illo on this?",
        [jean_baptiste, maya],
    )

    assert [str(user.id) for user in mentioned] == [str(jean_baptiste.id)]
    assert illo_invoked is True


def test_resolve_user_mentions_matches_unique_short_prefix() -> None:
    jbk = _user(
        "00000000-0000-0000-0000-000000000021",
        "jbk",
        "jbk@example.com",
    )
    axel = _user(
        "00000000-0000-0000-0000-000000000022",
        "Axel",
        "axel@example.com",
    )

    mentioned, illo_invoked = resolve_user_mentions("ping @jb about webhooks", [jbk, axel])

    assert [str(user.id) for user in mentioned] == [str(jbk.id)]
    assert illo_invoked is False


def test_resolve_user_mentions_ignores_ambiguous_short_prefix() -> None:
    jane = _user(
        "00000000-0000-0000-0000-000000000031",
        "Jane Doe",
        "jane@example.com",
    )
    jack = _user(
        "00000000-0000-0000-0000-000000000032",
        "Jack Roe",
        "jack@example.com",
    )

    mentioned, _illo_invoked = resolve_user_mentions("cc @ja please", [jane, jack])

    assert mentioned == []


def test_resolve_user_mentions_ignores_ambiguous_exact_alias() -> None:
    alex_smith = _user(
        "00000000-0000-0000-0000-000000000041",
        "Alex Smith",
        "alex.smith@example.com",
    )
    alex_jones = _user(
        "00000000-0000-0000-0000-000000000042",
        "Alex Jones",
        "alex.jones@example.com",
    )

    mentioned, _illo_invoked = resolve_user_mentions("cc @alex please", [alex_smith, alex_jones])

    assert mentioned == []


def test_resolve_user_mentions_matches_full_email_local_handle() -> None:
    alex_smith = _user(
        "00000000-0000-0000-0000-000000000051",
        "Alex Smith",
        "alex.smith@example.com",
    )
    alex_jones = _user(
        "00000000-0000-0000-0000-000000000052",
        "Alex Jones",
        "alex.jones@example.com",
    )

    mentioned, _illo_invoked = resolve_user_mentions(
        "cc @alex.smith please",
        [alex_smith, alex_jones],
    )

    assert [str(user.id) for user in mentioned] == [str(alex_smith.id)]
