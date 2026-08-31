import assert from 'node:assert/strict';
import {
  appendFileSync,
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';
import { run as workspaceRun } from './workspace-git.mjs';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const WORKSPACE = join(REPO, 'lib', 'workspace.mjs');
const PROJECT = join(REPO, 'lib', 'project.mjs');

function run(command, args, cwd, { allowFailure = false, env = process.env } = {}) {
  const result = spawnSync(command, args, { cwd, env, encoding: 'utf8' });
  if (result.error) throw result.error;
  if (!allowFailure && result.status !== 0) {
    assert.fail(`${command} ${args.join(' ')} failed:\n${result.stderr || result.stdout}`);
  }
  return result;
}

test('workspace commands capture output without persistent files', () => {
  const root = mkdtempSync(join(tmpdir(), 'workspace-capture-'));
  try {
    const before = readdirSync(root);
    const result = workspaceRun(
      process.execPath,
      ['-e', 'process.stdout.write("out"); process.stderr.write("err")'],
      { cwd: root },
    );
    assert.equal(result.status, 0);
    assert.equal(result.stdout, 'out');
    assert.equal(result.stderr, 'err');
    assert.deepEqual(readdirSync(root), before);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('workspace command capture enforces its output limit', () => {
  const root = mkdtempSync(join(tmpdir(), 'workspace-capture-limit-'));
  try {
    const result = workspaceRun(
      process.execPath,
      ['-e', 'process.stdout.write("x".repeat(2 * 1024 * 1024))'],
      { cwd: root, allowFailure: true },
    );
    assert.equal(result.status, 125);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /stdout exceeded/);
    assert.deepEqual(readdirSync(root), []);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

function git(cwd, ...args) {
  return run('git', args, cwd).stdout.trim();
}

function workspace(cwd, ...args) {
  return run(process.execPath, [WORKSPACE, ...args], cwd, { allowFailure: true });
}

function workspaceWithEnv(cwd, env, ...args) {
  return run(process.execPath, [WORKSPACE, ...args], cwd, { allowFailure: true, env });
}

function workspaceAsync(cwd, ...args) {
  return workspaceAsyncWithEnv(cwd, process.env, ...args);
}

function workspaceAsyncWithEnv(cwd, env, ...args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [WORKSPACE, ...args], { cwd, env, encoding: 'utf8' });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.once('error', reject);
    child.once('close', (status) => resolve({ status, stdout, stderr }));
  });
}

function record(main) {
  return JSON.parse(readFileSync(join(main, '.workspaces', 'workspaces.json'), 'utf8')).workspaces[0];
}

function actionsEvidence(published, overrides = {}) {
  return JSON.stringify({
    checkedAt: new Date().toISOString(),
    repositoryId: published.repositoryId,
    defaultBranch: published.defaultBranch,
    deploySha: published.deploySha,
    ci: {
      status: 'passed',
      provider: 'github-actions',
      runs: [{ id: 123, headSha: published.deploySha, status: 'completed', conclusion: 'success' }],
    },
    deployment: { status: 'not-applicable', reason: 'fixture has no deployment' },
    ...overrides,
  });
}

function localDbEndpoint({ port = '5432', transport = 'tcp', host = 'localhost' } = {}) {
  return { transport, host, port, user: 'user', database: 'app' };
}

function advanceRemote(root, origin, filename, contents) {
  const checkout = join(root, `remote-writer-${filename.replace(/[^a-z0-9]/gi, '-')}`);
  run('git', ['clone', origin, checkout], root);
  git(checkout, 'config', 'user.name', 'Test User');
  git(checkout, 'config', 'user.email', 'test@example.com');
  writeFileSync(join(checkout, filename), contents);
  git(checkout, 'add', filename);
  git(checkout, 'commit', '-m', `advance ${filename}`);
  git(checkout, 'push');
  return git(checkout, 'rev-parse', 'HEAD');
}

function deliver(main) {
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const publishResult = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(publishResult.status, 0, publishResult.stderr);
  const published = JSON.parse(publishResult.stdout);
  const verified = workspace(main, 'verify-delivery', 'test', actionsEvidence(published));
  assert.equal(verified.status, 0, verified.stderr);
  return integration;
}

function fakePsql(root) {
  const bin = join(root, 'fake-bin');
  const marker = join(root, 'psql-called');
  mkdirSync(bin);
  const executable = join(bin, 'psql');
  writeFileSync(executable, '#!/bin/sh\nprintf \'%s\\nPGHOSTADDR=%s\\n\' "$*" "${PGHOSTADDR-unset}" > "$PSQL_MARKER"\n');
  chmodSync(executable, 0o755);
  return {
    marker,
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      PSQL_MARKER: marker,
      PGHOSTADDR: 'db.example.com',
    },
  };
}

function fixture(t, { remoteFeature = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'workspace-delivery-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const fixtureRoot = join(root, 'fixture with spaces');
  mkdirSync(fixtureRoot);
  const origin = join(fixtureRoot, 'origin.git');
  const main = join(fixtureRoot, 'main');
  const worktreePath = join(main, '.workspaces', 'worktrees', 'test');
  run('git', ['init', '--bare', origin], fixtureRoot);
  run('git', ['clone', origin, main], fixtureRoot);
  git(main, 'config', 'user.name', 'Test User');
  git(main, 'config', 'user.email', 'test@example.com');
  writeFileSync(join(main, 'base.txt'), 'base\n');
  git(main, 'add', 'base.txt');
  git(main, 'commit', '-m', 'base');
  git(main, 'branch', '-M', 'master');
  git(main, 'push', '-u', 'origin', 'master');
  git(origin, 'symbolic-ref', 'HEAD', 'refs/heads/master');

  mkdirSync(join(main, '.workspaces', 'worktrees'), { recursive: true });
  appendFileSync(join(main, '.git', 'info', 'exclude'), '.workspaces/\n');
  git(main, 'worktree', 'add', '-b', 'feature/test', worktreePath);
  git(worktreePath, 'config', 'user.name', 'Test User');
  git(worktreePath, 'config', 'user.email', 'test@example.com');
  writeFileSync(join(worktreePath, 'base.txt'), 'base\nfeature change\n');
  writeFileSync(join(worktreePath, 'feature.txt'), 'feature\n');
  git(worktreePath, 'add', 'base.txt', 'feature.txt');
  git(worktreePath, 'commit', '-m', 'feature');
  if (remoteFeature) git(worktreePath, 'push', '-u', 'origin', 'feature/test');

  writeFileSync(join(main, 'main.txt'), 'main advance\n');
  git(main, 'add', 'main.txt');
  git(main, 'commit', '-m', 'main advance');
  git(main, 'push');

  writeFileSync(join(main, '.workspaces', 'workspaces.json'), JSON.stringify({
    workspaces: [{
      name: 'test',
      kind: 'feature',
      description: 'fixture',
      branch: 'feature/test',
      worktreePath,
      status: 'active',
      dbName: null,
      envFile: '',
      pipeline: { skill: 'feature', step: 7 },
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    }],
  }, null, 2));
  return { root, origin, main, worktreePath };
}

test('direct delivery uses the remote default and cleans landed refs', (t) => {
  const { root, origin, main, worktreePath } = fixture(t);

  const integrated = workspace(main, 'integrate', 'test');
  assert.equal(integrated.status, 0, integrated.stderr);
  const integration = JSON.parse(integrated.stdout);
  assert.equal(integration.defaultBranch, 'master');
  assert.equal(integration.baseSha, git(origin, 'rev-parse', 'refs/heads/master'));
  assert.equal(record(main).delivery.baseSha, integration.baseSha);
  assert.equal(git(worktreePath, 'merge-base', '--is-ancestor', 'origin/master', 'HEAD'), '');
  const [tip, parent, extraParent] = git(worktreePath, 'rev-list', '--parents', '-n', '1', 'HEAD').split(' ');
  assert.equal(tip, integration.integratedSha);
  assert.equal(parent, integration.baseSha);
  assert.equal(extraParent, undefined);

  const published = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(published.status, 0, published.stderr);
  const deployment = JSON.parse(published.stdout);
  assert.equal(git(origin, 'rev-parse', 'refs/heads/master'), deployment.deploySha);
  assert.equal(record(main).delivery.deliveryVerified, false);

  advanceRemote(root, origin, 'concurrent.txt', 'later default work\n');
  assert.notEqual(git(origin, 'rev-parse', 'refs/heads/master'), deployment.deploySha);

  const verified = workspace(main, 'verify-delivery', 'test', actionsEvidence(deployment));
  assert.equal(verified.status, 0, verified.stderr);
  assert.equal(record(main).delivery.deliveryVerified, true);
  assert.equal(record(main).pipeline.step, 7);

  const finished = workspace(main, 'finish', 'test');
  assert.equal(finished.status, 0, finished.stderr);
  assert.equal(existsSync(worktreePath), false);
  assert.equal(record(main).status, 'done');
  assert.equal(record(main).worktreePath, null);
  assert.equal(record(main).pipeline.step, 7);
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], main, { allowFailure: true }).status, 1);
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/remotes/origin/feature/test'], main, { allowFailure: true }).status, 1);
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], origin, { allowFailure: true }).status, 1);

  const repeated = workspace(main, 'finish', 'test');
  assert.equal(repeated.status, 0, repeated.stderr);
  assert.equal(record(main).status, 'done');
});

test('finish refuses unverified feature delivery before teardown', (t) => {
  const { main, worktreePath } = fixture(t);
  const result = workspace(main, 'finish', 'test');

  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /delivery has not been verified/);
  assert.equal(existsSync(worktreePath), true);
  assert.equal(record(main).status, 'active');
});

test('workspace state symlinks are refused without touching their target', (t) => {
  const { root, main } = fixture(t);
  const state = join(main, '.workspaces', 'workspaces.json');
  const outside = join(root, 'outside-state');
  writeFileSync(outside, 'keep exactly\n');
  rmSync(state);
  symlinkSync(outside, state);

  const result = workspace(main, 'list');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /regular, non-symlink file/);
  assert.equal(readFileSync(outside, 'utf8'), 'keep exactly\n');
});

test('legacy workspace-state symlinks are refused without moving or reading them', (t) => {
  const { root, main } = fixture(t);
  const outside = join(root, 'outside-legacy-state');
  writeFileSync(outside, '{"secret":"keep"}\n');
  rmSync(join(main, '.workspaces', 'workspaces.json'));
  mkdirSync(join(main, '.claude'));
  symlinkSync(outside, join(main, '.claude', 'workspaces.json'));

  const result = workspace(main, 'migrate');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /regular, non-symlink file/);
  assert.equal(readFileSync(outside, 'utf8'), '{"secret":"keep"}\n');
});

test('atomic state writes ignore a pre-created fixed temporary symlink', (t) => {
  const { root, main } = fixture(t);
  const outside = join(root, 'outside-fixed-temp');
  writeFileSync(outside, 'keep exactly\n');
  symlinkSync(outside, join(main, '.workspaces', 'workspaces.json.tmp'));

  const result = workspace(main, 'update', 'test', JSON.stringify({ description: 'updated safely' }));
  assert.equal(result.status, 0, result.stderr);
  assert.equal(record(main).description, 'updated safely');
  assert.equal(readFileSync(outside, 'utf8'), 'keep exactly\n');
});

test('concurrent updates to different workspace records do not lose changes', async (t) => {
  const { main } = fixture(t);
  const names = Array.from({ length: 12 }, (_, index) => `race-${index}`);
  for (const name of names) {
    const created = workspace(main, 'create', JSON.stringify({ name, status: 'active' }));
    assert.equal(created.status, 0, created.stderr);
  }

  const results = await Promise.all(names.map((name) => workspaceAsync(
    main,
    'update',
    name,
    JSON.stringify({ pipeline: { marker: name } }),
  )));
  for (const result of results) assert.equal(result.status, 0, result.stderr);
  const state = JSON.parse(readFileSync(join(main, '.workspaces', 'workspaces.json'), 'utf8'));
  for (const name of names) {
    assert.equal(state.workspaces.find((item) => item.name === name)?.pipeline.marker, name);
  }
});

test('stale runtime locks fail closed and are never deleted by a waiter', (t) => {
  const { main } = fixture(t);
  const lock = join(main, '.workspaces', 'workspaces.lock');
  writeFileSync(lock, '2147483647\n');
  const helper = pathToFileURL(join(REPO, 'lib', 'safe-runtime-files.mjs')).href;
  const script = `
    import { withRuntimeLock } from ${JSON.stringify(helper)};
    withRuntimeLock('.workspaces/workspaces.lock', () => {
      throw new Error('entered operation without owning the lock');
    }, { timeoutMs: 20 });
  `;

  const result = run(process.execPath, ['--input-type=module', '-e', script], main, { allowFailure: true });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /remove this lock deliberately/);
  assert.doesNotMatch(result.stderr, /entered operation/);
  assert.equal(readFileSync(lock, 'utf8'), '2147483647\n');
});

test('delivery verification waits for the workspace lifecycle lock', async (t) => {
  const { main } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const published = JSON.parse(workspace(main, 'publish', 'test', integration.integratedSha).stdout);
  const key = createHash('sha256').update('test').digest('hex');
  const lock = join(main, '.workspaces', `lifecycle-${key}.lock`);
  writeFileSync(lock, 'test holder\n');

  let settled = false;
  const pending = workspaceAsync(main, 'verify-delivery', 'test', actionsEvidence(published))
    .then((result) => { settled = true; return result; });
  await delay(100);
  assert.equal(settled, false);
  assert.equal(record(main).delivery.deliveryVerified, false);
  rmSync(lock);

  const result = await pending;
  assert.equal(result.status, 0, result.stderr);
  assert.equal(record(main).delivery.deliveryVerified, true);
});

test('generic state mutation cannot forge delivery verification or terminal status', (t) => {
  const { main } = fixture(t);
  for (const patch of [
    { delivery: { deliveryVerified: true } },
    { pipeline: { deliveryVerified: true } },
    { status: 'done' },
    { status: 'active' },
  ]) {
    const result = workspace(main, 'update', 'test', JSON.stringify(patch));
    assert.equal(result.status, 1);
    assert.match(result.stderr, /lifecycle-owned/);
  }
  const forgedCreate = workspace(main, 'create', JSON.stringify({
    name: 'forged',
    status: 'active',
    delivery: { deliveryVerified: true },
  }));
  assert.equal(forgedCreate.status, 1);
  assert.match(forgedCreate.stderr, /lifecycle-owned/);
});

test('publish refuses when the default branch advances after integration', (t) => {
  const { main, worktreePath } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  writeFileSync(join(main, 'late.txt'), 'late default change\n');
  git(main, 'add', 'late.txt');
  git(main, 'commit', '-m', 'late default change');
  git(main, 'push');

  const result = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /advanced after integration/);
  assert.equal(git(worktreePath, 'rev-parse', 'HEAD'), integration.integratedSha);
  assert.equal(record(main).delivery.deploySha, undefined);
});

test('publish exact-leases default against a concurrent rollback', (t) => {
  const { origin, main, worktreePath } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const rollbackSha = git(main, 'rev-list', '--max-parents=0', 'HEAD');
  const hook = join(main, '.git', 'hooks', 'pre-push');
  writeFileSync(hook, '#!/bin/sh\ngit --git-dir="$RACE_ORIGIN" update-ref refs/heads/master "$RACE_SHA"\n');
  chmodSync(hook, 0o755);

  const result = workspaceWithEnv(main, {
    ...process.env,
    RACE_ORIGIN: origin,
    RACE_SHA: rollbackSha,
  }, 'publish', 'test', integration.integratedSha);
  assert.equal(result.status, 1);
  assert.equal(git(origin, 'rev-parse', 'refs/heads/master'), rollbackSha);
  assert.equal(git(worktreePath, 'rev-parse', 'HEAD'), integration.integratedSha);
  assert.equal(record(main).delivery.deploySha, undefined);
});

test('publish sends the captured reviewed SHA when worktree HEAD races', (t) => {
  const { origin, main, worktreePath } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const hook = join(main, '.git', 'hooks', 'pre-push');
  writeFileSync(hook, '#!/bin/sh\nprintf \'raced\\n\' > "$RACE_WORKTREE/race.txt"\ngit -C "$RACE_WORKTREE" add race.txt\ngit -C "$RACE_WORKTREE" commit -m \'raced commit\' >/dev/null\n');
  chmodSync(hook, 0o755);

  const result = workspaceWithEnv(main, {
    ...process.env,
    RACE_WORKTREE: worktreePath,
  }, 'publish', 'test', integration.integratedSha);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /workspace HEAD changed during publish/);
  assert.equal(git(origin, 'rev-parse', 'refs/heads/master'), integration.integratedSha);
  assert.notEqual(git(worktreePath, 'rev-parse', 'HEAD'), integration.integratedSha);
  assert.equal(record(main).delivery.deploySha, undefined);
});

test('delivery verification rejects evidence for another deployed SHA', (t) => {
  const { main } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const publishResult = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(publishResult.status, 0);
  const published = JSON.parse(publishResult.stdout);
  const mismatchedEvidence = actionsEvidence(published, { deploySha: '0'.repeat(40) });

  const result = workspace(main, 'verify-delivery', 'test', mismatchedEvidence);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /identity does not match/);
  assert.equal(record(main).delivery.deliveryVerified, false);
});

test('delivery verification rejects empty and failed gate results', (t) => {
  const { main } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const publishResult = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(publishResult.status, 0);
  const published = JSON.parse(publishResult.stdout);
  const base = JSON.parse(actionsEvidence(published));
  const invalid = [
    { ...base, ci: {} },
    { ...base, ci: { status: 'passed', provider: 'github-actions', runs: [] } },
    {
      ...base,
      ci: {
        status: 'passed',
        provider: 'github-actions',
        runs: [{ id: 1, headSha: published.deploySha, status: 'completed', conclusion: 'failure' }],
      },
    },
    { ...base, deployment: { status: 'passed', command: 'sha256:not-a-hash', exitStatus: 0 } },
  ];
  for (const evidence of invalid) {
    const result = workspace(main, 'verify-delivery', 'test', JSON.stringify(evidence));
    assert.equal(result.status, 1);
  }
  assert.equal(record(main).delivery.deliveryVerified, false);
});

test('publish preserves a dirty local default checkout', (t) => {
  const { origin, main } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const localMainBefore = git(main, 'rev-parse', 'HEAD');
  writeFileSync(join(main, 'base.txt'), 'base\nuser-local change\n');

  const result = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr, '');
  assert.equal(readFileSync(join(main, 'base.txt'), 'utf8'), 'base\nuser-local change\n');
  assert.equal(git(main, 'rev-parse', 'HEAD'), localMainBefore);
  assert.equal(git(origin, 'rev-parse', 'refs/heads/master'), integration.integratedSha);
});

test('publish leaves a clean local default checkout unchanged', (t) => {
  const { origin, main } = fixture(t);
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const localMainBefore = git(main, 'rev-parse', 'HEAD');
  const treeBefore = git(main, 'write-tree');
  const statusBefore = git(main, 'status', '--porcelain=v1', '--untracked-files=all');

  const result = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(git(main, 'rev-parse', 'HEAD'), localMainBefore);
  assert.equal(git(main, 'write-tree'), treeBefore);
  assert.equal(git(main, 'status', '--porcelain=v1', '--untracked-files=all'), statusBefore);
  assert.equal(git(origin, 'rev-parse', 'refs/heads/master'), integration.integratedSha);
});

test('integrate refuses a rebase conflict without advancing delivery state', (t) => {
  const { origin, main, worktreePath } = fixture(t);
  writeFileSync(join(main, 'base.txt'), 'base\nmain conflict\n');
  git(main, 'add', 'base.txt');
  git(main, 'commit', '-m', 'conflicting default change');
  git(main, 'push');
  const remoteBefore = git(origin, 'rev-parse', 'refs/heads/master');

  const result = workspace(main, 'integrate', 'test');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /integration rebase failed/);
  assert.equal(git(origin, 'rev-parse', 'refs/heads/master'), remoteBefore);
  assert.equal(record(main).delivery?.integratedSha, undefined);
  assert.notEqual(git(worktreePath, 'status', '--porcelain=v1'), '');
});

test('finish removes an existing landed remote feature ref', (t) => {
  const { origin, main, worktreePath } = fixture(t, { remoteFeature: true });
  deliver(main);

  const result = workspace(main, 'finish', 'test');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(existsSync(worktreePath), false);
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], origin, { allowFailure: true }).status, 1);
});

test('finish restores the remote feature ref after a concurrent default rollback', (t) => {
  const { origin, main, worktreePath } = fixture(t, { remoteFeature: true });
  deliver(main);
  const rollbackSha = git(main, 'rev-list', '--max-parents=0', 'HEAD');
  const hook = join(main, '.git', 'hooks', 'pre-push');
  writeFileSync(hook, '#!/bin/sh\ngit --git-dir="$RACE_ORIGIN" update-ref refs/heads/master "$RACE_SHA"\n');
  chmodSync(hook, 0o755);

  const result = workspaceWithEnv(main, {
    ...process.env,
    RACE_ORIGIN: origin,
    RACE_SHA: rollbackSha,
  }, 'finish', 'test');
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /not contained in origin\/master/);
  assert.equal(git(origin, 'rev-parse', 'refs/heads/master'), rollbackSha);
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], origin, { allowFailure: true }).status, 0);
  assert.equal(existsSync(worktreePath), true);
  assert.equal(record(main).status, 'active');
});

test('finish preserves a divergent remote feature branch', (t) => {
  const { root, origin, main, worktreePath } = fixture(t, { remoteFeature: true });
  const integration = JSON.parse(workspace(main, 'integrate', 'test').stdout);
  const publishResult = workspace(main, 'publish', 'test', integration.integratedSha);
  assert.equal(publishResult.status, 0);
  const published = JSON.parse(publishResult.stdout);
  assert.equal(workspace(main, 'verify-delivery', 'test', actionsEvidence(published)).status, 0);

  const other = join(root, 'other');
  run('git', ['clone', origin, other], root);
  git(other, 'config', 'user.name', 'Test User');
  git(other, 'config', 'user.email', 'test@example.com');
  git(other, 'checkout', 'feature/test');
  writeFileSync(join(other, 'divergent.txt'), 'not on master\n');
  git(other, 'add', 'divergent.txt');
  git(other, 'commit', '-m', 'divergent feature tip');
  git(other, 'push');

  const result = workspace(main, 'finish', 'test');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /not contained in origin\/master/);
  assert.equal(existsSync(worktreePath), true);
  assert.equal(record(main).status, 'active');
  assert.notEqual(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], origin, { allowFailure: true }).status, 1);
});

test('finish refuses a feature branch moved to another worktree', (t) => {
  const { main, worktreePath } = fixture(t);
  deliver(main);
  const movedPath = join(main, '.workspaces', 'worktrees', 'moved-test');
  git(main, 'worktree', 'move', worktreePath, movedPath);

  const result = workspace(main, 'finish', 'test');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /checked out at .*moved-test/);
  assert.equal(existsSync(movedPath), true);
  assert.equal(record(main).status, 'active');
});

test('finish resumes after worktree and local-ref cleanup', (t) => {
  const { main, worktreePath } = fixture(t);
  deliver(main);
  git(main, 'worktree', 'remove', worktreePath, '--force');
  git(main, 'update-ref', '-d', 'refs/heads/feature/test');

  const result = workspace(main, 'finish', 'test');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(record(main).status, 'done');
  assert.equal(record(main).worktreePath, null);
});

for (const unsafe of [
  {
    name: 'remote database host',
    setup({ main }) {
      writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:secret@db.example.com/app\n');
    },
    error: /DATABASE_URL is not local/,
  },
  {
    name: 'libpq host override',
    setup({ main }) {
      writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:secret@localhost/app?host=db.example.com\n');
    },
    error: /unsafe libpq target override/,
  },
  {
    name: 'symlinked env file',
    setup({ root, main }) {
      const outside = join(root, 'outside.env');
      writeFileSync(outside, 'DATABASE_URL=postgres://user:secret@localhost/app\n');
      symlinkSync(outside, join(main, '.env'));
    },
    error: /env file must not be a symlink/,
  },
]) {
  test(`database cleanup refuses ${unsafe.name} before teardown`, (t) => {
    const fixtureState = fixture(t);
    const { root, main, worktreePath } = fixtureState;
    deliver(main);
    unsafe.setup(fixtureState);
    const stateUpdate = workspace(main, 'update', 'test', JSON.stringify({
      dbName: 'app_ws_test',
      dbIsolation: 'template',
      dbTemplate: 'app',
      dbEndpoint: localDbEndpoint(),
      envFile: '.env',
    }));
    assert.equal(stateUpdate.status, 0, stateUpdate.stderr);
    const { env, marker } = fakePsql(root);

    const result = workspaceWithEnv(main, env, 'finish', 'test');
    assert.equal(result.status, 1);
    assert.match(result.stderr, unsafe.error);
    assert.equal(existsSync(marker), false);
    assert.equal(existsSync(worktreePath), true);
    assert.equal(record(main).status, 'active');
  });
}

test('database cleanup uses local discrete psql arguments without exposing the password', (t) => {
  const fixtureState = fixture(t);
  const { root, main } = fixtureState;
  deliver(main);
  writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:do-not-expose@localhost:5433/app\n');
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({
    dbName: 'app_ws_test',
    dbIsolation: 'template',
    dbTemplate: 'app',
    dbEndpoint: localDbEndpoint({ port: '5433' }),
    envFile: '.env',
  })).status, 0);
  const { env, marker } = fakePsql(root);

  const result = workspaceWithEnv(main, env, 'finish', 'test');
  assert.equal(result.status, 0, result.stderr);
  const args = readFileSync(marker, 'utf8');
  assert.match(args, /--host localhost --dbname postgres/);
  assert.match(args, /--port 5433 --username user/);
  assert.match(args, /PGHOSTADDR=unset/);
  assert.doesNotMatch(args, /do-not-expose/);
});

test('database provisioning uses the validated local endpoint and scrubs inherited routing', (t) => {
  const { root, main } = fixture(t);
  writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:do-not-expose@localhost:5433/app\n');
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({ envFile: '.env' })).status, 0);
  const { env, marker } = fakePsql(root);

  const result = workspaceWithEnv(
    main,
    env,
    'clone-database',
    'test',
    'DATABASE_URL',
    'app',
    'app_ws_test',
  );
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    dbName: 'app_ws_test',
    dbTemplate: 'app',
    dbEndpoint: localDbEndpoint({ port: '5433' }),
    retried: false,
  });
  assert.equal(record(main).dbUrlVar, 'DATABASE_URL');
  const args = readFileSync(marker, 'utf8');
  assert.match(args, /--host localhost --dbname postgres/);
  assert.match(args, /--port 5433 --username user/);
  assert.match(args, /CREATE DATABASE "app_ws_test" TEMPLATE "app"/);
  assert.match(args, /PGHOSTADDR=unset/);
  assert.doesNotMatch(args, /do-not-expose/);
});

test('database provisioning and removal share one workspace lifecycle lock', async (t) => {
  const { root, main, worktreePath } = fixture(t);
  writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:secret@localhost/app\n');
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({ envFile: '.env' })).status, 0);

  const bin = join(root, 'blocking-fake-bin');
  const log = join(root, 'psql-log');
  const started = join(root, 'psql-started');
  const release = join(root, 'psql-release');
  mkdirSync(bin);
  const executable = join(bin, 'psql');
  writeFileSync(executable, [
    '#!/bin/sh',
    'printf \'%s\\n\' "$*" >> "$PSQL_LOG"',
    'case "$*" in',
    '  *\"CREATE DATABASE\"*)',
    '    : > "$PSQL_STARTED"',
    '    while [ ! -f "$PSQL_RELEASE" ]; do sleep 0.01; done',
    '    ;;',
    'esac',
    '',
  ].join('\n'));
  chmodSync(executable, 0o755);
  const env = {
    ...process.env,
    PATH: `${bin}:${process.env.PATH}`,
    PSQL_LOG: log,
    PSQL_STARTED: started,
    PSQL_RELEASE: release,
  };

  const cloning = workspaceAsyncWithEnv(
    main,
    env,
    'clone-database',
    'test',
    'DATABASE_URL',
    'app',
    'app_ws_test',
  );
  for (let attempt = 0; attempt < 100 && !existsSync(started); attempt++) await delay(10);
  assert.equal(existsSync(started), true, 'clone did not reach the controlled psql boundary');

  let removalSettled = false;
  const removing = workspaceAsyncWithEnv(main, env, 'remove', 'test')
    .then((result) => { removalSettled = true; return result; });
  await delay(100);
  assert.equal(removalSettled, false);
  assert.equal(existsSync(worktreePath), true);
  writeFileSync(release, 'continue\n');

  const cloned = await cloning;
  assert.equal(cloned.status, 0, cloned.stderr);
  const removed = await removing;
  assert.equal(removed.status, 0, removed.stderr);
  assert.equal(existsSync(worktreePath), false);
  const calls = readFileSync(log, 'utf8');
  assert.ok(calls.indexOf('CREATE DATABASE') < calls.indexOf('DROP DATABASE'));
  assert.equal(workspace(main, 'get', 'test').status, 1);
});

test('project detection and provisioning share the declared primary URL variable', (t) => {
  const { root, main } = fixture(t);
  writeFileSync(join(main, 'package.json'), JSON.stringify({ dependencies: { pg: '1.0.0' } }));
  writeFileSync(join(main, '.env'), 'DIRECT_URL=postgres://user:secret@localhost/app\n');
  const detected = run(process.execPath, [PROJECT, 'detect'], main);
  const profile = JSON.parse(detected.stdout);
  assert.deepEqual(profile.dbUrlVars, ['DIRECT_URL']);
  assert.equal(profile.dbPrimaryUrlVar, 'DIRECT_URL');
  assert.equal(profile.dbTemplate, 'app');
  assert.equal(profile.dbIsolation, 'template');
  const cachedPath = join(main, '.workspaces', 'project.json');
  const legacyCache = JSON.parse(readFileSync(cachedPath, 'utf8'));
  delete legacyCache.dbPrimaryUrlVar;
  writeFileSync(cachedPath, JSON.stringify(legacyCache));
  const loadedLegacy = run(process.execPath, [PROJECT, 'load'], main);
  assert.equal(JSON.parse(loadedLegacy.stdout).dbPrimaryUrlVar, 'DIRECT_URL');
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({ envFile: profile.envFile })).status, 0);
  const { env } = fakePsql(root);

  const provisioned = workspaceWithEnv(
    main,
    env,
    'clone-database',
    'test',
    profile.dbPrimaryUrlVar,
    profile.dbTemplate,
    'app_ws_test',
  );
  assert.equal(provisioned.status, 0, provisioned.stderr);
  assert.equal(record(main).dbUrlVar, 'DIRECT_URL');
});

for (const unsafe of [
  {
    name: 'a remote endpoint',
    databaseUrl: 'postgres://user:secret@db.example.com/app',
    template: 'app',
    dbName: 'app_ws_test',
    error: /DATABASE_URL is not local/,
  },
  {
    name: 'a libpq query override',
    databaseUrl: 'postgres://user:secret@localhost/app?hostaddr=203.0.113.4',
    template: 'app',
    dbName: 'app_ws_test',
    error: /unsafe libpq target override/,
  },
  {
    name: 'a mismatched template',
    databaseUrl: 'postgres://user:secret@localhost/app',
    template: 'other',
    dbName: 'other_ws_test',
    error: /endpoint does not match the requested template/,
  },
  {
    name: 'an unsafe database identifier',
    databaseUrl: 'postgres://user:secret@localhost/app',
    template: 'app',
    dbName: 'app;drop_database',
    error: /workspace database name must contain only/,
  },
]) {
  test(`database provisioning refuses ${unsafe.name} before invoking psql`, (t) => {
    const { root, main } = fixture(t);
    writeFileSync(join(main, '.env'), `DATABASE_URL=${unsafe.databaseUrl}\n`);
    assert.equal(workspace(main, 'update', 'test', JSON.stringify({ envFile: '.env' })).status, 0);
    const { env, marker } = fakePsql(root);

    const result = workspaceWithEnv(
      main,
      env,
      'clone-database',
      'test',
      'DATABASE_URL',
      unsafe.template,
      unsafe.dbName,
    );
    assert.equal(result.status, 1);
    assert.match(result.stderr, unsafe.error);
    assert.equal(existsSync(marker), false);
  });
}

test('database cleanup refuses ordinary local endpoint drift', (t) => {
  const fixtureState = fixture(t);
  const { root, main, worktreePath } = fixtureState;
  deliver(main);
  writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:secret@localhost:5433/app\n');
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({
    dbName: 'app_ws_test',
    dbIsolation: 'template',
    dbTemplate: 'app',
    dbEndpoint: localDbEndpoint(),
    envFile: '.env',
  })).status, 0);
  const { env, marker } = fakePsql(root);

  const result = workspaceWithEnv(main, env, 'finish', 'test');
  assert.equal(result.status, 1);
  assert.match(result.stderr, /endpoint changed since provisioning/);
  assert.equal(existsSync(marker), false);
  assert.equal(existsSync(worktreePath), true);
});

test('socket database cleanup preserves socket transport', (t) => {
  const fixtureState = fixture(t);
  const { root, main } = fixtureState;
  deliver(main);
  writeFileSync(join(main, '.env'), 'DATABASE_URL=postgresql:///app\n');
  const endpoint = {
    transport: 'default-local-socket',
    host: '',
    port: '5432',
    user: '',
    database: 'app',
  };
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({
    dbName: 'app_ws_test',
    dbIsolation: 'template',
    dbTemplate: 'app',
    dbEndpoint: endpoint,
    envFile: '.env',
  })).status, 0);
  const { env, marker } = fakePsql(root);

  const result = workspaceWithEnv(main, env, 'finish', 'test');
  assert.equal(result.status, 0, result.stderr);
  assert.doesNotMatch(readFileSync(marker, 'utf8'), /--host/);
});

test('legacy database records recover only an unambiguous template identity', (t) => {
  const { main } = fixture(t);
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({
    dbName: 'app_ws_test',
    dbIsolation: 'template',
    dbTemplate: null,
  })).status, 0);

  const migrated = JSON.parse(workspace(main, 'get', 'test').stdout);
  assert.equal(migrated.dbTemplate, 'app');
  assert.equal(record(main).dbTemplate, 'app');
});

test('a legacy record recovers its missing endpoint and tears the database down', (t) => {
  const fixtureState = fixture(t);
  const { root, main, worktreePath } = fixtureState;
  deliver(main);
  writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:do-not-expose@localhost:5433/app\n');
  // The shape a workspace created before dbEndpoint was recorded still has:
  // template isolation and a database name, but nothing saying where it lives.
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({
    dbName: 'app_ws_test',
    dbIsolation: 'template',
    dbTemplate: 'app',
    dbEndpoint: null,
    envFile: '.env',
  })).status, 0);

  const migrated = JSON.parse(workspace(main, 'get', 'test').stdout);
  assert.deepEqual(migrated.dbEndpoint, localDbEndpoint({ port: '5433' }));
  assert.deepEqual(record(main).dbEndpoint, localDbEndpoint({ port: '5433' }));

  const { env, marker } = fakePsql(root);
  const result = workspaceWithEnv(main, env, 'finish', 'test');
  assert.equal(result.status, 0, result.stderr);
  const args = readFileSync(marker, 'utf8');
  assert.match(args, /DROP DATABASE IF EXISTS "app_ws_test"/);
  assert.match(args, /--host localhost --dbname postgres/);
  assert.doesNotMatch(args, /do-not-expose/);
  assert.equal(existsSync(worktreePath), false);
  assert.equal(record(main).status, 'done');
});

test('an unrecoverable legacy endpoint refuses with a repair command that works', (t) => {
  const fixtureState = fixture(t);
  const { root, main, worktreePath } = fixtureState;
  deliver(main);
  // Same legacy shape, but nothing to recover from: no env file was recorded.
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({
    dbName: 'app_ws_test',
    dbIsolation: 'template',
    dbTemplate: 'app',
    dbEndpoint: null,
    envFile: '',
  })).status, 0);
  assert.equal(JSON.parse(workspace(main, 'get', 'test').stdout).dbEndpoint, null);
  const { env, marker } = fakePsql(root);

  const refused = workspaceWithEnv(main, env, 'finish', 'test');
  assert.equal(refused.status, 1);
  assert.match(refused.stderr, /no recorded endpoint/);
  assert.match(refused.stderr, /has no safe main-checkout env file/);
  assert.match(refused.stderr, /workspace\.mjs update test '\{"envFile":"\.env","dbUrlVar":"DATABASE_URL"\}'/);
  assert.match(refused.stderr, /"dbIsolation":"none"/);
  assert.equal(existsSync(marker), false);
  assert.equal(existsSync(worktreePath), true);
  assert.equal(record(main).status, 'active');

  // The message has to be an instruction, not a gesture: run exactly what it
  // printed, and teardown must then complete.
  writeFileSync(join(main, '.env'), 'DATABASE_URL=postgres://user:secret@localhost/app\n');
  const repair = refused.stderr.match(/update test '(\{"envFile".*?\})'/)[1];
  assert.equal(workspace(main, 'update', 'test', repair).status, 0);
  assert.deepEqual(JSON.parse(workspace(main, 'get', 'test').stdout).dbEndpoint, localDbEndpoint());

  const result = workspaceWithEnv(main, env, 'finish', 'test');
  assert.equal(result.status, 0, result.stderr);
  assert.match(readFileSync(marker, 'utf8'), /DROP DATABASE IF EXISTS "app_ws_test"/);
  assert.equal(existsSync(worktreePath), false);
  assert.equal(record(main).status, 'done');
});

test('abandon and remove share validated resource cleanup', (t) => {
  const abandoned = fixture(t);
  writeFileSync(join(abandoned.worktreePath, 'untracked.txt'), 'discard me\n');
  const abandonResult = workspace(abandoned.main, 'abandon', 'test');
  assert.equal(abandonResult.status, 0, abandonResult.stderr);
  assert.equal(existsSync(abandoned.worktreePath), false);
  assert.equal(record(abandoned.main).status, 'abandoned');
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], abandoned.main, { allowFailure: true }).status, 1);

  const removed = fixture(t);
  const removeResult = workspace(removed.main, 'remove', 'test');
  assert.equal(removeResult.status, 0, removeResult.stderr);
  assert.equal(existsSync(removed.worktreePath), false);
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], removed.main, { allowFailure: true }).status, 1);
  assert.equal(workspace(removed.main, 'get', 'test').status, 1);
});

test('a provisional record cleans up before resources exist', (t) => {
  const { main, worktreePath } = fixture(t);
  git(main, 'worktree', 'remove', worktreePath, '--force');
  git(main, 'update-ref', '-d', 'refs/heads/feature/test');

  const result = workspace(main, 'remove', 'test');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(workspace(main, 'get', 'test').status, 1);
});

test('a provisioned record cleans database, worktree, and branch after later setup failure', (t) => {
  const { root, main, worktreePath } = fixture(t);
  writeFileSync(join(main, '.env'), 'DIRECT_URL=postgres://user:secret@localhost/app\n');
  assert.equal(workspace(main, 'update', 'test', JSON.stringify({ envFile: '.env' })).status, 0);
  const { env, marker } = fakePsql(root);
  const provisioned = workspaceWithEnv(
    main,
    env,
    'clone-database',
    'test',
    'DIRECT_URL',
    'app',
    'app_ws_test',
  );
  assert.equal(provisioned.status, 0, provisioned.stderr);
  writeFileSync(join(worktreePath, 'failed-install.tmp'), 'simulated later setup failure\n');

  const cleanup = workspaceWithEnv(main, env, 'remove', 'test');
  assert.equal(cleanup.status, 0, cleanup.stderr);
  assert.match(readFileSync(marker, 'utf8'), /DROP DATABASE IF EXISTS "app_ws_test"/);
  assert.equal(existsSync(worktreePath), false);
  assert.equal(run('git', ['show-ref', '--verify', '--quiet', 'refs/heads/feature/test'], main, { allowFailure: true }).status, 1);
  assert.equal(workspace(main, 'get', 'test').status, 1);
});
