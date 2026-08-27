import assert from 'node:assert/strict';
import {
  chmodSync,
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const INSTALLER = join(REPO, 'bin', 'install-codex-bwrap-profile');

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'codex-bwrap-installer-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, 'bin'));
  copyFileSync(INSTALLER, join(root, 'bin', 'install-codex-bwrap-profile'));
  chmodSync(join(root, 'bin', 'install-codex-bwrap-profile'), 0o755);
  return root;
}

function runInstaller(root) {
  return spawnSync(join(root, 'bin', 'install-codex-bwrap-profile'), [], {
    cwd: root,
    encoding: 'utf8',
  });
}

test('installer refuses a symlinked privileged source before invoking sudo', (t) => {
  const root = fixture(t);
  mkdirSync(join(root, 'apparmor.d'));
  symlinkSync('/etc/shadow', join(root, 'apparmor.d', 'codex-bwrap'));

  const result = runInstaller(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /canonical regular, non-symlink file/);
  assert.doesNotMatch(result.stderr, /password|sudo/i);
});

test('installer refuses a symlinked source directory before invoking sudo', (t) => {
  const root = fixture(t);
  const outside = join(root, 'outside');
  mkdirSync(outside);
  writeFileSync(join(outside, 'codex-bwrap'), 'attacker-controlled policy\n');
  symlinkSync(outside, join(root, 'apparmor.d'));

  const result = runInstaller(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /canonical regular, non-symlink file/);
  assert.doesNotMatch(result.stderr, /password|sudo/i);
});

test('privileged installer boundary uses absolute commands and pre-opened stdin', () => {
  const source = readFileSync(INSTALLER, 'utf8');
  assert.match(source, /\/usr\/bin\/sudo \/usr\/bin\/install[^\n]+\/dev\/stdin/);
  assert.match(source, /< "\$source_profile"/);
  assert.match(source, /\/usr\/bin\/sudo \/usr\/sbin\/apparmor_parser/);
  assert.doesNotMatch(source, /^sudo\s/m);
});
