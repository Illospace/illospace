#!/usr/bin/env python3
"""
Illo Memory System — Nightly Consolidation ("Sleep")

Three phases:
  Phase 1: CONSOLIDATION — Import raw daily logs, compress, embed, auto-associate
  Phase 2: REFLECTION — Retrieval quality and mistake analysis
  Phase 3: SYNTHESIS — Cross-cluster insights, pattern detection

Run nightly via cron or manually:
    python3 consolidate.py [--phase all|consolidate|reflect|synthesize] [--date 2026-03-01]
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, date

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root
import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.embeddings import (
    embed_document, embed_batch, embed_query,
    vec_to_pg,
)

WORKSPACE = str(config.WORKSPACE_ROOT)
MEMORY_DIR = str(config.JOURNAL_DIR)  # illo-brain/journal/ — standalone, no external deps


def _mapping_count(result, key: str = "cnt") -> int:
    """Read an integer count from a mappings result defensively."""
    row = result.mappings().first()
    if not isinstance(row, dict):
        try:
            row = dict(row or {})
        except Exception:
            row = {}
    value = row.get(key, 0)
    try:
        return int(value or 0)
    except Exception:
        return 0

# ============================================================
# Phase 1: CONSOLIDATION
# ============================================================

def phase_consolidation(target_date: date, org_id: str | None = None):
    """Import and compress raw daily logs into the memory graph."""
    with UnitOfWork() as uow:
        result = uow.session.execute(text("""
            INSERT INTO consolidation_runs (run_date, phase, org_id)
            VALUES (:run_date, 'consolidation', :org_id) RETURNING id
        """), {"run_date": target_date, "org_id": org_id})
        run_id = result.mappings().first()["id"]

        memories_created = 0
        edges_created = 0

        # 1. Check for daily log
        daily_file = os.path.join(MEMORY_DIR, f"{target_date.isoformat()}.md")
        if os.path.exists(daily_file):
            print(f"[consolidate] Processing {daily_file}")
            memories_created += import_daily_log(uow, daily_file, target_date)
        else:
            print(f"[consolidate] No daily log for {target_date}")

        # 2. Import domain files if they've changed
        for md_file in glob.glob(os.path.join(MEMORY_DIR, "*.md")):
            basename = os.path.basename(md_file)
            if re.match(r'\d{4}-\d{2}-\d{2}\.md', basename):
                continue
            if basename in ('heartbeat-state.json',):
                continue
            result = uow.session.execute(text("""
                SELECT COUNT(*) as cnt FROM memories
                WHERE source = :source AND NOT archived
            """), {"source": f"import:{basename}"})
            existing = _mapping_count(result)
            if existing == 0:
                print(f"[consolidate] Importing {basename} (first time)")
                memories_created += import_domain_file(uow, md_file)

        # 3. Auto-associate: rebuild similarity edges for new memories
        result = uow.session.execute(text("""
            SELECT id FROM memories
            WHERE created_at >= CURRENT_DATE AND NOT archived
        """))
        new_ids = [r["id"] for r in result.mappings().all()]
        for mid in new_ids:
            edges_created += auto_associate(uow, mid)

        # 4. Decay old memories
        decayed = decay_memories(uow, days=30, threshold=2.0)

        # Complete run
        uow.session.execute(text("""
            UPDATE consolidation_runs
            SET completed_at = NOW(), status = 'completed',
                memories_created = :memories_created, edges_created = :edges_created,
                memories_decayed = :memories_decayed,
                summary = :summary
            WHERE id = :run_id
        """), {
            "memories_created": memories_created,
            "edges_created": edges_created,
            "memories_decayed": decayed,
            "summary": f"Created {memories_created} memories, {edges_created} edges, decayed {decayed}",
            "run_id": run_id,
        })

        # 5. Hierarchical consolidation (episodic → semantic → procedural)
        hier_stats = {"semantic_created": 0, "procedural_created": 0}
        try:
            from brain.systems.cognition.consolidate import run_consolidation
            hier_stats = run_consolidation()
            print(f"[consolidate] Hierarchical: {hier_stats['semantic_created']} semantic, "
                  f"{hier_stats['procedural_created']} procedural, "
                  f"forgetting={hier_stats.get('forgetting', {})}")
        except Exception as e:
            print(f"[consolidate] Hierarchical consolidation failed: {e}")

        print(f"[consolidate] Phase 1 complete: {memories_created} memories, {edges_created} edges, {decayed} decayed")
        return memories_created, edges_created


def import_daily_log(uow, filepath: str, log_date: date) -> int:
    """Import sections from a daily log into compressed memory nodes."""
    with open(filepath) as f:
        content = f.read()

    # Check if already imported
    source_tag = f"import:{os.path.basename(filepath)}"[:50]
    result = uow.session.execute(text(
        "SELECT COUNT(*) as cnt FROM memories WHERE source = :source"
    ), {"source": source_tag})
    if _mapping_count(result) > 0:
        print(f"  Already imported {filepath}, skipping")
        return 0

    sections = re.split(r'^## ', content, flags=re.MULTILINE)
    created = 0

    for section in sections[1:]:
        lines = section.strip().split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()

        if not body or len(body) < 30:
            continue

        dense = compress_text(title, body)
        memory_type, salience = classify_memory(title, body)
        semantic_emb = embed_document(dense)
        tags = extract_tags(title + " " + body)
        tags.append(log_date.isoformat())

        uow.session.execute(text("""
            INSERT INTO memories (content, memory_type, semantic_embedding,
                                salience,
                                source, tags, created_at, decay_eligible)
            VALUES (:content, :memory_type, CAST(:semantic_embedding AS vector),
                    :salience, :source, :tags, :created_at, :decay_eligible)
            RETURNING id
        """), {
            "content": dense, "memory_type": memory_type,
            "semantic_embedding": vec_to_pg(semantic_emb),
            "salience": salience, "source": source_tag, "tags": tags,
            "created_at": datetime.combine(log_date, datetime.min.time()),
            "decay_eligible": memory_type not in ('lesson', 'pattern'),
        })
        created += 1

    return created


def import_domain_file(uow, filepath: str) -> int:
    """Import a domain knowledge file (architecture.md, lessons.md, etc.)."""
    with open(filepath) as f:
        content = f.read()

    filename = os.path.basename(filepath)
    source_tag = f"import:{filename}"[:50]

    sections = re.split(r'^## ', content, flags=re.MULTILINE)
    created = 0

    for section in sections[1:]:
        lines = section.strip().split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()

        if not body or len(body) < 20:
            continue

        dense = compress_text(title, body)
        memory_type, salience = classify_memory(title, body)

        # Domain files get higher base salience
        if 'lesson' in filename.lower():
            memory_type = 'lesson'
            salience = max(salience, 8.0)
        elif 'architecture' in filename.lower():
            memory_type = 'fact'
            salience = max(salience, 6.0)
        elif 'process' in filename.lower():
            memory_type = 'fact'
            salience = max(salience, 5.0)

        semantic_emb = embed_document(dense)

        tags = extract_tags(title + " " + body)
        tags.append(filename.replace('.md', ''))

        uow.session.execute(text("""
            INSERT INTO memories (content, memory_type, semantic_embedding,
                                salience,
                                source, tags, decay_eligible)
            VALUES (:content, :memory_type, CAST(:semantic_embedding AS vector),
                    :salience, :source, :tags, :decay_eligible)
            RETURNING id
        """), {
            "content": dense, "memory_type": memory_type,
            "semantic_embedding": vec_to_pg(semantic_emb),
            "salience": salience, "source": source_tag, "tags": tags,
            "decay_eligible": memory_type not in ('lesson', 'pattern'),
        })
        created += 1

    return created


def auto_associate(uow, memory_id: int, k: int = 5, threshold: float = 0.5) -> int:
    """Create similarity edges for a memory based on embedding proximity."""
    result = uow.session.execute(text("""
        SELECT id, 1 - (semantic_embedding <=> (SELECT semantic_embedding FROM memories WHERE id = :mid)) as similarity
        FROM memories
        WHERE id != :mid AND NOT archived
        ORDER BY semantic_embedding <=> (SELECT semantic_embedding FROM memories WHERE id = :mid)
        LIMIT :k
    """), {"mid": memory_id, "k": k})

    edges = 0
    for row in result.mappings().all():
        if row["similarity"] > threshold:
            uow.session.execute(text("""
                INSERT INTO edges (source_id, target_id, relationship, weight, auto_generated)
                VALUES (:source_id, :target_id, 'similar_to', :weight, TRUE)
                ON CONFLICT (source_id, target_id, relationship) DO UPDATE SET weight = EXCLUDED.weight
            """), {"source_id": memory_id, "target_id": row["id"], "weight": row["similarity"]})
            edges += 1
    return edges


def decay_memories(uow, days: int = 30, threshold: float = 2.0) -> int:
    """Decay old low-salience memories."""
    cutoff = datetime.now() - timedelta(days=days)
    result = uow.session.execute(text("""
        UPDATE memories SET archived = TRUE
        WHERE decay_eligible AND NOT archived
          AND last_accessed < :cutoff AND salience < :threshold
        RETURNING id
    """), {"cutoff": cutoff, "threshold": threshold})
    return result.rowcount


# ============================================================
# Phase 2: REFLECTION
# ============================================================

def phase_reflection(target_date: date, org_id: str | None = None):
    """Analyze performance and retrieval quality."""
    with UnitOfWork() as uow:
        result = uow.session.execute(text("""
            INSERT INTO consolidation_runs (run_date, phase, org_id)
            VALUES (:run_date, 'reflection', :org_id) RETURNING id
        """), {"run_date": target_date, "org_id": org_id})
        run_id = result.mappings().first()["id"]

        # 1. Retrieval quality analysis
        result = uow.session.execute(text("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE feedback = 'hit') as hits,
                   COUNT(*) FILTER (WHERE feedback = 'miss') as misses,
                   COUNT(*) FILTER (WHERE feedback = 'partial') as partial
            FROM retrieval_log
            WHERE timestamp::date = :target_date
        """), {"target_date": target_date})
        retrieval = result.mappings().first()

        # 2. Check for known-mistake recurrence
        result = uow.session.execute(text("""
            SELECT COUNT(*) as accessed_lessons
            FROM memories
            WHERE memory_type = 'lesson' AND last_accessed::date = :target_date
        """), {"target_date": target_date})
        lessons_accessed = result.mappings().first()["accessed_lessons"]

        # 3. Compute competence scores
        competence = compute_competence(uow, target_date)

        # 4. Generate reflection notes
        notes = []
        if retrieval["total"] and retrieval["total"] > 0:
            hit_rate = (retrieval["hits"] or 0) / retrieval["total"]
            notes.append(f"Retrieval hit rate: {hit_rate:.0%} ({retrieval['hits']}/{retrieval['total']})")
            if hit_rate < 0.5:
                notes.append("LOW retrieval quality — review embedding/indexing.")

        # 6. Write daily metrics
        uow.session.execute(text("""
            INSERT INTO daily_metrics (metric_date, retrieval_attempts, retrieval_hits, retrieval_misses,
                competence_architecture, competence_debugging, competence_frontend,
                competence_provider_apis, competence_communication, competence_proactivity,
                reflection_notes)
            VALUES (:metric_date, :retrieval_attempts, :retrieval_hits, :retrieval_misses,
                :competence_architecture, :competence_debugging, :competence_frontend,
                :competence_provider_apis, :competence_communication, :competence_proactivity,
                :reflection_notes)
            ON CONFLICT (metric_date) DO UPDATE SET
                retrieval_attempts = EXCLUDED.retrieval_attempts,
                retrieval_hits = EXCLUDED.retrieval_hits, retrieval_misses = EXCLUDED.retrieval_misses,
                competence_architecture = EXCLUDED.competence_architecture,
                competence_debugging = EXCLUDED.competence_debugging,
                competence_frontend = EXCLUDED.competence_frontend,
                competence_provider_apis = EXCLUDED.competence_provider_apis,
                competence_communication = EXCLUDED.competence_communication,
                competence_proactivity = EXCLUDED.competence_proactivity,
                reflection_notes = EXCLUDED.reflection_notes
        """), {
            "metric_date": target_date,
            "retrieval_attempts": retrieval["total"] or 0,
            "retrieval_hits": retrieval["hits"] or 0,
            "retrieval_misses": retrieval["misses"] or 0,
            "competence_architecture": competence.get("architecture"),
            "competence_debugging": competence.get("debugging"),
            "competence_frontend": competence.get("frontend"),
            "competence_provider_apis": competence.get("provider_apis"),
            "competence_communication": competence.get("communication"),
            "competence_proactivity": competence.get("proactivity"),
            "reflection_notes": "\n".join(notes) if notes else "No notable patterns today.",
        })

        # Complete run
        uow.session.execute(text("""
            UPDATE consolidation_runs
            SET completed_at = NOW(), status = 'completed',
                summary = :summary
            WHERE id = :run_id
        """), {"summary": "\n".join(notes), "run_id": run_id})

        print(f"[reflect] Phase 2 complete: {len(notes)} observations")
        for note in notes:
            print(f"  → {note}")

        return notes


def compute_competence(uow, target_date: date) -> dict:
    """Compute competence scores by domain based on memory access patterns and mistake tracking."""
    domains = {
        "architecture": ["architecture", "stack", "infrastructure", "deployment"],
        "debugging": ["bug", "fix", "debugging", "trace", "data-assumption"],
        "frontend": ["frontend", "react", "shopify", "ui"],
        "provider_apis": ["provider", "api", "model", "v2"],
        "communication": ["operator", "preference", "process"],
        "proactivity": ["proactive", "heartbeat", "monitoring"],
    }

    competence = {}
    for domain, tag_list in domains.items():
        result = uow.session.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE memory_type = 'lesson') as lessons,
                COUNT(*) FILTER (WHERE memory_type = 'pattern') as patterns,
                AVG(salience) as avg_salience,
                SUM(access_count) as total_access
            FROM memories
            WHERE NOT archived AND tags && :tag_list
        """), {"tag_list": tag_list})
        stats = result.mappings().first()

        if stats["lessons"] or stats["patterns"]:
            score = min(1.0, (
                (stats["lessons"] or 0) * 0.1 +
                (stats["patterns"] or 0) * 0.2 +
                float(stats["avg_salience"] or 5) / 10.0 * 0.3 +
                min((stats["total_access"] or 0), 50) / 50 * 0.4
            ))
            competence[domain] = round(score, 2)
        else:
            competence[domain] = 0.3

    return competence


# ============================================================
# Phase 3: SYNTHESIS
# ============================================================

def phase_synthesis(target_date: date, org_id: str | None = None):
    """Look for cross-cluster patterns and generate insights."""
    with UnitOfWork() as uow:
        result = uow.session.execute(text("""
            INSERT INTO consolidation_runs (run_date, phase, org_id)
            VALUES (:run_date, 'synthesis', :org_id) RETURNING id
        """), {"run_date": target_date, "org_id": org_id})
        run_id = result.mappings().first()["id"]

        patterns_detected = 0

        # 1. Find topic clusters with high density
        result = uow.session.execute(text("""
            SELECT unnest(tags) as tag, COUNT(*) as cnt, AVG(salience) as avg_sal
            FROM memories WHERE NOT archived
            GROUP BY tag
            HAVING COUNT(*) >= 4
            ORDER BY cnt DESC
        """))
        hot_topics = result.mappings().all()

        # 2. Detect potential contradictions (with auto-edge creation)
        potential_contradictions = []
        try:
            from brain.systems.cognition.graph import detect_contradictions
            potential_contradictions = detect_contradictions(uow.session, limit=10)
        except Exception as e:
            print(f"[synthesis] Graph contradiction detection failed: {e}")

        summary_parts = []
        if patterns_detected > 0:
            summary_parts.append(f"{patterns_detected} new patterns detected")
        if hot_topics:
            top_tags = [f"{t['tag']}({t['cnt']})" for t in hot_topics[:5]]
            summary_parts.append(f"Hot topics: {', '.join(top_tags)}")
        if potential_contradictions:
            summary_parts.append(f"{len(potential_contradictions)} potential contradictions found")

        summary = "; ".join(summary_parts) if summary_parts else "No new patterns detected"

        uow.session.execute(text("""
            UPDATE consolidation_runs
            SET completed_at = NOW(), status = 'completed',
                patterns_detected = :patterns_detected, summary = :summary
            WHERE id = :run_id
        """), {"patterns_detected": patterns_detected, "summary": summary, "run_id": run_id})

        print(f"[synthesis] Phase 3 complete: {summary}")
        return patterns_detected


# ============================================================
# Helper Functions
# ============================================================

def compress_text(title: str, body: str) -> str:
    """Compress verbose text into dense memory representation."""
    clean = re.sub(r'\s+', ' ', body).strip()
    clean = re.sub(r'[#*`_~]', '', clean)
    clean = re.sub(r'^[-•]\s*', '', clean, flags=re.MULTILINE)
    return f"[{title}] {clean[:400]}"


def classify_memory(title: str, body: str) -> tuple:
    """Classify memory type and estimate salience. Returns (type, salience)."""
    combined = (title + " " + body).lower()

    if any(w in combined for w in ['lesson', 'learned', 'mistake', 'never again', 'root cause']):
        return ('lesson', 8.0)
    elif any(w in combined for w in ['decided', 'decision', 'chose', 'going with', 'approach']):
        return ('decision', 7.0)
    elif any(w in combined for w in ['bug', 'fix', 'broke', 'hotfix', 'debug']):
        return ('episode', 6.0)
    elif any(w in combined for w in ['prefers', 'values', 'likes', 'wants', 'preference']):
        return ('preference', 7.0)
    elif any(w in combined for w in ['pattern', 'recurring', 'always', 'every time']):
        return ('pattern', 8.0)
    elif any(w in combined for w in ['architecture', 'stack', 'infrastructure', 'system']):
        return ('fact', 5.0)
    else:
        return ('episode', 4.0)


def extract_tags(text: str) -> list:
    """Extract relevant tags from text."""
    text_lower = text.lower()
    tag_map = {
        'v2': ['v2', 'v2 path', 'v2 adapter'],
        'bug': ['bug', 'broken', 'error', 'issue'],
        'frontend': ['react', 'frontend', 'ui', 'component'],
        'backend': ['fastapi', 'backend', 'api', 'endpoint'],
        'shopify': ['shopify', 'remix', 'app'],
        'provider': ['provider', 'model', 'openai', 'anthropic'],
        'deployment': ['deploy', 'staging', 'production', 'infra'],
        'operator': ['operator'],
        'architecture': ['architecture', 'system', 'design'],
        'process': ['tdd', 'test', 'workflow', 'process'],
        'data-assumption': ['assumed', 'assumption', 'trace', 'verify', 'actual value'],
    }

    tags = []
    for tag, keywords in tag_map.items():
        if any(kw in text_lower for kw in keywords):
            tags.append(tag)

    return tags


# ============================================================
# Wake-up Index Generator
# ============================================================

def generate_index(output_path: str = None) -> str:
    """Generate a lightweight wake-up index file."""
    with UnitOfWork() as uow:
        # High-salience memories by type
        result = uow.session.execute(text("""
            SELECT id, content, memory_type, salience, tags
            FROM memories
            WHERE NOT archived AND superseded_by IS NULL AND salience >= 5
            ORDER BY salience DESC, last_accessed DESC
            LIMIT 50
        """))
        memories = result.mappings().all()

        # Stats
        result = uow.session.execute(text("SELECT COUNT(*) as total FROM memories WHERE NOT archived"))
        total = result.mappings().first()["total"]
        result = uow.session.execute(text("SELECT COUNT(*) as total FROM edges"))
        total_edges = result.mappings().first()["total"]

    # Build index
    lines = [
        "# Illo Memory Index",
        f"# Generated: {datetime.now().isoformat()}",
        f"# Active memories: {total} | Edges: {total_edges}",
        ""
    ]

    # Group by type
    by_type = {}
    for m in memories:
        t = m["memory_type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(m)

    type_order = ['pattern', 'lesson', 'preference', 'decision', 'fact', 'episode', 'insight']
    for t in type_order:
        if t in by_type:
            lines.append(f"## {t.upper()}S")
            for m in by_type[t]:
                lines.append(f"- [#{m['id']} s:{m['salience']:.0f}] {m['content'][:120]}")
            lines.append("")

    index_text = "\n".join(lines)

    if output_path:
        with open(output_path, 'w') as f:
            f.write(index_text)
        print(f"[index] Written to {output_path}")

    return index_text


# ============================================================
# Main
# ============================================================

def _get_all_users() -> list[dict]:
    """Return all users for per-user nightly runs."""
    try:
        from brain.systems.auth.users import get_all_users
        return get_all_users()
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser(description="Illo Memory Consolidation")
    parser.add_argument("--phase", default="all", choices=["all", "consolidate", "reflect", "synthesize", "index"])
    parser.add_argument("--date", help="Target date (YYYY-MM-DD), default yesterday")
    parser.add_argument("--user-id", help="Run for specific user only (UUID)")
    parser.add_argument("--all-users", action="store_true",
                        help="Run per-user, staggered (multiplayer mode)")
    args = parser.parse_args()

    if args.date:
        target = date.fromisoformat(args.date)
    else:
        target = date.today() - timedelta(days=1)

    if args.all_users:
        from brain.systems.auth.users import get_all_orgs
        orgs = get_all_orgs()
        print(f"{'='*60}")
        print(f"ILLO MEMORY CONSOLIDATION (MULTIPLAYER) — {target}")
        print(f"Running for {len(orgs)} org(s)")
        print(f"{'='*60}")
        for i, org in enumerate(orgs):
            org_id = str(org["id"])
            print(f"\n--- Org: {org['name']} ({i+1}/{len(orgs)}) ---")
            if args.phase in ("all", "consolidate"):
                phase_consolidation(target, org_id=org_id)
            if args.phase in ("all", "reflect"):
                phase_reflection(target, org_id=org_id)
            if args.phase in ("all", "synthesize"):
                phase_synthesis(target, org_id=org_id)
            # Divergence detection (multiplayer only)
            if args.phase == "all":
                try:
                    from brain.jobs.pipelines.divergence import detect_divergence, store_divergence_results
                    overlaps = detect_divergence(target, org_id=org_id)
                    if overlaps:
                        print(f"  [divergence] Found {len(overlaps)} overlap(s):")
                        for o in overlaps:
                            print(f"    → {o['suggestion']}")
                        store_divergence_results(target, org_id, overlaps)
                    else:
                        print("  [divergence] No significant topic overlap detected")
                except Exception as exc:
                    print(f"  [divergence] Skipped: {exc}")
    else:
        print(f"{'='*60}")
        print(f"ILLO MEMORY CONSOLIDATION — {target}")
        print(f"{'='*60}")

        if args.phase in ("all", "consolidate"):
            phase_consolidation(target)

        if args.phase in ("all", "reflect"):
            phase_reflection(target)

        if args.phase in ("all", "synthesize"):
            phase_synthesis(target)

    if args.phase in ("all", "index"):
        index_path = os.path.join(str(config.PRIVATE_HOME), "WAKEUP_INDEX.md")
        generate_index(index_path)

    print(f"\n{'='*60}")
    print(f"CONSOLIDATION COMPLETE")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
