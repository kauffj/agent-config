// Direct publication and validated workspace lifecycle teardown.

import { existsSync } from 'node:fs';
import { withWorkspaceLifecycleLock } from './safe-runtime-files.mjs';
import { requireWorkspace, removeRecord, updateRecord } from './workspace-state.mjs';
import {
  actualDefaultBranch,
  assertContained,
  assertWorkspace,
  fetchRemoteBranch,
  git,
  headSha,
  refExists,
  remoteBranchSha,
  repositoryId,
  stdout,
  validateWorkspaceLocation,
} from './workspace-git.mjs';
import { dropWorkspaceDatabase, prepareDatabaseCleanup } from './workspace-database.mjs';

function assertActiveWorkspace(record) {
  if (record.status !== 'active') {
    throw new Error(`workspace '${record.name}' is ${record.status}, not active`);
  }
}

function integrateUnlocked(name) {
  const record = requireWorkspace(name);
  assertActiveWorkspace(record);
  const { branch, worktreePath } = assertWorkspace(record);
  const defaultBranch = actualDefaultBranch();
  if (branch === defaultBranch) throw new Error('workspace branch is the remote default branch');

  const remoteBranchShaBeforeIntegrate = remoteBranchSha(branch) || null;
  fetchRemoteBranch(defaultBranch);
  const baseSha = stdout(git(['rev-parse', `refs/remotes/origin/${defaultBranch}`]));
  const rebased = git(['rebase', `origin/${defaultBranch}`], {
    cwd: worktreePath,
    allowFailure: true,
  });
  if (rebased.status !== 0) {
    const detail = (rebased.stderr || rebased.stdout).trim();
    throw new Error(`integration rebase failed; resolve or abort the rebase before retrying${detail ? `: ${detail}` : ''}`);
  }
  assertWorkspace(record);
  const integratedSha = headSha(worktreePath);
  updateRecord(name, {
    delivery: {
      defaultBranch,
      baseSha,
      remoteBranchShaBeforeIntegrate,
      integratedSha,
      deliveryVerified: false,
      deliveryEvidence: null,
    },
  });
  return {
    name,
    branch,
    worktreePath,
    defaultBranch,
    baseSha,
    remoteBranchShaBeforeIntegrate,
    integratedSha,
  };
}

function publishUnlocked(name, expectedSha) {
  const record = requireWorkspace(name);
  assertActiveWorkspace(record);
  const { branch, worktreePath } = assertWorkspace(record);
  const defaultBranch = actualDefaultBranch();
  if (branch === defaultBranch) throw new Error('workspace branch is the remote default branch');
  if (record.delivery?.defaultBranch && record.delivery.defaultBranch !== defaultBranch) {
    throw new Error(`origin default changed from '${record.delivery.defaultBranch}' to '${defaultBranch}'`);
  }

  const head = headSha(worktreePath);
  if (!expectedSha || expectedSha !== head || record.delivery?.integratedSha !== head) {
    throw new Error('workspace HEAD does not match the verified integrated SHA; integrate and verify again');
  }
  fetchRemoteBranch(defaultBranch);
  const fetchedDefaultSha = stdout(git(['rev-parse', `refs/remotes/origin/${defaultBranch}`]));
  if (record.delivery?.baseSha !== fetchedDefaultSha) {
    throw new Error(`origin/${defaultBranch} advanced after integration; integrate and verify again`);
  }
  const containsDefault = git(
    ['merge-base', '--is-ancestor', `origin/${defaultBranch}`, head],
    { cwd: worktreePath, allowFailure: true },
  );
  if (containsDefault.status !== 0) {
    throw new Error(`origin/${defaultBranch} advanced after integration; integrate and verify again`);
  }

  git([
    'push', `--force-with-lease=refs/heads/${defaultBranch}:${fetchedDefaultSha}`,
    'origin', `${head}:refs/heads/${defaultBranch}`,
  ], { cwd: worktreePath });
  const remoteLine = stdout(git(['ls-remote', '--heads', 'origin', `refs/heads/${defaultBranch}`]));
  const remoteSha = remoteLine.split(/\s+/)[0] || '';
  if (remoteSha !== head) throw new Error(`origin/${defaultBranch} did not advance to ${head}`);
  if (headSha(worktreePath) !== head) {
    throw new Error('workspace HEAD changed during publish; the captured reviewed SHA shipped, but integration and review must run again');
  }

  const publishedAt = new Date().toISOString();
  const publishedRepositoryId = repositoryId();
  updateRecord(name, {
    delivery: {
      defaultBranch,
      deployChoice: 'published',
      deploySha: head,
      repositoryId: publishedRepositoryId,
      publishedAt,
      deliveryVerified: false,
      deliveryEvidence: null,
    },
  });
  return {
    name,
    branch,
    worktreePath,
    defaultBranch,
    deploySha: head,
    repositoryId: publishedRepositoryId,
    publishedAt,
  };
}

function validReason(result) {
  return result?.status === 'not-applicable'
    && typeof result.reason === 'string'
    && result.reason.trim().length > 0;
}

function validCommandResult(result) {
  return result?.status === 'passed'
    && /^sha256:[a-f0-9]{64}$/.test(result.command ?? '')
    && result.exitStatus === 0;
}

function validateGateResults(evidence, deploySha) {
  const { ci, deployment } = evidence;
  if (ci?.status === 'passed' && ci.provider === 'github-actions') {
    const accepted = new Set(['success', 'neutral', 'skipped']);
    if (!Array.isArray(ci.runs) || ci.runs.length === 0 || ci.runs.some((run) => (
      (!Number.isInteger(run.id) && typeof run.id !== 'string')
      || run.headSha !== deploySha
      || run.status !== 'completed'
      || !accepted.has(run.conclusion)
    ))) {
      throw new Error('GitHub Actions evidence must contain nonempty successful runs for the deployed SHA');
    }
  } else if (ci?.provider === 'documented-command') {
    if (!validCommandResult(ci)) {
      throw new Error('documented CI evidence must contain a successful hashed command result');
    }
  } else if (!validReason(ci)) {
    throw new Error('CI evidence must be successful or have a nonempty not-applicable reason');
  }

  if (!validCommandResult(deployment) && !validReason(deployment)) {
    throw new Error('deployment evidence must be successful or have a nonempty not-applicable reason');
  }
}

function verifyDeliveryUnlocked(name, evidenceJson) {
  const record = requireWorkspace(name);
  assertActiveWorkspace(record);
  let evidence;
  try {
    evidence = JSON.parse(evidenceJson);
  } catch {
    throw new Error('delivery verification evidence must be valid JSON');
  }
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)
      || typeof evidence.checkedAt !== 'string' || !evidence.ci || !evidence.deployment) {
    throw new Error('delivery evidence must include checkedAt plus independent CI and deployment results');
  }
  const deploySha = record.delivery?.deploySha;
  if (!deploySha) throw new Error(`workspace '${name}' has no deployed SHA`);
  validateGateResults(evidence, deploySha);
  const defaultBranch = actualDefaultBranch();
  if (record.delivery?.defaultBranch !== defaultBranch) {
    throw new Error(`recorded default branch does not match origin/${defaultBranch}`);
  }
  const currentRepositoryId = repositoryId();
  if (record.delivery?.repositoryId !== currentRepositoryId
      || evidence.repositoryId !== currentRepositoryId
      || evidence.defaultBranch !== defaultBranch
      || evidence.deploySha !== deploySha) {
    throw new Error('delivery evidence identity does not match the published repository, branch, and SHA');
  }
  const checkedAt = Date.parse(evidence.checkedAt);
  const publishedAt = Date.parse(record.delivery?.publishedAt ?? '');
  if (!Number.isFinite(checkedAt) || !Number.isFinite(publishedAt)
      || checkedAt < publishedAt || checkedAt > Date.now() + 5 * 60 * 1000) {
    throw new Error('delivery evidence timestamp is invalid or predates publication');
  }
  fetchRemoteBranch(defaultBranch);
  const containsDeployment = git(
    ['merge-base', '--is-ancestor', deploySha, `refs/remotes/origin/${defaultBranch}`],
    { allowFailure: true },
  );
  if (containsDeployment.status !== 0) {
    throw new Error(`deployed SHA ${deploySha} is not contained in origin/${defaultBranch}`);
  }
  const verified = updateRecord(name, {
    delivery: { deliveryVerified: true, deliveryEvidence: evidence },
  });
  return {
    name,
    defaultBranch,
    deploySha,
    repositoryId: currentRepositoryId,
    deliveryVerified: verified.delivery.deliveryVerified,
    deliveryEvidence: evidence,
  };
}

function removeWorkspaceResources(record, preparedDatabase, localSha = '') {
  const databaseDropped = dropWorkspaceDatabase(preparedDatabase);
  const worktreeRemoved = Boolean(record.worktreePath && existsSync(record.worktreePath));
  if (worktreeRemoved) git(['worktree', 'remove', record.worktreePath, '--force']);
  if (localSha) git(['update-ref', '-d', `refs/heads/${record.branch}`, localSha]);
  return { databaseDropped, worktreeRemoved, localBranchRemoved: Boolean(localSha) };
}

function abandonUnlocked(name) {
  const record = requireWorkspace(name);
  const { branch } = validateWorkspaceLocation(record, { clean: false });
  const localRef = `refs/heads/${branch}`;
  const localSha = refExists(localRef) ? stdout(git(['rev-parse', localRef])) : '';
  const result = removeWorkspaceResources(record, prepareDatabaseCleanup(record), localSha);
  updateRecord(name, { status: 'abandoned', worktreePath: null });
  return { name, status: 'abandoned', ...result };
}

function removeUnlocked(name) {
  const record = requireWorkspace(name);
  const { branch } = validateWorkspaceLocation(record, { clean: false });
  const localRef = `refs/heads/${branch}`;
  const localSha = refExists(localRef) ? stdout(git(['rev-parse', localRef])) : '';
  const result = removeWorkspaceResources(record, prepareDatabaseCleanup(record), localSha);
  removeRecord(name);
  return { name, status: 'removed', ...result };
}

function finishUnlocked(name) {
  const record = requireWorkspace(name);
  if (record.delivery?.deliveryVerified !== true) {
    throw new Error(`workspace '${name}' delivery has not been verified`);
  }
  const { branch } = validateWorkspaceLocation(record);
  const defaultBranch = actualDefaultBranch();
  if (branch === defaultBranch) throw new Error('workspace branch is the remote default branch');

  fetchRemoteBranch(defaultBranch);
  const remoteSha = remoteBranchSha(branch);
  if (remoteSha) {
    fetchRemoteBranch(branch);
    const fetchedSha = stdout(git(['rev-parse', `refs/remotes/origin/${branch}`]));
    if (fetchedSha !== remoteSha) throw new Error(`origin/${branch} changed while verifying`);
  }

  const localRef = `refs/heads/${branch}`;
  const localSha = refExists(localRef) ? stdout(git(['rev-parse', localRef])) : '';
  const candidates = [];
  if (localSha) candidates.push(localSha);
  const supersededRemoteBranch = Boolean(
    remoteSha && (record.delivery?.remoteBranchShaBeforeIntegrate
      ?? record.delivery?.remoteFeatureShaBeforeIntegrate) === remoteSha,
  );
  if (remoteSha && !supersededRemoteBranch) candidates.push(remoteSha);
  if (record.delivery?.deploySha) candidates.push(record.delivery.deploySha);
  if (!candidates.length) throw new Error('no branch ref or deployed SHA can prove shipment');
  assertContained(candidates, defaultBranch);

  const preparedDatabase = prepareDatabaseCleanup(record);
  if (remoteSha) {
    git([
      'push', '--atomic',
      `--force-with-lease=refs/heads/${branch}:${remoteSha}`,
      'origin',
      `:refs/heads/${branch}`,
    ]);
  }

  fetchRemoteBranch(defaultBranch);
  try {
    assertContained(candidates, defaultBranch);
  } catch (error) {
    if (remoteSha && !remoteBranchSha(branch)) {
      git([
        'push', `--force-with-lease=refs/heads/${branch}:`,
        'origin', `${remoteSha}:refs/heads/${branch}`,
      ]);
    }
    throw error;
  }

  if (remoteSha) {
    const remoteTrackingRef = `refs/remotes/origin/${branch}`;
    if (refExists(remoteTrackingRef)) git(['update-ref', '-d', remoteTrackingRef, remoteSha]);
  }
  const cleanup = removeWorkspaceResources(record, preparedDatabase, localSha);
  git(['config', '--remove-section', `branch.${branch}`], { allowFailure: true });
  updateRecord(name, { status: 'done', worktreePath: null });
  return {
    name,
    status: 'done',
    defaultBranch,
    deploySha: record.delivery?.deploySha ?? null,
    ...cleanup,
    remoteBranchRemoved: Boolean(remoteSha),
  };
}

export function integrate(name) {
  return withWorkspaceLifecycleLock(name, () => integrateUnlocked(name));
}

export function publish(name, expectedSha) {
  return withWorkspaceLifecycleLock(name, () => publishUnlocked(name, expectedSha));
}

export function verifyDelivery(name, evidenceJson) {
  return withWorkspaceLifecycleLock(name, () => verifyDeliveryUnlocked(name, evidenceJson));
}

export function abandon(name) {
  return withWorkspaceLifecycleLock(name, () => abandonUnlocked(name));
}

export function remove(name) {
  return withWorkspaceLifecycleLock(name, () => removeUnlocked(name));
}

export function finish(name) {
  return withWorkspaceLifecycleLock(name, () => finishUnlocked(name));
}
