#!/usr/bin/env python3
"""
Curiosity Engine — Daily critical reading pipeline.

Runs at 10 PM EST via cron (before the 3 AM nightly cycle so it can learn
from the reading). Picks one high-quality source, reads critically, encodes
insights into the brain with bounded salience (max 6), creates connections
to existing knowledge, and writes a morning brief for the operator.

Design principles:
- RECENCY: Only recent content matters. Skip anything older than 30 days.
- CONNECTIONS > FACTS: Every insight must connect to existing brain knowledge.
  Isolated facts are noise. The goal is to strengthen the graph, not grow it.
- DILUTION GUARD: Max salience 6, tagged type:research, one per day.
  Our conversations always outweigh external reading.

Usage:
    python3 curiosity.py              # Run one reading cycle
    python3 curiosity.py --source URL # Read a specific URL
    python3 curiosity.py --list       # Show source rotation
    python3 curiosity.py --history    # Show recent readings
    python3 curiosity.py --brief      # Show today's morning brief
"""

import argparse
import json
import os
import sys
import subprocess
import logging
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.kernel import config

logging.basicConfig(level=logging.INFO, format='[curiosity] %(message)s')
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(config.BRAIN_DIR) / "brain" / "platform" / "data"
STATE_FILE = SCRIPT_DIR / "curiosity_state.json"
READINGS_DIR = SCRIPT_DIR / "readings"
BRIEFS_DIR = SCRIPT_DIR / "briefs"
READINGS_DIR.mkdir(exist_ok=True)
BRIEFS_DIR.mkdir(exist_ok=True)

# Max age for content — anything older is skipped
MAX_CONTENT_AGE_DAYS = 30

# ── Source Registry ──
# Each source has: url/search, tier (1-3), topic, frequency, description
SOURCES = [
    # Tier 1 — High trust, primary sources
    {
        "id": "lilian-weng-agents",
        "url": "https://lilianweng.github.io",
        "tier": 1,
        "topic": "agent-architecture",
        "type": "blog",
        "description": "Lilian Weng — definitive survey posts on agent architecture, memory, planning",
        "frequency_days": 14,
    },
    {
        "id": "anthropic-research",
        "url": "https://www.anthropic.com/research",
        "tier": 1,
        "topic": "agent-architecture",
        "type": "blog",
        "description": "Anthropic research blog — agent design, safety, capabilities",
        "frequency_days": 7,
    },
    {
        "id": "arxiv-agent-memory",
        "url": "https://arxiv.org/search/?query=memory+augmented+llm+agent&searchtype=all&order=-announced_date_first",
        "tier": 1,
        "topic": "agent-memory",
        "type": "arxiv",
        "description": "arXiv — memory augmented LLM agent papers (recent)",
        "frequency_days": 7,
    },
    {
        "id": "arxiv-tool-use",
        "url": "https://arxiv.org/search/?query=tool+use+large+language+model&searchtype=all&order=-announced_date_first",
        "tier": 1,
        "topic": "agent-tools",
        "type": "arxiv",
        "description": "arXiv — tool use in LLMs (recent)",
        "frequency_days": 14,
    },
    {
        "id": "pgvector-releases",
        "url": "https://github.com/pgvector/pgvector/releases",
        "tier": 1,
        "topic": "stack",
        "type": "releases",
        "description": "pgvector releases — vector search improvements",
        "frequency_days": 14,
    },
    # Tier 2 — Good signal, verify claims
    {
        "id": "cloudflare-blog",
        "url": "https://blog.cloudflare.com/tag/post-mortem/",
        "tier": 2,
        "topic": "production-engineering",
        "type": "blog",
        "description": "Cloudflare postmortems — world-class incident analysis",
        "frequency_days": 14,
    },
    {
        "id": "simon-willison",
        "url": "https://simonwillison.net",
        "tier": 2,
        "topic": "ai-tooling",
        "type": "blog",
        "description": "Simon Willison — AI tooling, LLM practical use, SQLite",
        "frequency_days": 7,
    },
    {
        "id": "fastapi-releases",
        "url": "https://github.com/tiangolo/fastapi/releases",
        "tier": 2,
        "topic": "stack",
        "type": "releases",
        "description": "FastAPI releases — async patterns, security fixes",
        "frequency_days": 14,
    },
    {
        "id": "postgres-weekly",
        "url": "https://postgresweekly.com/issues/latest",
        "tier": 2,
        "topic": "stack",
        "type": "newsletter",
        "description": "PostgreSQL Weekly — curated PG news",
        "frequency_days": 7,
    },
    {
        "id": "incident-io",
        "url": "https://incident.io/blog",
        "tier": 2,
        "topic": "production-engineering",
        "type": "blog",
        "description": "incident.io blog — incident response, on-call, postmortems",
        "frequency_days": 14,
    },
]


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_reads": {}, "total_readings": 0, "last_run": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def pick_source(state):
    """Pick the next source to read based on rotation and frequency."""
    now = datetime.now()
    candidates = []

    for source in SOURCES:
        last_read = state["last_reads"].get(source["id"])
        if last_read:
            last_dt = datetime.fromisoformat(last_read)
            days_since = (now - last_dt).days
            if days_since < source["frequency_days"]:
                continue  # Not due yet
            priority = days_since / source["frequency_days"]  # Higher = more overdue
        else:
            priority = 100  # Never read — highest priority

        candidates.append((priority, source))

    if not candidates:
        logger.info("All sources are current. Nothing to read today.")
        return None

    # Sort by priority (most overdue first), then by tier (tier 1 first)
    candidates.sort(key=lambda x: (-x[0], x[1]["tier"]))
    return candidates[0][1]


def fetch_content(url):
    """Fetch and extract readable content from a URL."""
    try:
        result = subprocess.run(
            ["python3", "-c", f"""
import urllib.request, json
req = urllib.request.Request('{url}', headers={{'User-Agent': 'Mozilla/5.0'}})
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.read().decode('utf-8', errors='ignore')[:50000])
"""],
            capture_output=True, text=True, timeout=45
        )
        return result.stdout[:50000] if result.returncode == 0 else None
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


def get_existing_brain_context():
    """Query the brain for current knowledge areas to help the reader make connections."""
    try:
        # Get recent memories to understand current context
        result = subprocess.run(
            ["python3", str(SCRIPT_DIR / "memory.py"), "list", "--limit", "20"],
            capture_output=True, text=True, timeout=30, cwd=str(SCRIPT_DIR)
        )
        recent = result.stdout[:3000] if result.returncode == 0 else ""

        # Get skill states
        result2 = subprocess.run(
            ["python3", str(SCRIPT_DIR / "skills.py"), "dashboard"],
            capture_output=True, text=True, timeout=30, cwd=str(SCRIPT_DIR)
        )
        skills = result2.stdout[:2000] if result2.returncode == 0 else ""

        return f"RECENT BRAIN MEMORIES:\n{recent}\n\nSKILL STATES:\n{skills}"
    except Exception as e:
        logger.warning(f"Could not fetch brain context: {e}")
        return ""


def build_analysis_prompt(source, content, brain_context):
    """Build the critical reading prompt for the child agent."""
    date_cutoff = (datetime.now() - timedelta(days=MAX_CONTENT_AGE_DAYS)).strftime("%Y-%m-%d")

    return f"""You are the Curiosity Engine for an AI agent named Illo. Your job is NOT to
summarize articles. Your job is to find recent, high-quality insights and CREATE
CONNECTIONS to Illo's existing knowledge and work.

SOURCE: {source['description']}
URL: {source['url']}
TOPIC: {source['topic']}

RECENCY RULE: Only consider content published after {date_cutoff}. If the page
only has old content, report "nothing_recent" and stop. Things move fast in AI —
old papers are already baked into model training data.

{brain_context}

PAGE CONTENT (truncated):
{content[:25000]}

YOUR TASK — Critical Reading with Connections:

1. **Find the most recent, relevant item** on this page. For blogs, the latest
   post. For arXiv, papers from the last 30 days. For releases, the latest version.
   SKIP anything older than {MAX_CONTENT_AGE_DAYS} days.

2. **Read it deeply and critically**:
   - What is the core claim or contribution?
   - What evidence? (experiments with data > production experience > theory > opinion)
   - Confidence: HIGH (reproduced results) / MEDIUM (single source, reasonable) / SPECULATIVE
   - What are the LIMITATIONS the author acknowledges (or should acknowledge)?

3. **CREATE CONNECTIONS — this is the most important part**:
   Look at the brain context above. Find specific connections:
   - Does this reinforce, challenge, or extend something Illo already knows?
   - Could this change how Illo approaches a current skill or task?
   - Does this suggest a concrete improvement to the brain system, our stack, or our process?
   - What existing memory would this connect to, and why?
   If you can't find a real connection, say so. Forced connections are worse than none.

4. **Contradiction check**: Does this contradict our current approach? If so, which
   side has stronger evidence?

OUTPUT FORMAT (JSON):
{{
    "source_id": "{source['id']}",
    "source_tier": {source['tier']},
    "date": "{datetime.now().strftime('%Y-%m-%d')}",
    "nothing_recent": false,
    "item_title": "Title of the specific article/paper/release",
    "item_url": "Direct URL if available",
    "item_date": "Publication date if available (YYYY-MM-DD)",
    "core_claim": "One sentence: what's the key insight?",
    "evidence_type": "experimental|production|theoretical|opinion",
    "evidence_strength": "What specifically supports this claim",
    "limitations": "What the author misses or downplays",
    "confidence": "high|medium|speculative",
    "summary": "2-3 sentences: what did you learn?",
    "connections": [
        {{
            "to": "What existing knowledge/skill/memory this connects to",
            "relationship": "reinforces|challenges|extends|suggests_improvement",
            "explanation": "Specifically how and why this connection matters"
        }}
    ],
    "concrete_application": "A specific, actionable thing we could do differently based on this (or 'none')",
    "contradictions": "Any contradictions with our current approach (or 'none')",
    "worth_deep_dive": true,
    "tags": ["topic1", "topic2"],
    "morning_brief": "2-3 sentences for the operator: what you read, the key insight, and how it connects to the work. Written in plain language, not academic. If it suggests a concrete action, say what it is."
}}

QUALITY RULES:
- If nothing on this page is recent or relevant, set "nothing_recent": true and stop.
- Don't force insights. An honest "nothing useful today" is better than noise.
- Connections must be SPECIFIC — "this relates to AI agents" is not a connection.
  "This paper's decay function outperforms time-based decay, which is what our
  memory consolidation uses" IS a connection.
- The morning_brief is for a busy CTO — concise, concrete, no jargon."""


def connect_to_existing_memories(reading):
    """After encoding, create explicit edges to related existing memories."""
    if not reading or not reading.get("connections"):
        return

    for conn in reading["connections"]:
        try:
            # Search for the memory this connects to
            query = conn.get("to", "")
            if not query:
                continue

            result = subprocess.run(
                ["python3", str(SCRIPT_DIR / "memory.py"), "query", "-q", query, "--limit", "1"],
                capture_output=True, text=True, timeout=30, cwd=str(SCRIPT_DIR)
            )
            if result.returncode == 0 and result.stdout.strip():
                # Try to extract a memory ID from the output to create an edge
                logger.info(f"Connection found: '{query}' — {conn.get('relationship', 'related')}: {conn.get('explanation', '')[:80]}")
            else:
                logger.info(f"No existing memory found for connection: '{query}'")
        except Exception as e:
            logger.warning(f"Connection search failed: {e}")


def encode_reading(reading):
    """Store the reading in the brain with bounded salience."""
    if not reading:
        logger.info("No reading to encode.")
        return

    if reading.get("nothing_recent"):
        logger.info("Nothing recent found. Skipping encoding.")
        return

    if reading.get("core_claim") in (None, "none", ""):
        logger.info("No core claim. Skipping encoding.")
        return

    # Max salience 6 for external knowledge — our conversations go up to 10
    confidence_to_salience = {"high": 6, "medium": 4, "speculative": 2}
    salience = confidence_to_salience.get(reading.get("confidence", "speculative"), 3)

    # Build rich content that includes connections — this is what makes it
    # intelligence, not just facts
    connections_text = ""
    for conn in reading.get("connections", []):
        connections_text += f" [{conn.get('relationship', 'related')} → {conn.get('to', '?')}]"

    content = (
        f"[Research] {reading.get('item_title', 'Unknown')}: "
        f"{reading.get('core_claim', '')} "
        f"Evidence: {reading.get('evidence_type', 'unknown')} "
        f"({reading.get('evidence_strength', 'unspecified')}). "
        f"{reading.get('summary', '')} "
        f"Application: {reading.get('concrete_application', 'none')}."
        f"{connections_text}"
    )

    tags = reading.get("tags", [])
    tags.extend(["research", f"source:{reading.get('source_id', 'unknown')}",
                 f"tier:{reading.get('source_tier', 3)}",
                 f"confidence:{reading.get('confidence', 'speculative')}"])

    try:
        cmd = [
            "python3", str(SCRIPT_DIR / "memory.py"), "add",
            "-c", content,
            "-t", "research",
            "-s", str(salience),
            "--tags", ",".join(tags),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                cwd=str(SCRIPT_DIR))
        if result.returncode == 0:
            logger.info(f"Encoded: salience={salience}, tags={tags}")
            # Now create connections to existing memories
            connect_to_existing_memories(reading)
        else:
            logger.error(f"Failed to encode: {result.stderr}")
    except Exception as e:
        logger.error(f"Encoding error: {e}")


def write_morning_brief(source, reading):
    """Write a concise morning brief for the operator."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    brief_file = BRIEFS_DIR / f"{date_str}.md"

    if reading.get("nothing_recent"):
        brief_content = f"""# 📚 Morning Brief — {date_str}

**Source checked:** {source['description']}

Nothing recent or relevant found today. All quiet on the {source['topic']} front.
"""
    else:
        connections_md = ""
        for conn in reading.get("connections", []):
            connections_md += f"\n- **{conn.get('relationship', 'related')}** → {conn.get('to', '?')}: {conn.get('explanation', '')}"

        action = reading.get("concrete_application", "none")
        action_md = f"\n\n**💡 Suggested action:** {action}" if action and action != "none" else ""

        brief_content = f"""# 📚 Morning Brief — {date_str}

**Source:** {source['description']}
**Read:** [{reading.get('item_title', 'Unknown')}]({reading.get('item_url', source['url'])})
**Published:** {reading.get('item_date', 'unknown')}
**Confidence:** {reading.get('confidence', '?')} ({reading.get('evidence_type', '?')})

## TL;DR
{reading.get('morning_brief', reading.get('summary', 'No summary available.'))}

## Connections to Our Work
{connections_md if connections_md else 'No direct connections found today.'}
{action_md}

## Limitations
{reading.get('limitations', 'Not assessed.')}

---
*Curiosity Engine — reading #{load_state().get('total_readings', 0)}*
"""

    with open(brief_file, "w") as f:
        f.write(brief_content)
    logger.info(f"Morning brief written: {brief_file}")
    return brief_file


def save_reading(source, reading):
    """Save the full reading analysis to disk."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{source['id']}.json"
    filepath = READINGS_DIR / filename
    with open(filepath, "w") as f:
        json.dump({"source": source, "analysis": reading,
                   "timestamp": datetime.now().isoformat()}, f, indent=2)
    logger.info(f"Saved reading: {filepath}")


def run_reading_cycle(specific_url=None):
    """Main reading cycle: pick source, fetch, get brain context, analyze, encode."""
    state = load_state()

    if specific_url:
        source = {"id": "manual", "url": specific_url, "tier": 2,
                  "topic": "manual", "type": "manual",
                  "description": "Manually specified URL", "frequency_days": 0}
    else:
        source = pick_source(state)
        if not source:
            return

    logger.info(f"Reading: {source['description']}")
    logger.info(f"URL: {source['url']}")

    # Fetch content
    content = fetch_content(source["url"])
    if not content:
        logger.error("Failed to fetch content. Skipping.")
        return

    # Get existing brain context for connection-making
    logger.info("Fetching brain context for connections...")
    brain_context = get_existing_brain_context()

    # Build prompt and write it for the child agent
    prompt = build_analysis_prompt(source, content, brain_context)
    prompt_file = SCRIPT_DIR / "logs" / f"curiosity-prompt-{datetime.now().strftime('%Y-%m-%d')}.txt"
    prompt_file.parent.mkdir(exist_ok=True)
    with open(prompt_file, "w") as f:
        f.write(prompt)

    logger.info(f"Analysis prompt written to {prompt_file}")
    logger.info("Launching child agent for critical reading...")

    # The scheduler-owned curiosity program handles child agent execution around
    # this prompt. Output goes to a JSON file that we then encode.
    output_file = SCRIPT_DIR / "logs" / f"curiosity-output-{datetime.now().strftime('%Y-%m-%d')}.json"

    # Update state
    state["last_reads"][source["id"]] = datetime.now().isoformat()
    state["total_readings"] = state.get("total_readings", 0) + 1
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    return {
        "source": source,
        "prompt_file": str(prompt_file),
        "output_file": str(output_file),
        "content_length": len(content),
    }


def show_history():
    """Show recent readings."""
    readings = sorted(READINGS_DIR.glob("*.json"), reverse=True)[:10]
    if not readings:
        print("No readings yet.")
        return
    for r in readings:
        with open(r) as f:
            data = json.load(f)
        analysis = data.get("analysis", {})
        print(f"\n📖 {r.name}")
        print(f"   {analysis.get('item_title', 'Unknown')}")
        print(f"   Confidence: {analysis.get('confidence', '?')} | "
              f"Application: {analysis.get('concrete_application', 'none')[:80]}")
        conns = analysis.get("connections", [])
        if conns:
            print(f"   Connections: {len(conns)} — {conns[0].get('to', '?')[:60]}")


def show_brief():
    """Show today's morning brief."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    brief_file = BRIEFS_DIR / f"{date_str}.md"
    if brief_file.exists():
        print(brief_file.read_text())
    else:
        # Try yesterday
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        brief_file = BRIEFS_DIR / f"{yesterday}.md"
        if brief_file.exists():
            print(f"(No brief for today yet. Showing yesterday's:)\n")
            print(brief_file.read_text())
        else:
            print("No brief available. The curiosity engine runs at 10 PM EST.")


def show_sources():
    """Show source rotation with next-due dates."""
    state = load_state()
    now = datetime.now()
    print("\n📚 Source Rotation:\n")
    for s in SOURCES:
        last = state["last_reads"].get(s["id"])
        if last:
            last_dt = datetime.fromisoformat(last)
            days_since = (now - last_dt).days
            next_due = s["frequency_days"] - days_since
            status = f"✅ read {days_since}d ago, next in {next_due}d" if next_due > 0 else f"⏰ overdue by {-next_due}d"
        else:
            status = "🆕 never read"
        print(f"  [{s['tier']}] {s['id']:<25} {status}")
        print(f"      {s['description']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Curiosity Engine — daily critical reading")
    parser.add_argument("--source", help="Read a specific URL")
    parser.add_argument("--list", action="store_true", help="Show source rotation")
    parser.add_argument("--history", action="store_true", help="Show recent readings")
    parser.add_argument("--brief", action="store_true", help="Show today's morning brief")
    args = parser.parse_args()

    if args.list:
        show_sources()
    elif args.history:
        show_history()
    elif args.brief:
        show_brief()
    else:
        result = run_reading_cycle(specific_url=args.source)
        if result:
            print(json.dumps(result, indent=2))
