"""Briefing — the single owner of dossier assembly for handoff packets.

See ``specs/illo-handoff-packets/``: triage, notify, digest, and on-demand
"brief me" flows all assemble context through this package; callers never
grow their own context-collection logic.
"""

from brain.systems.briefing.core import (
    SOURCE_PRIORITY,
    Dossier,
    DossierBudget,
    DossierItem,
    DossierSection,
    SourcePiece,
    assemble_dossier,
)

__all__ = [
    "SOURCE_PRIORITY",
    "Dossier",
    "DossierBudget",
    "DossierItem",
    "DossierSection",
    "SourcePiece",
    "assemble_dossier",
]
