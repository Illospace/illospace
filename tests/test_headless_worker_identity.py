from brain.systems.runs.headless_worker_identity import (
    build_headless_worker_thread_id,
    headless_worker_directory_name,
    parse_headless_worker_identity,
)


def test_headless_worker_identity_round_trip_and_production_name():
    digest = "0434cc5ed822d9f7"

    thread_id = build_headless_worker_thread_id(16034, digest)
    directory_name = headless_worker_directory_name(thread_id)

    assert directory_name is not None
    assert directory_name == "headless-worker-16034-0434cc5ed822d9f7"
    assert parse_headless_worker_identity(thread_id) == (16034, digest)
    assert parse_headless_worker_identity(directory_name) == (16034, digest)
    assert parse_headless_worker_identity(
        "headless-worker-16034-0434cc5ed822d9f7"
    ) == (16034, digest)


def test_headless_worker_identity_rejects_unowned_names_without_raising():
    assert parse_headless_worker_identity("idea-16034") is None
    assert headless_worker_directory_name("idea:16034") is None


def test_directory_parser_preserves_existing_digest_acceptance():
    assert parse_headless_worker_identity("headless-worker-001-digest-with-suffix") == (
        1,
        "digest-with-suffix",
    )
