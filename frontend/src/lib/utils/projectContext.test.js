import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildProjectContextAttachPayload,
  buildProjectContextMessageAttachment,
  countProjectContextResources,
  extractIdeaProjectContext,
  inferProjectContextResource,
  normalizeProjectContextResource,
  projectContextStatusCopy,
  validateProjectContextResources,
} from './projectContext.ts';

test('summarizes validation status with worker-facing copy', () => {
    assert.equal(projectContextStatusCopy(null), 'No project context attached');
    assert.equal(projectContextStatusCopy({ validation_status: 'client_validated' }), 'Ready for workers');
    assert.equal(projectContextStatusCopy({ validation_status: 'client_invalid' }), 'Needs attention');
    assert.equal(projectContextStatusCopy({ status: 'snapshot_created' }), 'Attached');
});

test('counts either UI resources or backend snapshot targets', () => {
    assert.equal(countProjectContextResources({ resources: [{ path: 'frontend' }, { path: 'docs' }] }), 2);
    assert.equal(countProjectContextResources({ targets: [{ path: 'brain' }] }), 1);
});

test('builds reusable project context payloads', () => {
    const inlineContext = { selected_profile_name: 'Cortex UI', resources: [{ path: 'frontend' }] };
    assert.deepEqual(buildProjectContextMessageAttachment(inlineContext), {
        type: 'project_context',
        name: 'Cortex UI',
        project_context: inlineContext,
    });
    assert.deepEqual(buildProjectContextAttachPayload(inlineContext), { project_context: inlineContext });
    assert.deepEqual(
        buildProjectContextAttachPayload({ project_profile_id: 'profile-1', selected_profile_name: 'Saved' }),
        { project_profile_id: 'profile-1' },
    );
});

test('extracts project context from current idea shapes', () => {
    const context = { selected_profile_name: 'Cortex UI', resources: [{ path: 'frontend' }] };

    assert.equal(extractIdeaProjectContext({ project_context: context }), context);
    assert.equal(extractIdeaProjectContext({ agent_details: { project_context: context } }), context);
    assert.equal(extractIdeaProjectContext({ metadata: { project_context_snapshot: context } }), context);
    assert.equal(extractIdeaProjectContext(null), null);
});

test('infers common resource kinds from user-entered lines', () => {
    assert.equal(inferProjectContextResource('example-org/example-repo').type, 'folder');
    assert.equal(inferProjectContextResource('docs/project-context.md').type, 'doc');
    assert.equal(inferProjectContextResource('frontend/src/lib/features/cortex').type, 'folder');
});

test('normalizes connector resources before project save or run', () => {
    assert.deepEqual(normalizeProjectContextResource({
        type: 'repo',
        repo: 'example-org/example-backend',
        uri: 'https://github.com/example-org/example-backend',
    }), {
        id: 'resource-1',
        type: 'repo',
        kind: 'repo',
        label: 'example-org/example-backend',
        name: 'example-org/example-backend',
        repo: 'example-org/example-backend',
        uri: 'https://github.com/example-org/example-backend',
    });
});

test('allows empty projects and flags invalid resource entries before run', () => {
    assert.equal(validateProjectContextResources([]).valid, true);
    assert.equal(validateProjectContextResources([{ path: 'frontend' }, { path: 'frontend' }]).valid, false);
    assert.equal(validateProjectContextResources([{ path: 'frontend' }, { repo: 'example-org/example-repo' }]).valid, true);
    assert.equal(validateProjectContextResources([{ uri: 'browser-file://spec.md', name: 'spec.md' }]).valid, false);
    assert.equal(validateProjectContextResources([{
        uri: '/static/uploads/project-context/abc/spec.md',
        path: '/tmp/illo/spec.md',
        name: 'spec.md',
        uploaded_files: [{ storage_path: '/tmp/illo/spec.md' }],
    }]).valid, true);
});
