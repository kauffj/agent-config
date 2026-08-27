import assert from 'node:assert/strict';
import {
  cpSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readlinkSync,
  readdirSync,
  realpathSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { classifyHarnessLinkTarget, resolveHarnessLinks } from './harness-layout.mjs';

const REPO = realpathSync(dirname(dirname(fileURLToPath(import.meta.url))));
const INSTALLER = join(REPO, 'bin', 'agent-config-install');
const DOCTOR = join(REPO, 'lib', 'doctor.mjs');
const EXPECTED_LINKS = [
  ['.config/agent-config', '.', 'directory'],
  ['.claude', '.', 'directory'],
  ['.codex/AGENTS.md', 'instructions/AGENTS.md', 'file'],
  ['.codex/hooks.json', 'codex/hooks.json', 'file'],
  ['.grok/AGENTS.md', 'instructions/AGENTS.md', 'file'],
  ['.agents/skills', 'skills', 'directory'],
];

function temporaryDirectory(t, prefix) {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function runNode(script, home, args = []) {
  return spawnSync(process.execPath, [script, ...args], {
    cwd: REPO,
    env: { ...process.env, HOME: home },
    encoding: 'utf8',
  });
}

function snapshotTree(root) {
  if (!lstatOrNull(root)) return [];
  const entries = [];
  function walk(path) {
    for (const entry of readdirSync(path, { withFileTypes: true })) {
      const absolute = join(path, entry.name);
      const rel = relative(root, absolute);
      if (entry.isSymbolicLink()) entries.push([rel, 'link', readlinkSync(absolute)]);
      else if (entry.isDirectory()) {
        entries.push([rel, 'directory']);
        walk(absolute);
      } else entries.push([rel, 'file', readFileSync(absolute, 'utf8')]);
    }
  }
  walk(root);
  return entries;
}

function lstatOrNull(path) {
  try { return lstatSync(path); }
  catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

function assertExpectedLinks(home, { chainedSkills = false, chainedHooks = false } = {}) {
  const expected = EXPECTED_LINKS.map(([homePath, repoPath]) => ({
    homePath,
    linkPath: join(home, homePath),
    targetPath: resolve(REPO, repoPath),
  }));
  const links = snapshotTree(home)
    .filter((entry) => entry[1] === 'link')
    .map((entry) => entry[0])
    .sort();
  assert.deepEqual(links, expected.map((entry) => entry.homePath).sort());
  for (const spec of expected) {
    assert.equal(lstatSync(spec.linkPath).isSymbolicLink(), true, spec.homePath);
    const expectedRawTarget = chainedSkills && spec.homePath === '.agents/skills'
      ? '../.claude/skills'
      : chainedHooks && spec.homePath === '.codex/hooks.json'
        ? '../.claude/codex/hooks.json'
        : spec.targetPath;
    assert.equal(readlinkSync(spec.linkPath), expectedRawTarget);
    assert.equal(realpathSync(spec.linkPath), realpathSync(spec.targetPath));
  }
}

test('shared layout matches the public discovery contract', () => {
  assert.deepEqual(
    resolveHarnessLinks(REPO, '/test-home').map(({ homePath, targetPath, targetType }) => [
      homePath,
      relative(REPO, targetPath) || '.',
      targetType,
    ]),
    EXPECTED_LINKS,
  );
});

test('an exact canonical link target must also resolve', () => {
  const spec = resolveHarnessLinks(REPO, '/test-home')
    .find(({ id }) => id === 'grok-instructions');
  assert.equal(classifyHarnessLinkTarget(spec, spec.targetPath, null), 'conflict');
  assert.equal(classifyHarnessLinkTarget(spec, spec.targetPath, spec.targetPath), 'correct');
});

test('installer creates exactly the six discovery links in an empty home', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');
  const result = runNode(INSTALLER, home);

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /created 6 link\(s\), migrated 0/);
  assertExpectedLinks(home);
});

test('installer refuses an incomplete source before creating links', (t) => {
  const root = temporaryDirectory(t, 'agent-config-source-');
  const fixtureRepo = join(root, 'repo');
  const home = join(root, 'home');
  for (const dir of ['bin', 'lib', 'skills']) mkdirSync(join(fixtureRepo, dir), { recursive: true });
  mkdirSync(home);
  cpSync(INSTALLER, join(fixtureRepo, 'bin', 'agent-config-install'));
  cpSync(join(REPO, 'lib', 'harness-layout.mjs'), join(fixtureRepo, 'lib', 'harness-layout.mjs'));

  const result = runNode(join(fixtureRepo, 'bin', 'agent-config-install'), home);

  assert.equal(result.status, 1);
  assert.match(result.stderr, /refusing to install from invalid source targets/);
  assert.match(result.stderr, /instructions\/AGENTS\.md.*does not exist/);
  assert.deepEqual(snapshotTree(home), []);
});

test('installer refuses a symlinked canonical source before creating links', (t) => {
  const root = temporaryDirectory(t, 'agent-config-source-');
  const fixtureRepo = join(root, 'repo');
  const home = join(root, 'home');
  for (const dir of ['bin', 'instructions', 'lib', 'skills']) {
    mkdirSync(join(fixtureRepo, dir), { recursive: true });
  }
  mkdirSync(home);
  cpSync(INSTALLER, join(fixtureRepo, 'bin', 'agent-config-install'));
  cpSync(join(REPO, 'lib', 'harness-layout.mjs'), join(fixtureRepo, 'lib', 'harness-layout.mjs'));
  writeFileSync(join(fixtureRepo, 'policy.md'), 'policy\n');
  symlinkSync('../policy.md', join(fixtureRepo, 'instructions', 'AGENTS.md'));

  const result = runNode(join(fixtureRepo, 'bin', 'agent-config-install'), home);

  assert.equal(result.status, 1);
  assert.match(result.stderr, /not its canonical source path/);
  assert.deepEqual(snapshotTree(home), []);
});

test('installer is idempotent and check mode is read-only', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');

  const missingSnapshot = snapshotTree(home);
  const missingCheck = runNode(INSTALLER, home, ['--check']);
  assert.equal(missingCheck.status, 1);
  assert.match(missingCheck.stdout, /\.config\/agent-config: missing/);
  assert.deepEqual(snapshotTree(home), missingSnapshot);

  assert.equal(runNode(INSTALLER, home).status, 0);
  const installedSnapshot = snapshotTree(home);
  const secondInstall = runNode(INSTALLER, home);
  assert.equal(secondInstall.status, 0, secondInstall.stderr);
  assert.match(secondInstall.stdout, /already configured/);
  assert.deepEqual(snapshotTree(home), installedSnapshot);

  const installedCheck = runNode(INSTALLER, home, ['--check']);
  assert.equal(installedCheck.status, 0, installedCheck.stdout);
  assert.match(installedCheck.stdout, /all discovery links are configured/);
  assert.deepEqual(snapshotTree(home), installedSnapshot);
});

test('installer migrates only the exact legacy Codex instruction link', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');
  const codexInstructions = join(home, '.codex', 'AGENTS.md');
  mkdirSync(dirname(codexInstructions), { recursive: true });
  symlinkSync('../.claude/CLAUDE.md', codexInstructions);

  const result = runNode(INSTALLER, home);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /created 5 link\(s\), migrated 1/);
  assertExpectedLinks(home);
});

test('installer upgrades the current chained-link topology without rewriting it', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');
  mkdirSync(join(home, '.agents'), { recursive: true });
  mkdirSync(join(home, '.codex'), { recursive: true });
  symlinkSync(REPO, join(home, '.claude'));
  symlinkSync('../.claude/skills', join(home, '.agents', 'skills'));
  symlinkSync('../.claude/CLAUDE.md', join(home, '.codex', 'AGENTS.md'));
  symlinkSync('../.claude/codex/hooks.json', join(home, '.codex', 'hooks.json'));

  const result = runNode(INSTALLER, home);

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /created 2 link\(s\), migrated 1/);
  assert.equal(readlinkSync(join(home, '.agents', 'skills')), '../.claude/skills');
  assert.equal(readlinkSync(join(home, '.codex', 'hooks.json')), '../.claude/codex/hooks.json');
  assertExpectedLinks(home, { chainedSkills: true, chainedHooks: true });
});

test('installer and doctor reject an arbitrary realpath-equivalent link chain', (t) => {
  const root = temporaryDirectory(t, 'agent-config-home-');
  const home = join(root, 'home');
  const untrusted = join(root, 'untrusted');
  mkdirSync(home);
  mkdirSync(untrusted);
  assert.equal(runNode(INSTALLER, home).status, 0);

  const sharedSkills = join(home, '.agents', 'skills');
  const untrustedAlias = join(untrusted, 'skills');
  unlinkSync(sharedSkills);
  symlinkSync(join(REPO, 'skills'), untrustedAlias);
  symlinkSync(untrustedAlias, sharedSkills);
  assert.equal(realpathSync(sharedSkills), realpathSync(join(REPO, 'skills')));
  const before = snapshotTree(root);

  const install = runNode(INSTALLER, home);
  assert.equal(install.status, 1);
  assert.match(install.stderr, /refusing to change conflicting paths/);
  assert.deepEqual(snapshotTree(root), before);

  const diagnosis = runNode(DOCTOR, home);
  assert.equal(diagnosis.status, 0, diagnosis.stdout + diagnosis.stderr);
  assert.match(diagnosis.stdout, /~\/\.agents\/skills: resolves to/);
  assert.deepEqual(snapshotTree(root), before);
});

for (const conflict of [
  {
    name: 'regular file',
    create(path) { writeFileSync(path, 'do not replace\n'); },
  },
  {
    name: 'directory',
    create(path) { mkdirSync(path); },
  },
  {
    name: 'unrelated symlink',
    create(path) { symlinkSync(REPO, path); },
  },
  {
    name: 'dangling symlink',
    create(path) { symlinkSync('../missing-policy.md', path); },
  },
]) {
  test(`installer preserves and rejects a conflicting ${conflict.name}`, (t) => {
    const home = temporaryDirectory(t, 'agent-config-home-');
    const codexInstructions = join(home, '.codex', 'AGENTS.md');
    mkdirSync(dirname(codexInstructions), { recursive: true });
    conflict.create(codexInstructions);
    const before = snapshotTree(home);

    const result = runNode(INSTALLER, home);

    assert.equal(result.status, 1);
    assert.match(result.stderr, /refusing to change conflicting paths/);
    assert.deepEqual(snapshotTree(home), before);
  });
}

test('installer preserves and rejects a non-directory parent', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');
  writeFileSync(join(home, '.config'), 'do not replace\n');
  const before = snapshotTree(home);

  const result = runNode(INSTALLER, home);

  assert.equal(result.status, 1);
  assert.match(result.stderr, /~\/\.config is not a directory/);
  assert.deepEqual(snapshotTree(home), before);
});

test('installer preserves and rejects a symlinked parent', (t) => {
  const root = temporaryDirectory(t, 'agent-config-home-');
  const home = join(root, 'home');
  const outside = join(root, 'outside');
  mkdirSync(home);
  mkdirSync(outside);
  symlinkSync(outside, join(home, '.codex'));
  const beforeHome = snapshotTree(home);
  const beforeOutside = snapshotTree(outside);

  const result = runNode(INSTALLER, home);

  assert.equal(result.status, 1);
  assert.match(result.stderr, /~\/\.codex must not be a symbolic link/);
  assert.deepEqual(snapshotTree(home), beforeHome);
  assert.deepEqual(snapshotTree(outside), beforeOutside);
});

test('installer rejects non-canonical and root HOME paths', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');

  const nonCanonical = runNode(INSTALLER, `${home}/../${home.slice(home.lastIndexOf('/') + 1)}`);
  assert.equal(nonCanonical.status, 1);
  assert.match(nonCanonical.stderr, /canonical, non-root path/);

  const rootHome = runNode(INSTALLER, '/');
  assert.equal(rootHome.status, 1);
  assert.match(rootHome.stderr, /canonical, non-root path/);
  assert.deepEqual(snapshotTree(home), []);
});

test('doctor reports missing discovery links as warnings without modifying home', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');
  const before = snapshotTree(home);

  const result = runNode(DOCTOR, home);

  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /\.claude: discovery link is missing/);
  assert.match(result.stdout, /\.agents\/skills: discovery link is missing/);
  assert.deepEqual(snapshotTree(home), before);
});

test('doctor warns instead of failing when a discovery parent is not a directory', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');
  writeFileSync(join(home, '.config'), 'conflict\n');
  const before = snapshotTree(home);

  const result = runNode(DOCTOR, home);

  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /\.config\/agent-config: discovery link is missing/);
  assert.deepEqual(snapshotTree(home), before);
});

test('doctor accepts the installed layout', (t) => {
  const home = temporaryDirectory(t, 'agent-config-home-');
  assert.equal(runNode(INSTALLER, home).status, 0);

  const installed = runNode(DOCTOR, home);
  assert.equal(installed.status, 0, installed.stdout + installed.stderr);
  assert.doesNotMatch(installed.stdout, /discovery link/);
});

test('doctor warns when a canonical instruction link is dangling', (t) => {
  const root = temporaryDirectory(t, 'agent-config-doctor-');
  const fixtureRepo = join(root, 'repo');
  const home = join(root, 'home');
  for (const dir of ['agents', 'commands', 'hooks', 'instructions', 'lib', 'skills']) {
    mkdirSync(join(fixtureRepo, dir), { recursive: true });
  }
  mkdirSync(join(home, '.grok'), { recursive: true });
  cpSync(DOCTOR, join(fixtureRepo, 'lib', 'doctor.mjs'));
  cpSync(join(REPO, 'lib', 'harness-layout.mjs'), join(fixtureRepo, 'lib', 'harness-layout.mjs'));
  writeFileSync(join(fixtureRepo, 'settings.json'), '{"env":{}}\n');
  symlinkSync(
    join(fixtureRepo, 'instructions', 'AGENTS.md'),
    join(home, '.grok', 'AGENTS.md'),
  );

  const result = spawnSync(process.execPath, [join(fixtureRepo, 'lib', 'doctor.mjs')], {
    cwd: fixtureRepo,
    env: { ...process.env, HOME: home },
    encoding: 'utf8',
  });

  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /~\/\.grok\/AGENTS\.md: is dangling/);

  mkdirSync(join(fixtureRepo, 'instructions', 'AGENTS.md'));
  const wrongType = spawnSync(process.execPath, [join(fixtureRepo, 'lib', 'doctor.mjs')], {
    cwd: fixtureRepo,
    env: { ...process.env, HOME: home },
    encoding: 'utf8',
  });
  assert.equal(wrongType.status, 0, wrongType.stdout + wrongType.stderr);
  assert.match(wrongType.stdout, /~\/\.grok\/AGENTS\.md: target is not a file/);
});

test('doctor retains source-integrity errors', (t) => {
  const root = temporaryDirectory(t, 'agent-config-doctor-');
  const fixtureRepo = join(root, 'repo');
  const home = join(root, 'home');
  for (const dir of ['agents', 'commands', 'hooks', 'instructions', 'lib', 'skills']) {
    mkdirSync(join(fixtureRepo, dir), { recursive: true });
  }
  mkdirSync(home);
  cpSync(DOCTOR, join(fixtureRepo, 'lib', 'doctor.mjs'));
  cpSync(join(REPO, 'lib', 'harness-layout.mjs'), join(fixtureRepo, 'lib', 'harness-layout.mjs'));
  writeFileSync(join(fixtureRepo, 'settings.json'), '{"env":{}}\n');
  writeFileSync(join(fixtureRepo, 'instructions', 'AGENTS.md'), 'fixture policy\n');
  writeFileSync(join(fixtureRepo, 'hooks', 'broken.sh'), '$HOME/.claude/lib/not-here.mjs\n');

  const result = spawnSync(process.execPath, [join(fixtureRepo, 'lib', 'doctor.mjs')], {
    cwd: fixtureRepo,
    env: { ...process.env, HOME: home },
    encoding: 'utf8',
  });

  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stdout, /references missing file → lib\/not-here\.mjs/);
});
