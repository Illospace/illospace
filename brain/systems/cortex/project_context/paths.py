"""Stable Project Context path helper surface.

New code should import from ``permissions`` or ``contract_paths`` directly when
it needs a narrower dependency. This module intentionally remains as a small
facade for older call sites and external tests.
"""
from brain.systems.cortex.project_context.contract_paths import *  # noqa: F401,F403
from brain.systems.cortex.project_context.permissions import *  # noqa: F401,F403
