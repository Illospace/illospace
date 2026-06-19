"""Active, source-backed reconstructive memory system."""

from brain.systems.reconstructive_memory.controller import reconstruct_memory
from brain.systems.reconstructive_memory.contracts import EvidencePack, EvidenceItem, ReconstructionTraceStep
from brain.systems.reconstructive_memory.ingestion import IngestedMemorySource, ingest_memory_source

__all__ = [
    "EvidenceItem",
    "EvidencePack",
    "IngestedMemorySource",
    "ReconstructionTraceStep",
    "ingest_memory_source",
    "reconstruct_memory",
]
