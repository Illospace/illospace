#!/usr/bin/env python3
"""Import reconstructive memories into the brain from a starter kit.

Usage:
    python3 -m brain.app.cli.brain_import --from-export ./export/
    python3 -m brain.app.cli.brain_import --from-export ./export/ --dry-run
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.kernel import config


async def import_from_export(export_dir: str, dry_run: bool = False) -> dict:
    """Import from an exported starter kit directory."""
    from sqlalchemy import text
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    stats = {'memories': 0, 'skills': 0, 'templates': 0, 'schema': False}

    # 1. Apply schema if tables don't exist
    schema_path = os.path.join(export_dir, 'schema.sql')
    if os.path.exists(schema_path):
        async with UnitOfWork() as uow:
            result = await uow.session.execute(text("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'memory_nodes'
                )
            """))
            row = result.mappings().first()
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

    # 2. Load memories
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
                    result = await add_memory(
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

    # 3. Load skills
    skills_path = os.path.join(export_dir, 'skills.json')
    if os.path.exists(skills_path):
        with open(skills_path) as f:
            skills = json.load(f)

        if not dry_run:
            from brain.systems.skills.gate import validate_skill_structure
            async with UnitOfWork() as uow:
                for s in skills:
                    # Validate via centralized skill-creator gate (issue #187)
                    violations = validate_skill_structure(
                        s['name'], s.get('description', ''), s.get('procedure', ''),
                    )
                    if violations:
                        print(f"  [GATE] Skill '{s['name']}' blocked: {'; '.join(violations)}")
                        continue

                    # Upsert by name
                    await uow.session.execute(text("""
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


async def _async_main(args) -> None:
    stats = await import_from_export(args.from_export, dry_run=args.dry_run)
    mode = "DRY RUN" if args.dry_run else "IMPORTED"
    print(f"\n[{mode}] Memories: {stats['memories']}, Skills: {stats['skills']}, "
          f"Templates: {stats['templates']}")


def main():
    parser = argparse.ArgumentParser(description='Import memories into the brain')
    parser.add_argument('--from-export', required=True, help='Import from starter kit directory')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == '__main__':
    main()
