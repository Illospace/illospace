# Memory recall and the knowledge index

Memory recall remains a supported path. Removing it requires evidence that
knowledge search can answer the same questions at least as well. A cue extraction
fix does not establish that result. This preserves the measurement gate in
[#723](https://github.com/Illospace/illospace/issues/723).

The two paths have different contracts. Reconstructive memory returns source
spans and assertions through its controller. The knowledge index mirrors eligible
shared content nodes and combines them with other sources. Index eligibility has
one owner in `brain/systems/knowledge/memory_eligibility.py`; a private,
non-content, archived, or superseded node cannot be assumed to have a live mirror.
A shared-index ranking score alone cannot justify removing access or evidence
contracts that its corpus does not cover.

## Measured decision on 2026-10-07

A read-only live harvest returned 94 candidates: 25 GitHub, 25 Slack, 25 memory,
and 19 unlabelled run inputs. Sixteen non-sensitive questions were curated from
verified canonical records: eight GitHub, four Slack, and four distilled memory
notes, with 18 acceptable evidence references. Raw source text and run inputs
were not used as public fixtures. The questions state observed symptoms without
internal symbols, paths, or distinctive answer wording.

Both existing engines scored the same question set at depth 50, with cutoffs 3
and 10. Semantic retrieval was available in every case. The knowledge corpus had
22,521 items: 8,002 domain records, 5,424 GitHub items, 1,654 memory items, 55
skills, and 7,386 Slack items. Direct memory recall had 1,654 memory items.

| Engine | Recall at 3 | Recall at 10 | Mean reciprocal rank at 50 | Misses at 50 |
| --- | ---: | ---: | ---: | ---: |
| Knowledge | 0.7500 | 0.8125 | 0.62462911 | 0 |
| Memory | 0.1250 | 0.1250 | 0.13090861 | 12 |

All 12 direct-memory misses were GitHub or Slack questions, outside that engine's
corpus. Those aggregate scores measure source coverage as well as ranking. The
four memory-backed questions give the focused comparison:

| Observed question | Knowledge evidence rank | Memory evidence rank |
| --- | ---: | ---: |
| Disabled job can return after restart | 3 | 1 |
| Scheduled follow-up cannot send its alert | 28 | 17 |
| Feature appears absent from branch history | 3 | 28 |
| Reported approval does not prove delivery | 1 | 1 |

Memory ranked the accepted evidence earlier in two cases, later in one, and the
same in one. The sample does not establish replacement parity. It also does not
test doc or domain ground truth, owner-private memory, or the broader recall
distribution. Retain direct memory recall as a supported path; accept the
duplicate ranking path rather than remove a contract on this limited evidence.
Any future removal must meet the replacement gate below. This decision resolves
the current deletion proposal without claiming equal recall or a full benchmark.

The measured question-set ID is `illospace-mixed-recall-20261007`, version `1`.
Memory ground truth is `memory_node:6034`, `memory_node:6040`,
`memory_node:6008`, `memory_node:6011`, `memory_node:5974`, and `memory_node:5957`.
Keep the complete question set and both retrieved-content artifacts in the
operator's private report home.

## Repairing existing cue graphs

The extractor has one owner in `brain/systems/reconstructive_memory/cues.py`.
Ingestion uses it on every call, including a reused first-sentence content node.
Re-ingesting the exact original text through `ingest_memory_source`, with its
original organization, owner, visibility, and scope, adds the revised cue nodes
and upserts their content edges. It appends a source, spans, and an assertion;
it preserves the content node identity, original text, and historical cues.
Verify the same returned content-node ID and the required new subject cues.
Do not use a new backfill owner or change access to repair one historical node.

## Evidence required for a replacement

Use a frozen, source-backed question set with a meaningful share of Slack,
distilled lessons, and notes. Questions describe the observed symptom before the
cause is known. They must not contain internal symbols, file paths, subsystem
names, or distinctive wording copied from the answer. A harvested title is only
a provisional candidate, not an approved question or proof of recall.

Score both engines with the same questions, organization, timestamp, depth, and
cutoffs. Report memory-backed questions separately from sources that direct
memory recall cannot search. Preserve each engine's lexical and semantic channel
scores and corpus fingerprint. An unavailable semantic channel, missing source,
or changed corpus is a measurement limit, not evidence of replacement parity.

The existing `knowledge_recall compare` command compares changes within one
engine and rejects different engines. For a replacement decision, inspect the
two evaluation artifacts' per-case ranks and source coverage together; do not
change the comparison guard or claim its `not-comparable` result is a regression.

## Operator measurement contract

Run the existing CLI inside the approved runtime. Use a private artifact
directory: candidate files can include source titles and old agent input, and
evaluation files can include retrieved text. Keep raw artifacts there; publish
only reviewed, sanitized questions, source handles, aggregate scores, channel
availability, and the final decision. These commands do not migrate data or
replace a recall path.

```bash
python3 -m brain.app.cli.knowledge_recall harvest \
  --org-id "$recall_org_id" --limit-per-source 25 \
  --generated-at "$recall_timestamp" --output "$recall_artifacts/candidates.json"

# Curate a versioned mixed question set from verified candidate sources first.
python3 -m brain.app.cli.knowledge_recall eval \
  --org-id "$recall_org_id" --question-set "$recall_artifacts/questions.json" \
  --engine knowledge --k 3 --k 10 --search-limit 50 \
  --generated-at "$recall_timestamp" --output "$recall_artifacts/knowledge.json"

python3 -m brain.app.cli.knowledge_recall eval \
  --org-id "$recall_org_id" --question-set "$recall_artifacts/questions.json" \
  --engine memory --k 3 --k 10 --search-limit 50 \
  --generated-at "$recall_timestamp" --output "$recall_artifacts/memory.json"
```

The question-set schema is the existing seed schema: `question_set_id`, `version`,
`description`, and `cases`, with `case_id`, `question`, and non-empty
`acceptable_evidence` (`source`, `source_ref`) per case. Inspect every canonical
answer and review query leakage before scoring. Read-only database sessions and
stable source timestamps avoid changing the corpus during measurement.

If the evidence cannot justify removal, record the observed limitation and retain
memory recall. A decision to accept the duplicate path must state why a stronger
corpus or replacement costs more than it is worth. Keep that decision separate
from any claim that the engines have equal measured recall.
