import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const CORE_SKILLS = [
  'feature',
  'workspace',
  'propose',
  'review-pr',
  'review-security',
  'review-simplicity',
  'review-ui',
  'explore',
].map((name) => `skills/${name}/SKILL.md`);
const REVIEWER_ROLES = [
  'agents/security-reviewer.md',
  'agents/simplicity-reviewer.md',
  'agents/ui-reviewer.md',
  'agents/visual-reviewer.md',
  'agents/qa-tester.md',
];

const FORBIDDEN = [
  [/(?:\$HOME|~)\/\.claude\//, 'Claude home used as a shared source path'],
  [/\bAskUserQuestion\b/, 'Claude-only user-question tool name'],
  [/\bsubagent_type\b/, 'Claude-only subagent selector'],
  [/\bInvoke\s+\/?(?:feature|workspace|propose|review-pr|review-security|review-simplicity|review-ui)\b/i, 'harness-specific cross-skill invocation'],
  [/\$ARGUMENTS\b/, 'Claude-only invocation placeholder'],
];

for (const relative of [...CORE_SKILLS, ...REVIEWER_ROLES]) {
  test(`${relative} uses the portable workflow vocabulary`, () => {
    const body = readFileSync(join(ROOT, relative), 'utf8');
    for (const [pattern, label] of FORBIDDEN) {
      assert.doesNotMatch(body, pattern, `${label} in ${relative}`);
    }
  });
}

test('explore delegates semantically instead of using Claude fork frontmatter', () => {
  const body = readFileSync(join(ROOT, 'skills/explore/SKILL.md'), 'utf8');
  const frontmatter = body.split('---', 3)[1];
  assert.doesNotMatch(frontmatter, /^context:/m);
  assert.doesNotMatch(frontmatter, /^agent:/m);
  assert.match(body, /exploration subagent/);
});

test('core skill frontmatter contains no harness-only invocation metadata', () => {
  for (const relative of CORE_SKILLS) {
    const body = readFileSync(join(ROOT, relative), 'utf8');
    const frontmatter = body.split('---', 3)[1];
    assert.doesNotMatch(frontmatter, /^argument-hint:/m, relative);
    assert.doesNotMatch(frontmatter, /^context:/m, relative);
    assert.doesNotMatch(frontmatter, /^agent:/m, relative);
  }
});

test('core assets resolve shared helpers and principles through the neutral install root', () => {
  for (const relative of [
    'skills/feature/SKILL.md',
    'skills/workspace/SKILL.md',
    'skills/propose/SKILL.md',
    'skills/review-pr/SKILL.md',
    'agents/simplicity-reviewer.md',
    'agents/ui-reviewer.md',
    'agents/visual-reviewer.md',
  ]) {
    const body = readFileSync(join(ROOT, relative), 'utf8');
    assert.match(body, /\$HOME\/\.config\/agent-config\//, relative);
  }
});

test('focused reviewers delegate target gathering and classification to review-pr', () => {
  for (const reviewer of ['security', 'simplicity', 'ui']) {
    const body = readFileSync(join(ROOT, `skills/review-${reviewer}/SKILL.md`), 'utf8');
    assert.match(body, /Apply the `review-pr` skill/, reviewer);
    assert.match(body, new RegExp(`--reviewers ${reviewer}`), reviewer);
    assert.doesNotMatch(body, /git (fetch|merge-base|symbolic-ref)/, reviewer);
  }
});

test('workflow entry skills share one runtime bootstrap operation', () => {
  for (const skill of ['feature', 'workspace', 'propose']) {
    const body = readFileSync(join(ROOT, `skills/${skill}/SKILL.md`), 'utf8');
    assert.match(body, /workspace\.mjs" bootstrap/, skill);
    assert.doesNotMatch(body, /EXCLUDE=|grep -qxF '\.workspaces\/'/, skill);
  }
});
