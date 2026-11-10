"""Recorded-shape Slack payloads use invented Acme identities."""

import pytest

from illo.channels.slack.intake import SlackIntake


@pytest.fixture
def intake(app):
    return SlackIntake(app.state.ctx, "U_ACME_ILLO", clip_timeout=0.02, clip_poll=0.001)


@pytest.fixture
def message():
    def make(**fields):
        return {
            "type": "event_callback",
            "event_id": "Ev_ACME",
            "event": {
                "type": "message",
                "channel": "D_ACME_ADA",
                "channel_type": "im",
                "user": "U_ACME_ADA",
                "ts": "1700000000.000001",
                "text": "Give me a lane update",
                **fields,
            },
        }

    return make
