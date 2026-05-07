#!/usr/bin/env python3
"""Import memories into the brain from a starter kit or legacy workspace.

Usage:
    python3 -m brain.app.cli.brain_import --from-export ./export/
    python3 -m brain.app.cli.brain_import --from-workspace /path/to/legacy-workspace/
    python3 -m brain.app.cli.brain_import --from-export ./export/ --dry-run
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.kernel import config


def import_from_export(export_dir: str, dry_run: bool = False) -> dict:
    """Import from an exported starter kit directory."""
    from sqlalchemy import text
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    stats = {'memories': 0, 'skills': 0, 'templates': 0, 'schema': False}

    # 1. Apply schema if tables don't exist
    schema_path = os.path.join(export_dir, 'schema.sql')
    if os.path.exists(schema_path):
        with UnitOfWork() as uow:
            row = uow.session.execute(text("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'memories'
                )
            """)).mappings().first()
            exists = row['exists']

        if not exists and not dry_run:
            print("Creating tables from schema.sql...")
            db_url = os.environ.get('DATABASE_URL', '')
            # Use psql for schema import
            try:
                subprocess.run(
                    ['psql', db_url, '-f', schema_path],
                    capture_output=True, text=True, timeout=30
                )
                stats['schema'] = True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                print("  Warning: Could not run psql, tables may need manual creation")
        elif exists:
            print("Tables already exist, skipping schema.")
        else:
            print("[DRY RUN] Would create tables from schema.sql")

    # 2. Apply scope migration
    migration_path = str(Path(config.BRAIN_DIR) / 'migrations' / '012_scope_column.sql')
    if not dry_run:
        try:
            with UnitOfWork() as uow:
                uow.session.execute(text(
                    "ALTER TABLE memories ADD COLUMN IF NOT EXISTS scope VARCHAR(20) DEFAULT 'personal'"
                ))
        except Exception:
            pass  # Column may already exist

    # 3. Load memories
    memories_path = os.path.join(export_dir, 'memories.jsonl')
    if os.path.exists(memories_path):
        from brain.app.cli.memory import add_memory

        with open(memories_path) as f:
            for line in f:
                if not line.strip():
                    continue
                m = json.loads(line)
                if dry_run:
                    print(f"  [DRY] Would import: [{m.get('memory_type', '?')}] {m['content'][:60]}...")
                else:
                    result = add_memory(
                        content=m['content'],
                        memory_type=m.get('memory_type', 'lesson'),
                        salience=m.get('salience', 5.0),
                        tags=m.get('tags', []),
                        source=m.get('source', 'starter_kit'),
                        scope=m.get('original_scope', 'universal'),
                    )
                    if not result.get('rejected'):
                        stats['memories'] += 1
                    else:
                        print(f"  Rejected: {result.get('reason', 'unknown')}")

    # 4. Load skills
    skills_path = os.path.join(export_dir, 'skills.json')
    if os.path.exists(skills_path):
        with open(skills_path) as f:
            skills = json.load(f)

        if not dry_run:
            from brain.systems.skills.gate import validate_skill_structure
            with UnitOfWork() as uow:
                for s in skills:
                    # Validate via centralized skill-creator gate (issue #187)
                    violations = validate_skill_structure(
                        s['name'], s.get('description', ''), s.get('procedure', ''),
                    )
                    if violations:
                        print(f"  [GATE] Skill '{s['name']}' blocked: {'; '.join(violations)}")
                        continue

                    # Upsert by name
                    uow.session.execute(text("""
                        INSERT INTO skills (name, description, procedure, version, level,
                                           maturity, confidence, pitfalls, refinements, triggers)
                        VALUES (:name, :desc, :proc, :version, :level,
                                :maturity, :confidence, :pitfalls, :refinements, :triggers)
                        ON CONFLICT (name) DO UPDATE SET
                            description = EXCLUDED.description,
                            procedure = EXCLUDED.procedure,
                            pitfalls = EXCLUDED.pitfalls,
                            refinements = EXCLUDED.refinements
                    """), {
                        "name": s['name'], "desc": s.get('description', ''),
                        "proc": s['procedure'], "version": s.get('version', 1),
                        "level": s.get('level', 'cognitive'),
                        "maturity": s.get('maturity', 'emerging'),
                        "confidence": s.get('confidence', 0.3),
                        "pitfalls": json.dumps(s.get('pitfalls', [])),
                        "refinements": json.dumps(s.get('refinements', [])),
                        "triggers": json.dumps(s.get('triggers', [])),
                    })
                    stats['skills'] += 1
        else:
            stats['skills'] = len(skills)
            for s in skills[:5]:
                print(f"  [DRY] Would import skill: {s['name']} ({s.get('maturity', '?')})")

    # 5. Copy optional operator-context templates to the private context dir
    # (don't overwrite existing local files).
    templates_dir = os.path.join(export_dir, 'templates')
    if os.path.isdir(templates_dir):
        context_dir = str(config.AGENT_CONTEXT_DIR)
        for fname in os.listdir(templates_dir):
            dest = os.path.join(context_dir, fname)
            if os.path.exists(dest):
                print(f"  Skipping {fname} (already exists)")
            elif dry_run:
                print(f"  [DRY] Would copy template to private context: {fname}")
            else:
                import shutil
                os.makedirs(context_dir, exist_ok=True)
                shutil.copy2(os.path.join(templates_dir, fname), dest)
                stats['templates'] += 1

    return stats


def import_from_workspace(workspace_dir: str, dry_run: bool = False) -> dict:
    """Import from a raw legacy workspace by parsing markdown files."""
    from brain.systems.memory.scope import classify_scope

    stats = {'total': 0, 'universal': 0, 'personal': 0}
    memories_to_import = []

    # Parse daily logs (legacy: reads from workspace/memory/ for one-time import;
    # ongoing daily logs now live in illo-brain/journal/ — see config.JOURNAL_DIR)
    memory_dir = os.path.join(workspace_dir, 'memory')
    if os.path.isdir(memory_dir):
        for fname in sorted(os.listdir(memory_dir)):
            if fname.endswith('.md'):
                memories_to_import.extend(
                    _parse_markdown_sections(
                        os.path.join(memory_dir, fname), 'daily_log'
                    )
                )

    # Classify and import
    if not dry_run:
        from brain.app.cli.memory import add_memory

    for mem in memories_to_import:
        scope = classify_scope(mem['content'], mem['type'])
        stats['total'] += 1
        stats[scope] += 1

        if dry_run:
            print(f"  [{scope}] [{mem['type']}] {mem['content'][:70]}...")
        else:
            add_memory(
                content=mem['content'],
                memory_type=mem['type'],
                salience=mem.get('salience', 5.0),
                source='workspace_import',
                scope=scope,
            )

    return stats


def _parse_markdown_sections(filepath: str, default_type: str) -> list[dict]:
    """Parse a markdown file into memory-sized chunks by section."""
    with open(filepath) as f:
        content = f.read()

    memories = []
    # Split by ## headers
    sections = re.split(r'\n(?=## )', content)

    for section in sections:
        text = section.strip()
        if len(text) < 30:  # Skip tiny fragments
            continue

        # Split large sections into paragraphs
        paragraphs = [p.strip() for p in text.split('\n\n') if len(p.strip()) > 30]

        if len(paragraphs) <= 3:
            # Small enough to be one memory
            memories.append({
                'content': text,
                'type': default_type,
                'salience': 5.0,
            })
        else:
            # Split into paragraph-level memories
            for para in paragraphs:
                if len(para) > 30:
                    memories.append({
                        'content': para,
                        'type': default_type,
                        'salience': 4.0,
                    })

    return memories


def main():
    parser = argparse.ArgumentParser(description='Import memories into the brain')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--from-export', help='Import from starter kit directory')
    group.add_argument('--from-workspace', help='Import from legacy workspace')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    if args.from_export:
        stats = import_from_export(args.from_export, dry_run=args.dry_run)
        mode = "DRY RUN" if args.dry_run else "IMPORTED"
        print(f"\n[{mode}] Memories: {stats['memories']}, Skills: {stats['skills']}, "
              f"Templates: {stats['templates']}")
    else:
        stats = import_from_workspace(args.from_workspace, dry_run=args.dry_run)
        mode = "DRY RUN" if args.dry_run else "IMPORTED"
        print(f"\n[{mode}] {stats['total']} memories "
              f"({stats['universal']} universal, {stats['personal']} personal)")


if __name__ == '__main__':
    main()
