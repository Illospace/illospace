import test from 'node:test';
import assert from 'node:assert/strict';

import { buildRunEvidenceDebug } from './runEvidenceDebug.ts';

test('summarizes worker evidence, step tools, and semantic verifier gaps', () => {
  const debug = buildRunEvidenceDebug({
    contract_type: 'freeform',
    contract_requirements: { evidence: { require_worker_evidence: true, strict_json: true } },
    run_steps: [{ node_id: 'skill-catalog-audit', skill_name: 'debug' }],
    tool_calls: [
      { tool: 'brain_skills/skill_view', node_id: 'skill-catalog-audit', args: '{"name":"catalog"}' },
    ],
    execution_artifacts: [
      {
        type: 'worker_result',
        node_id: 'skill-catalog-audit',
        trust_status: 'untrusted',
        evidence: {
          schema: { valid: false },
          files: [],
          commands: [],
          artifacts: [],
          unresolved_uncertainty: ['skill_view output was not recorded as JSON'],
        },
      },
    ],
    verification_runs: [
      {
        verifier_type: 'semantic_evidence_judge',
        status: 'failed',
        evidence: {
          missing_evidence: ['No strict JSON worker evidence was recorded.'],
          unsupported_claims: ['Final response says the skill catalog audit found 14 skills.'],
        },
      },
    ],
  });

  assert.equal(debug?.tone, 'failed');
  assert.equal(debug?.defaultOpen, true);
  assert.equal(debug?.summaryLabel, '1 worker / 1 uncertain');
  assert.deepEqual(debug?.contractRequirements, ['evidence: require_worker_evidence, strict_json']);
  assert.deepEqual(debug?.missingEvidence, ['No strict JSON worker evidence was recorded.']);
  assert.deepEqual(debug?.unsupportedClaims, ['Final response says the skill catalog audit found 14 skills.']);
  assert.equal(debug?.steps[0].stepId, 'skill-catalog-audit');
  assert.deepEqual(debug?.steps[0].tools, ['brain_skills/skill_view']);
  assert.equal(debug?.steps[0].evidenceLabel, '1 worker / 1 uncertain');
});

test('keeps existing PR review contract requirements compact', () => {
  const debug = buildRunEvidenceDebug({
    contract_type: 'pr_review',
    contract_requirements: {
      minimum_reviewed_prs: 2,
      pull_requests: [{ number: 1249 }, { number: 1250 }],
    },
    execution_artifacts: [
      { type: 'existing_pr_under_review', number: 1249 },
      { type: 'existing_pr_under_review', number: 1250 },
    ],
    verification_runs: [{ verifier_type: 'semantic_evidence_judge', status: 'passed' }],
  });

  assert.equal(debug?.tone, 'passed');
  assert.equal(debug?.defaultOpen, false);
  assert.equal(debug?.summaryLabel, '2 artifacts');
  assert.deepEqual(debug?.contractRequirements, ['minimum_reviewed_prs: 2', '2 PR refs']);
});

test('treats completed evidence gaps as warning tone', () => {
  const debug = buildRunEvidenceDebug({
    contract_type: 'read_only_audit',
    contract_status: 'needs_evidence',
    verification_warnings: [
      {
        completion_contract_status: 'needs_evidence',
        missing_conditions: ['Source-code evidence was not recorded.'],
      },
    ],
  });

  assert.equal(debug?.tone, 'warning');
  assert.equal(debug?.defaultOpen, true);
  assert.deepEqual(debug?.missingEvidence, ['Source-code evidence was not recorded.']);
  assert.equal(debug?.verifierLabel, 'contract: needs_evidence');
});
