"""Slack teammate integration for self-hosted Illospace."""

from brain.systems.slack.connector import (
    SlackConnectorConfig,
    ensure_slack_connection,
    process_normalized_slack_envelope,
    process_socket_payload,
    socket_mode_ack,
)
from brain.systems.slack.identity import link_slack_identity, list_slack_identity_mappings
from brain.systems.slack.ingress import normalize_slack_socket_event

__all__ = [
    "SlackConnectorConfig",
    "ensure_slack_connection",
    "normalize_slack_socket_event",
    "process_normalized_slack_envelope",
    "process_socket_payload",
    "link_slack_identity",
    "list_slack_identity_mappings",
    "socket_mode_ack",
]
