"""Illo Brain — configuration.

Single source of truth. Reads from environment variables or .env file.
No other module should hardcode paths, credentials, or connection details.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve .env (if python-dotenv available)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    _project_root = Path(__file__).resolve().parents[2]
    # Check project root first (illo_brain/.env), then brain/.env
    for _candidate in [
        _project_root / ".env",
        _project_root / "brain" / ".env",
        _project_root / "core" / ".env",  # legacy location
    ]:
        if _candidate.exists():
            load_dotenv(_candidate)
            break
except ImportError:
    pass  # dotenv not installed — rely on real env vars

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BRAIN_DIR = Path(__file__).resolve().parents[2]  # repository root
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", BRAIN_DIR.parent))

# Runtime-private agent state. Keep operator prompts, generated journals, logs,
# uploads, and local brain exports out of the public source tree by default.
# Set ILLO_PRIVATE_HOME to an absolute or repo-relative path when you want this
# state somewhere else (for example a mounted volume in production).
_PRIVATE_HOME_RAW = os.getenv("ILLO_PRIVATE_HOME", ".illo")
PRIVATE_HOME = Path(_PRIVATE_HOME_RAW)
if not PRIVATE_HOME.is_absolute():
    PRIVATE_HOME = BRAIN_DIR / PRIVATE_HOME

JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", PRIVATE_HOME / "journal"))
AGENT_CONTEXT_DIR = Path(os.getenv("AGENT_CONTEXT_DIR", PRIVATE_HOME / "agent-context"))
AGENT_CHECKLIST_PATH = Path(os.getenv("AGENT_CHECKLIST_PATH", AGENT_CONTEXT_DIR / "pre-flight-checklist.md"))
AGENT_SOUL_PATH = Path(os.getenv("AGENT_SOUL_PATH", AGENT_CONTEXT_DIR / "SOUL.md"))
BRAIN_LOG_DIR = Path(os.getenv("BRAIN_LOG_DIR", PRIVATE_HOME / "logs"))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "illo_memory")
DB_USER = os.getenv("DB_USER", "illo")
DB_PASSWORD = os.getenv("DB_PASSWORD", "illo")

DB_DSN = {
    "host": DB_HOST,
    "port": DB_PORT,
    "dbname": DB_NAME,
    "user": DB_USER,
    "password": DB_PASSWORD,
}

from urllib.parse import quote_plus as _qp

DB_URL = (
    f"postgresql://{_qp(DB_USER)}:{_qp(DB_PASSWORD)}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

VAULT_MASTER_KEY = os.getenv("VAULT_MASTER_KEY", "")

DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Embeddings — three modes:
#   "gpu"  — local GPU server (brain.platform.gpu, fast, requires VRAM)
#   "cpu"  — local tiny model via sentence-transformers (no GPU needed, ~100MB)
#   "api"  — cloud embedding API (Gemini or OpenAI, requires key)
# ---------------------------------------------------------------------------
EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "api")

# GPU mode: local embedding server (requires brain.platform.gpu server running)
GPU_SERVER_URL = os.getenv("GPU_SERVER_URL", "http://127.0.0.1:9800")

# CPU mode: local sentence-transformers model (downloaded on first use)
EMBEDDING_CPU_MODEL = os.getenv("EMBEDDING_CPU_MODEL", "all-MiniLM-L6-v2")  # 384-dim, ~80MB

# API mode: cloud embedding provider
EMBEDDING_API_PROVIDER = os.getenv("EMBEDDING_API_PROVIDER", "gemini")  # "gemini" or "openai"
EMBEDDING_API_KEY = (
    os.getenv("EMBEDDING_API_KEY", "")
    or os.getenv("GEMINI_API_KEY", "")
    or os.getenv("GOOGLE_API_KEY", "")
)
EMBEDDING_API_MODEL = os.getenv("EMBEDDING_API_MODEL", "gemini-embedding-2")

# Dimensions — must match DB vector column.
# Each backend has a natural dimension: gpu=2000, cpu=384 (MiniLM), api=768 (OpenAI/Gemini)
# If not explicitly set, auto-select based on backend.
_EMBEDDING_DIM_DEFAULTS = {"gpu": 2000, "cpu": 384, "api": 768}
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "0")) or _EMBEDDING_DIM_DEFAULTS.get(EMBEDDING_BACKEND, 768)
MEMORY_RERANKER = os.getenv("MEMORY_RERANKER", "weighted")

# Semantic stores intentionally share one embedding model/dimension. Changing
# this value requires a staged migration plus re-embedding; never resize
# populated vectors silently.
MEMORY_SEMANTIC_EMBEDDING_DIM = EMBEDDING_DIM
SUMMARY_SEMANTIC_EMBEDDING_DIM = EMBEDDING_DIM
NARRATIVE_SEMANTIC_EMBEDDING_DIM = EMBEDDING_DIM
SKILL_SEMANTIC_EMBEDDING_DIM = EMBEDDING_DIM
SKILL_TASK_CENTROID_EMBEDDING_DIM = EMBEDDING_DIM

# Emotional embeddings are local heuristic feature vectors, not provider output.
MEMORY_EMOTIONAL_EMBEDDING_DIM = 32

# Cortex idea embeddings are a legacy OpenAI-specific vector field. Keep the
# typmod explicit until a dedicated idea re-embedding migration replaces it.
IDEA_EMBEDDING_DIM = 1536


@dataclass(frozen=True)
class EmbeddingVectorSpec:
    """Policy for one pgvector-backed embedding family."""

    family: str
    dimensions: int
    table: str
    column: str
    configurable: bool
    provider_specific: bool
    notes: str


@dataclass(frozen=True)
class EmbeddingTypmodMismatch:
    """Observed database vector typmod that does not match registry policy."""

    family: str
    table: str
    column: str
    expected_dimensions: int
    actual_dimensions: int | None
    actual_type: str | None


class EmbeddingDimensionError(RuntimeError):
    """Raised when configured embedding dimensions and DB typmods drift."""


def embedding_vector_registry() -> dict[str, EmbeddingVectorSpec]:
    """Return the central embedding dimension registry.

    Semantic families share EMBEDDING_DIM. The emotional and idea families are
    fixed by their generation policy and documented here rather than being
    scattered as raw Vector(N) literals.
    """

    semantic_dim = int(EMBEDDING_DIM)
    return {
        "memory.semantic": EmbeddingVectorSpec(
            family="memory.semantic",
            dimensions=semantic_dim,
            table="memories",
            column="semantic_embedding",
            configurable=True,
            provider_specific=False,
            notes="Shared semantic memory embedding dimension from EMBEDDING_DIM.",
        ),
        "memory.emotional": EmbeddingVectorSpec(
            family="memory.emotional",
            dimensions=MEMORY_EMOTIONAL_EMBEDDING_DIM,
            table="memories",
            column="emotional_embedding",
            configurable=False,
            provider_specific=False,
            notes="Fixed 32-dim local emotion feature vector.",
        ),
        "summary.semantic": EmbeddingVectorSpec(
            family="summary.semantic",
            dimensions=semantic_dim,
            table="memory_summaries",
            column="semantic_embedding",
            configurable=True,
            provider_specific=False,
            notes="Summary retrieval uses the shared semantic embedding space.",
        ),
        "narrative.semantic": EmbeddingVectorSpec(
            family="narrative.semantic",
            dimensions=semantic_dim,
            table="project_narratives",
            column="semantic_embedding",
            configurable=True,
            provider_specific=False,
            notes="Narrative retrieval uses the shared semantic embedding space.",
        ),
        "skill.semantic": EmbeddingVectorSpec(
            family="skill.semantic",
            dimensions=semantic_dim,
            table="skills",
            column="embedding",
            configurable=True,
            provider_specific=False,
            notes="Skill matching uses the shared semantic embedding space.",
        ),
        "skill.task_centroid": EmbeddingVectorSpec(
            family="skill.task_centroid",
            dimensions=semantic_dim,
            table="skills",
            column="task_centroid",
            configurable=True,
            provider_specific=False,
            notes="Skill task centroids are averages in the shared semantic space.",
        ),
        "idea.embedding": EmbeddingVectorSpec(
            family="idea.embedding",
            dimensions=IDEA_EMBEDDING_DIM,
            table="ideas",
            column="embedding",
            configurable=False,
            provider_specific=True,
            notes="Legacy Cortex idea embeddings are intentionally OpenAI-specific.",
        ),
    }


def get_embedding_dimension(family: str) -> int:
    """Return the expected dimension for an embedding family."""

    try:
        return embedding_vector_registry()[family].dimensions
    except KeyError as exc:
        known = ", ".join(sorted(embedding_vector_registry()))
        raise KeyError(f"Unknown embedding vector family {family!r}; known: {known}") from exc


def embedding_database_vector_specs() -> tuple[EmbeddingVectorSpec, ...]:
    """Return registry specs that have a database table/column typmod."""

    return tuple(embedding_vector_registry().values())


def parse_vector_type_dimension(type_name: str | None) -> int | None:
    """Extract N from pgvector type strings such as 'vector(2000)'."""

    if not type_name:
        return None
    match = re.search(
        r"(?:^|[.\\s])vector\((\d+)\)$",
        type_name.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    return int(match.group(1))


def _row_value(row, key: str, index: int = 0):
    if row is None:
        return None
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping[key]
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def fetch_database_vector_type(
    connection,
    table: str,
    column: str,
    *,
    schema: str = "public",
) -> str | None:
    """Read the formatted pgvector type for a table column."""

    from sqlalchemy import text as sa_text

    row = connection.execute(
        sa_text(
            """
            SELECT pg_catalog.format_type(a.atttypid, a.atttypmod) AS vector_type
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema
              AND c.relname = :table
              AND a.attname = :column
              AND NOT a.attisdropped
            """
        ),
        {"schema": schema, "table": table, "column": column},
    ).fetchone()
    return _row_value(row, "vector_type")


def collect_embedding_typmod_mismatches(
    connection,
    *,
    schema: str = "public",
) -> list[EmbeddingTypmodMismatch]:
    """Compare DB pgvector typmods with the embedding registry."""

    mismatches: list[EmbeddingTypmodMismatch] = []
    for spec in embedding_database_vector_specs():
        actual_type = fetch_database_vector_type(
            connection,
            spec.table,
            spec.column,
            schema=schema,
        )
        actual_dimensions = parse_vector_type_dimension(actual_type)
        if actual_dimensions != spec.dimensions:
            mismatches.append(
                EmbeddingTypmodMismatch(
                    family=spec.family,
                    table=spec.table,
                    column=spec.column,
                    expected_dimensions=spec.dimensions,
                    actual_dimensions=actual_dimensions,
                    actual_type=actual_type,
                )
            )
    return mismatches


def validate_embedding_vector_typmods(connection, *, schema: str = "public") -> None:
    """Fail clearly when database vector dimensions drift from policy."""

    mismatches = collect_embedding_typmod_mismatches(connection, schema=schema)
    if not mismatches:
        return

    details = "; ".join(
        (
            f"{m.family} {m.table}.{m.column}: "
            f"expected vector({m.expected_dimensions}), found {m.actual_type or 'missing/non-vector'}"
        )
        for m in mismatches
    )
    raise EmbeddingDimensionError(
        "Embedding dimension registry does not match database vector typmods. "
        f"{details}. Stage a migration and re-embedding plan before changing dimensions."
    )

# ---------------------------------------------------------------------------
# Retrieval tuning
# ---------------------------------------------------------------------------
DEFAULT_RETRIEVAL_LIMIT = int(os.getenv("RETRIEVAL_LIMIT", "10"))
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "50"))

# ---------------------------------------------------------------------------
# Memory graph
# ---------------------------------------------------------------------------
AUTO_EDGE_K = int(os.getenv("AUTO_EDGE_K", "5"))
AUTO_EDGE_MIN_SIM = float(os.getenv("AUTO_EDGE_MIN_SIM", "0.75"))

# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------
DECAY_RATE = float(os.getenv("DECAY_RATE", "0.05"))
DECAY_THRESHOLD = float(os.getenv("DECAY_THRESHOLD", "2.0"))

# ---------------------------------------------------------------------------
# Emotion signals (heuristic layer — supplemented by embeddings)
# ---------------------------------------------------------------------------
EMOTION_SIGNALS = {
    "frustration": {
        "keywords": ["still broken", "again", "i told you", "wrong", "not working", "wtf", "seriously", "come on"],
        "valence": -0.7,
    },
    "joy": {
        "keywords": ["perfect", "exactly", "love it", "amazing", "great job", "nice", "brilliant"],
        "valence": 0.9,
    },
    "urgency": {
        "keywords": ["production", "down", "customer waiting", "asap", "urgent", "broken", "now"],
        "valence": -0.3,
    },
    "satisfaction": {
        "keywords": ["works", "that's it", "good", "solid", "clean", "well done"],
        "valence": 0.6,
    },
    "curiosity": {
        "keywords": ["what if", "how about", "interesting", "let's try", "explore", "think about"],
        "valence": 0.4,
    },
    "disappointment": {
        "keywords": ["expected more", "not what i wanted", "missed", "should have", "thought you would"],
        "valence": -0.5,
    },
    "excitement": {
        "keywords": ["let's go", "can't wait", "this is huge", "game changer", "wow"],
        "valence": 0.8,
    },
    "confusion": {
        "keywords": ["don't understand", "what do you mean", "confused", "unclear", "huh"],
        "valence": -0.2,
    },
    "trust": {
        "keywords": ["i trust you", "go ahead", "your call", "you decide", "handle it"],
        "valence": 0.7,
    },
}
