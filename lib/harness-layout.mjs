import { join, resolve } from 'node:path';

// Shared discovery topology. Paths are relative to HOME; targets are relative
// to this repository. Consumers decide whether to mutate or merely diagnose.
export const HARNESS_LINK_LAYOUT = [
  {
    id: 'source',
    homePath: '.config/agent-config',
    repoPath: '.',
    targetType: 'directory',
  },
  {
    id: 'claude',
    homePath: '.claude',
    repoPath: '.',
    targetType: 'directory',
  },
  {
    id: 'codex-instructions',
    homePath: '.codex/AGENTS.md',
    repoPath: 'instructions/AGENTS.md',
    targetType: 'file',
    legacyLinkTarget: '../.claude/CLAUDE.md',
  },
  {
    id: 'codex-hooks',
    homePath: '.codex/hooks.json',
    repoPath: 'codex/hooks.json',
    targetType: 'file',
    compatibleLinkTargets: ['../.claude/codex/hooks.json'],
  },
  {
    id: 'grok-instructions',
    homePath: '.grok/AGENTS.md',
    repoPath: 'instructions/AGENTS.md',
    targetType: 'file',
  },
  {
    id: 'shared-skills',
    homePath: '.agents/skills',
    repoPath: 'skills',
    targetType: 'directory',
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
  if (rawTarget === spec.legacyLinkTarget) return 'legacy';
  if (resolvedTarget !== spec.targetPath) return 'conflict';
  if (rawTarget === spec.targetPath || spec.compatibleLinkTargets?.includes(rawTarget)) {
    return 'correct';
  }
  return 'conflict';
}
