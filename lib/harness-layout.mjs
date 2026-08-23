import { join, resolve } from 'node:path';

// Shared discovery topology. Paths are relative to HOME; targets are relative
// to this repository. Consumers decide whether to mutate or merely diagnose.
export const HARNESS_LINK_LAYOUT = [
  {
    id: 'source',
    homePath: '.config/agent-config',
    repoPath: '.',
  },
  {
    id: 'claude',
    homePath: '.claude',
    repoPath: '.',
  },
  {
    id: 'codex-instructions',
    homePath: '.codex/AGENTS.md',
    repoPath: 'instructions/AGENTS.md',
    legacyLinkTarget: '../.claude/CLAUDE.md',
  },
  {
    id: 'shared-skills',
    homePath: '.agents/skills',
    repoPath: 'skills',
    compatibleLinkTargets: ['../.claude/skills'],
  },
];

export function resolveHarnessLinks(repo, home) {
  return HARNESS_LINK_LAYOUT.map((entry) => ({
    ...entry,
    linkPath: join(home, entry.homePath),
    targetPath: resolve(repo, entry.repoPath),
  }));
}

export function classifyHarnessLinkTarget(spec, rawTarget, resolvedTarget) {
  if (rawTarget === spec.targetPath) return 'correct';
  if (spec.compatibleLinkTargets?.includes(rawTarget) && resolvedTarget === spec.targetPath) {
    return 'correct';
  }
  if (rawTarget === spec.legacyLinkTarget) return 'legacy';
  return 'conflict';
}
