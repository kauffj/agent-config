import assert from 'node:assert/strict';
import {
  appendFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const PROJECT = join(REPO, 'lib', 'project.mjs');
const WORKSPACE = join(REPO, 'lib', 'workspace.mjs');

function run(command, args, cwd, { allowFailure = false } = {}) {
  const result = spawnSync(command, args, { cwd, encoding: 'utf8' });
  if (result.error) throw result.error;
  if (!allowFailure && result.status !== 0) {
    assert.fail(`${command} ${args.join(' ')} failed:\n${result.stderr || result.stdout}`);
  }
  return result;
}

function git(cwd, ...args) {
  return run('git', args, cwd).stdout.trim();
}

function project(cwd, command, { allowFailure = false } = {}) {
  return run(process.execPath, [PROJECT, command], cwd, { allowFailure });
}

function fixture(t, { ignored = true } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'project-profile-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  git(root, 'init');
  git(root, 'config', 'user.name', 'Test User');
  git(root, 'config', 'user.email', 'test@example.com');
  writeFileSync(join(root, 'README.md'), 'fixture\n');
  git(root, 'add', 'README.md');
  git(root, 'commit', '-m', 'base');
  if (ignored) appendFileSync(join(root, '.git', 'info', 'exclude'), '.workspaces/\n');
  return root;
}

test('npm projects with a package lock use npm ci', (t) => {
  const root = fixture(t);
  writeFileSync(join(root, 'package.json'), JSON.stringify({ scripts: { build: 'echo build' } }));
  writeFileSync(join(root, 'package-lock.json'), '{}\n');

  const profile = JSON.parse(project(root, 'detect').stdout);
  assert.equal(profile.pkgMgr, 'npm');
  assert.equal(profile.installCmd, 'npm ci');
  assert.equal(profile.buildCmd, 'npm run build');
  assert.equal(Object.hasOwn(profile, 'defaultBranch'), false);
});

test('npm projects without a package lock use npm install', (t) => {
  const root = fixture(t);
  writeFileSync(join(root, 'package.json'), '{}\n');

  const profile = JSON.parse(project(root, 'detect').stdout);
  assert.equal(profile.installCmd, 'npm install');
});

test('ignored runtime profiles refresh when detector inputs change', (t) => {
  const root = fixture(t);
  writeFileSync(join(root, 'package.json'), '{}\n');
  assert.equal(JSON.parse(project(root, 'load').stdout).installCmd, 'npm install');

  writeFileSync(join(root, 'package-lock.json'), '{}\n');
  assert.equal(JSON.parse(project(root, 'load').stdout).installCmd, 'npm ci');
});

test('tracked neutral project overrides are applied without entering the runtime cache', (t) => {
  const root = fixture(t);
  mkdirSync(join(root, '.agent'));
  writeFileSync(join(root, '.agent', 'project.json'), JSON.stringify({ devCmd: 'make serve' }));
  git(root, 'add', '.agent/project.json');
  git(root, 'commit', '-m', 'add project override');

  const profile = JSON.parse(project(root, 'load').stdout);
  const cached = JSON.parse(readFileSync(join(root, '.workspaces', 'project.json'), 'utf8'));
  assert.equal(profile.devCmd, 'make serve');
  assert.equal(cached.devCmd, null);
  assert.equal(Object.hasOwn(cached, 'overrides'), false);
});

test('untracked project overrides are refused as nondurable', (t) => {
  const root = fixture(t);
  mkdirSync(join(root, '.agent'));
  writeFileSync(join(root, '.agent', 'project.json'), JSON.stringify({ devCmd: 'make serve' }));

  const result = project(root, 'load', { allowFailure: true });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /must be tracked/);
});

test('legacy runtime-cache overrides are refused with a migration instruction', (t) => {
  const root = fixture(t);
  project(root, 'detect');
  const cache = join(root, '.workspaces', 'project.json');
  const profile = JSON.parse(readFileSync(cache, 'utf8'));
  writeFileSync(cache, JSON.stringify({ ...profile, overrides: { devCmd: 'make serve' } }));

  const result = project(root, 'load', { allowFailure: true });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /must move to tracked \.agent\/project\.json/);
});

test('tracked project profiles are refused without changing their bytes', (t) => {
  const root = fixture(t);
  const cacheDir = join(root, '.workspaces');
  const cache = join(cacheDir, 'project.json');
  mkdirSync(cacheDir);
  writeFileSync(cache, '{"inputs":{"stale":true},"keep":"exactly"}\n');
  git(root, 'add', '-f', '.workspaces/project.json');
  git(root, 'commit', '-m', 'track runtime profile');
  const before = readFileSync(cache, 'utf8');

  const result = project(root, 'load', { allowFailure: true });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /is tracked; untrack it/);
  assert.equal(readFileSync(cache, 'utf8'), before);
});

test('nonignored project profile destinations are refused', (t) => {
  const root = fixture(t, { ignored: false });

  const result = project(root, 'load', { allowFailure: true });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /is not ignored/);
});

test('workspace bootstrap establishes ignored runtime state idempotently', (t) => {
  const root = fixture(t, { ignored: false });
  const first = JSON.parse(run(process.execPath, [WORKSPACE, 'bootstrap'], root).stdout);
  const second = JSON.parse(run(process.execPath, [WORKSPACE, 'bootstrap'], root).stdout);

  assert.equal(first.addedExclude, true);
  assert.equal(second.addedExclude, false);
  assert.match(readFileSync(join(root, '.git', 'info', 'exclude'), 'utf8'), /^\.workspaces\/$/m);
  assert.equal(project(root, 'load').status, 0);
});

test('runtime cache symlinks are refused without touching their target', (t) => {
  const root = fixture(t);
  const outside = join(dirname(root), `${basename(root)}-outside-cache`);
  t.after(() => rmSync(outside, { force: true }));
  writeFileSync(outside, 'keep exactly\n');
  mkdirSync(join(root, '.workspaces'));
  symlinkSync(outside, join(root, '.workspaces', 'project.json'));

  const result = project(root, 'load', { allowFailure: true });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /regular, non-symlink file/);
  assert.equal(readFileSync(outside, 'utf8'), 'keep exactly\n');
});

test('legacy project-profile symlinks are refused without moving or reading them', (t) => {
  const root = fixture(t);
  const outside = join(dirname(root), `${basename(root)}-outside-legacy`);
  t.after(() => rmSync(outside, { force: true }));
  writeFileSync(outside, '{"secret":"keep"}\n');
  mkdirSync(join(root, '.claude'));
  symlinkSync(outside, join(root, '.claude', 'project.json'));

  const result = project(root, 'load', { allowFailure: true });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /regular, non-symlink file/);
  assert.equal(readFileSync(outside, 'utf8'), '{"secret":"keep"}\n');
});

test('older cached profiles derive the primary database URL variable in memory', (t) => {
  const root = fixture(t);
  writeFileSync(join(root, 'package.json'), JSON.stringify({ dependencies: { pg: '1.0.0' } }));
  writeFileSync(join(root, '.env'), 'DIRECT_URL=postgres://user:secret@localhost/app\n');
  project(root, 'detect');
  const cache = join(root, '.workspaces', 'project.json');
  const legacy = JSON.parse(readFileSync(cache, 'utf8'));
  delete legacy.dbPrimaryUrlVar;
  writeFileSync(cache, JSON.stringify(legacy, null, 2));
  const bytesBeforeLoad = readFileSync(cache, 'utf8');

  const loaded = JSON.parse(project(root, 'load').stdout);
  assert.equal(loaded.dbPrimaryUrlVar, 'DIRECT_URL');
  assert.equal(readFileSync(cache, 'utf8'), bytesBeforeLoad);
});

test('profiles keep the repository identity inside a nested worktree', (t) => {
  const root = fixture(t);
  const worktree = join(root, '.workspaces', 'worktrees', 'nested-task');
  mkdirSync(dirname(worktree), { recursive: true });
  git(root, 'worktree', 'add', '-b', 'feature/nested-task', worktree);
  writeFileSync(join(worktree, 'package.json'), '{}\n');

  const profile = JSON.parse(project(worktree, 'detect').stdout);
  assert.equal(profile.repoName, basename(root));
  assert.notEqual(profile.repoName, 'nested-task');
});
