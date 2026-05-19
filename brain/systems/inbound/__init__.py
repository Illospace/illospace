"""Inbound coordination foundation."""

from brain.systems.inbound import admin
from brain.systems.inbound.service import (
    InboundValidationError,
    create_domain_projection,
    create_source_policy,
    submit_inbound_envelope,
)

__all__ = [
    "admin",
    "InboundValidationError",
    "create_domain_projection",
    "create_source_policy",
    "submit_inbound_envelope",
]
