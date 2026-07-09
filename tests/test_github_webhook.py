"""Tests for brain/systems/inbound/github_webhook.py — pure webhook translation."""

import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.inbound.github_webhook import (
    GITHUB_ENVELOPE_KIND,
    github_event_to_envelope,
    verify_signature,
)


def _sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


class TestVerifySignature:
    def test_valid_signature_passes(self):
        secret, body = b"s3cr3t", b'{"a":1}'
        assert verify_signature(secret, body, _sign(secret, body)) is True

    def test_accepts_str_secret_and_body(self):
        assert verify_signature("s3cr3t", '{"a":1}', _sign(b"s3cr3t", b'{"a":1}')) is True

    def test_wrong_secret_fails(self):
        body = b'{"a":1}'
        assert verify_signature(b"wrong", body, _sign(b"right", body)) is False

    def test_tampered_body_fails(self):
        secret = b"s"
        sig = _sign(secret, b'{"a":1}')
        assert verify_signature(secret, b'{"a":2}', sig) is False

    def test_missing_or_malformed_header_fails(self):
        assert verify_signature(b"s", b"b", None) is False
        assert verify_signature(b"s", b"b", "") is False
        assert verify_signature(b"s", b"b", "md5=deadbeef") is False

    def test_empty_secret_fails(self):
        assert verify_signature("", b"b", "sha256=x") is False

    def test_non_ascii_header_returns_false_not_raise(self):
        # Starlette decodes header values as latin-1, so the header can carry
        # bytes >= 0x80. Must return False, not raise TypeError (→ unhandled 500).
        assert verify_signature(b"s", b"b", "sha256=\xff\xfe\x80") is False


class TestEventToEnvelope:
    def test_issue_opened(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "Illospace/uwear-backend"},
            "issue": {
                "number": 42,
                "title": "Login is broken",
                "html_url": "https://github.com/Illospace/uwear-backend/issues/42",
                "state": "open",
                "node_id": "I_abc",
                "updated_at": "2026-07-08T10:00:00Z",
                "user": {"login": "alice"},
            },
        }
        env = github_event_to_envelope("issues", payload, delivery_id="d-1")
        assert env["origin"] == "github:Illospace/uwear-backend"
        assert env["kind"] == GITHUB_ENVELOPE_KIND
        assert env["idempotency_key"] == "github:d-1"
        assert env["hints"]["number"] == 42
        assert env["hints"]["action"] == "opened"
        assert env["hints"]["source_updated_at"] == "2026-07-08T10:00:00Z"
        assert env["hints"]["author"] == "alice"
        assert "Login is broken" in env["summary"]
        assert env["payload"] is payload  # full payload carried for the projection

    def test_pull_request_closed(self):
        payload = {
            "action": "closed",
            "repository": {"full_name": "o/r"},
            "pull_request": {
                "number": 7, "title": "Add feature", "html_url": "u",
                "state": "closed", "node_id": "PR_1",
                "updated_at": "2026-07-08T11:00:00Z", "user": {"login": "bob"},
            },
        }
        env = github_event_to_envelope("pull_request", payload)
        assert env["hints"]["event"] == "pull_request"
        assert env["hints"]["state"] == "closed"
        assert env["hints"]["number"] == 7
        assert env["idempotency_key"] is None  # no delivery id

    def test_issue_comment_prefers_comment_fields(self):
        payload = {
            "action": "created",
            "repository": {"full_name": "o/r"},
            "issue": {"number": 5, "title": "Bug", "html_url": "issue-url",
                      "user": {"login": "author"}, "updated_at": "old"},
            "comment": {"html_url": "comment-url", "user": {"login": "commenter"},
                        "updated_at": "2026-07-08T12:00:00Z"},
        }
        env = github_event_to_envelope("issue_comment", payload)
        assert env["hints"]["url"] == "comment-url"
        assert env["hints"]["author"] == "commenter"
        assert env["hints"]["source_updated_at"] == "2026-07-08T12:00:00Z"

    def test_unsupported_event_returns_none(self):
        assert github_event_to_envelope("star", {"action": "created"}) is None

    def test_missing_repo_defaults_origin(self):
        env = github_event_to_envelope("issues", {"action": "opened", "issue": {"number": 1}})
        assert env["origin"] == "github"
