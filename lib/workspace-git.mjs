// Process, Git, and worktree invariants shared by workspace lifecycle modules.

import { existsSync, realpathSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { isAbsolute, resolve } from 'node:path';
import { createHash } from 'node:crypto';

export function run(command, args, {
  cwd = process.cwd(),
  allowFailure = false,
  redactArgs = false,
  env = process.env,
} = {}) {
  const result = spawnSync(command, args, { cwd, env, encoding: 'utf8' });
  if (result.error) throw result.error;
  if (!allowFailure && result.status !== 0) {
    const detail = (result.stderr || result.stdout || '').trim();
    const invocation = redactArgs ? command : `${command} ${args.join(' ')}`;
    throw new Error(`${invocation} failed${detail ? `: ${detail}` : ''}`);
  }
  return result;
}

export function git(args, options) {
  return run('git', args, options);
}

export function stdout(result) {
  return result.stdout.trim();
}

function validateBranch(branch, label) {
  if (!branch || branch === 'null') throw new Error(`${label} is missing`);
  const check = git(['check-ref-format', '--branch', branch], { allowFailure: true });
  if (check.status !== 0) throw new Error(`${label} '${branch}' is invalid`);
  return branch;
}

function currentBranch(worktreePath) {
  return stdout(git(['branch', '--show-current'], { cwd: worktreePath }));
}

function worktreesForBranch(branch) {
  const ref = `refs/heads/${branch}`;
  const paths = [];
  let path = null;
  for (const line of stdout(git(['worktree', 'list', '--porcelain'])).split('\n')) {
    if (line.startsWith('worktree ')) path = line.slice('worktree '.length);
    else if (line === `branch ${ref}` && path) paths.push(path);
  }
  return paths;
}

export function actualDefaultBranch() {
  const lines = stdout(git(['ls-remote', '--symref', 'origin', 'HEAD']));
  const match = lines.match(/^ref:\s+refs\/heads\/([^\t\n]+)\s+HEAD$/m);
  if (!match) throw new Error('origin did not declare a default branch');
  return validateBranch(match[1], 'origin default branch');
}

export function fetchRemoteBranch(branch) {
  git(['fetch', 'origin', `+refs/heads/${branch}:refs/remotes/origin/${branch}`]);
}

export function assertWorkspace(record, { clean = true } = {}) {
  const branch = validateBranch(record.branch, 'workspace branch');
  const worktreePath = record.worktreePath;
  if (!worktreePath || !isAbsolute(worktreePath) || !existsSync(worktreePath)) {
    throw new Error(`workspace '${record.name}' has no existing absolute worktree`);
  }
  if (realpathSync(worktreePath) !== worktreePath) {
    throw new Error(`workspace '${record.name}' worktree path is not canonical`);
  }
  const owners = worktreesForBranch(branch);
  if (owners.length !== 1 || owners[0] !== worktreePath) {
    throw new Error(`workspace '${record.name}' branch is checked out in an unexpected worktree`);
  }
  const mainCommonDir = realpathSync(resolve(stdout(git(['rev-parse', '--git-common-dir']))));
  const worktreeCommonDir = realpathSync(resolve(
    worktreePath,
    stdout(git(['rev-parse', '--git-common-dir'], { cwd: worktreePath })),
  ));
  if (mainCommonDir !== worktreeCommonDir) {
    throw new Error(`workspace '${record.name}' belongs to a different repository`);
  }
  const checkedOut = currentBranch(worktreePath);
  if (checkedOut !== branch) {
    throw new Error(`workspace '${record.name}' is on '${checkedOut || 'detached HEAD'}', not '${branch}'`);
  }
  if (clean && stdout(git(['status', '--porcelain', '--untracked-files=all'], { cwd: worktreePath }))) {
    throw new Error(`workspace '${record.name}' has uncommitted or untracked files`);
  }
  return { branch, worktreePath };
}

export function validateWorkspaceLocation(record, { clean = true } = {}) {
  const branch = validateBranch(record.branch, 'workspace branch');
  if (record.worktreePath && existsSync(record.worktreePath)) {
    return { branch, ...assertWorkspace(record, { clean }), worktreeExists: true };
  }
  const owners = worktreesForBranch(branch);
  if (owners.length) {
    throw new Error(`workspace '${record.name}' branch is checked out at '${owners[0]}', not its recorded worktree`);
  }
  return { branch, worktreePath: record.worktreePath, worktreeExists: false };
}

export function headSha(worktreePath) {
  return stdout(git(['rev-parse', 'HEAD'], { cwd: worktreePath }));
}

export function remoteBranchSha(branch) {
  const line = stdout(git(['ls-remote', '--heads', 'origin', `refs/heads/${branch}`]));
  return line ? line.split(/\s+/)[0] : '';
}

export function refExists(ref) {
  return git(['show-ref', '--verify', '--quiet', ref], { allowFailure: true }).status === 0;
}

export function assertContained(candidates, defaultBranch) {
  for (const candidate of candidates) {
    const contained = git(
      ['merge-base', '--is-ancestor', candidate, `refs/remotes/origin/${defaultBranch}`],
      { allowFailure: true },
    );
    if (contained.status !== 0) {
      throw new Error(`${candidate} is not contained in origin/${defaultBranch}; workspace left intact`);
    }
  }
}

export function repositoryId() {
  return `sha256:${createHash('sha256')
    .update(stdout(git(['remote', 'get-url', 'origin'])))
    .digest('hex')}`;
}
