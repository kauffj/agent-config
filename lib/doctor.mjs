#!/usr/bin/env node
// Repo integrity checker for the Claude config repo.
//
// Resolves every cross-reference in the config — file paths embedded in
// skills/agents/commands/hooks, settings.json `env` values, and the Claude/Codex
// symlinks — against what is actually on disk, and reports anything dangling.
// Its job is to turn SILENT breakage (a renamed agent file, a deleted lib
// script, a moved principles doc) into a loud, early failure.
//
// CLI:
//   node lib/doctor.mjs        # check; prints a report and exits 1 on any ERROR
//
// Design: pure check-and-report. doctor knows nothing about who invoked it
// (SessionStart, pre-commit, manual) — it emits findings + an exit code, and
// the caller decides policy (warn vs. block).

import { readFileSync, readdirSync, existsSync, realpathSync, statSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// Canonical (symlink-resolved) repo root, so the ~/.claude self-check below
// compares like-for-like however doctor was invoked (real path or symlink).
const REPO = realpathSync(dirname(dirname(fileURLToPath(import.meta.url)))); // lib/.. = repo root
const HOME = process.env.HOME || '';

// Only these trees + settings.json are scanned for embedded file references.
const SCAN_DIRS = ['skills', 'agents', 'commands', 'hooks', 'codex'];
// lib/ is a reference target but is deliberately not a scanned source tree.
const REF_DIRS = [...SCAN_DIRS, 'lib'];
const SCAN_EXTS = new Set(['.md', '.sh', '.json', '.mjs', '.js']);

const errors = [];
const warnings = [];
const err = (m) => errors.push(m);
const warn = (m) => warnings.push(m);

// Every reference that points at nothing, classified in one batch at the end
// (see classifyMissing). { rel, source }.
const missing = [];

function walk(dir) {
  const out = [];
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, ent.name);
    if (ent.isDirectory()) out.push(...walk(p));
    else out.push(p);
  }
  return out;
}

const relToRepo = (abs) => abs.startsWith(REPO + '/') ? abs.slice(REPO.length + 1) : abs;
const stripTrail = (s) => s.replace(/[.,:;`'")\]]+$/, '');

// Extract on-disk file references from text and report any that don't exist.
// Two shapes are recognized (both false-positive-resistant):
//   1. $HOME/.claude/<path>, ${HOME}/.claude/<path>, ~/.claude/<path>
//   2. bare relative refs into a known config dir, ending in a known extension
//      (agents/foo.md, lib/bar.mjs, hooks/baz.sh, ...)
// Runtime placeholders like $WORKTREE_PATH / $PORT never match either shape.
function checkRefsInText(text, source) {
  const rels = new Set();
  const homeRe = /(?:\$HOME|\$\{HOME\}|~)\/\.claude\/([A-Za-z0-9._/-]+)/g;
  const relRe = new RegExp(
    `\\b(?:${REF_DIRS.join('|')})/[A-Za-z0-9._/-]*\\.(?:md|mjs|js|sh|json|ts)\\b`,
    'g',
  );
  let m;
  while ((m = homeRe.exec(text))) rels.add(stripTrail(m[1]));
  while ((m = relRe.exec(text))) rels.add(stripTrail(m[0]));
  for (const rel of rels) {
    if (!existsSync(join(REPO, rel))) missing.push({ rel, source });
  }
}

// A reference to a GITIGNORED path is a runtime output — state/, meta/, caches
// the hooks write on first run — not a source file this repo ships. It is absent
// from a fresh clone by design and present on a running install, so treating it
// as a broken reference made `doctor` exit 1 on every clone, and pre-commit
// (which execs doctor) refused the first commit. Asking git keeps the two
// categories apart with no second list to drift out of sync with .gitignore.
function ignoredSet(rels) {
  if (!rels.length) return new Set();
  const parse = (s) => new Set((s ?? '').split('\n').filter(Boolean));
  try {
    return parse(execSync('git check-ignore --stdin', {
      cwd: REPO, input: rels.join('\n'), encoding: 'utf8',
      stdio: ['pipe', 'pipe', 'ignore'],
    }));
  } catch (e) {
    // exit 1 just means "none of these are ignored" — not a failure. Any other
    // exit (no git, not a repo) yields nothing, so everything stays an error:
    // when we cannot tell the categories apart, the loud one is the safe one.
    return parse(e.stdout);
  }
}

function classifyMissing() {
  const ignored = ignoredSet([...new Set(missing.map((m) => m.rel))]);
  for (const { rel, source } of missing) {
    if (ignored.has(rel)) warn(`${source}: runtime path, not created until first run → ${rel}`);
    else err(`${source}: references missing file → ${rel}`);
  }
}

// Resolve a settings.json env VALUE to an absolute path, or null if it isn't
// path-like (a flag, number, or plain string).
function resolveEnvPath(val) {
  const v = val.trim();
  let m;
  if ((m = v.match(/^(?:\$HOME|\$\{HOME\}|~)\/\.claude\/(.+)$/))) return join(REPO, m[1]);
  if ((m = v.match(/^(?:\$HOME|\$\{HOME\})\/(.+)$/))) return join(HOME, m[1]);
  if ((m = v.match(/^~\/(.+)$/))) return join(HOME, m[1]);
  if (v.startsWith('/')) return v;
  return null;
}

function checkSettingsEnv() {
  const sp = join(REPO, 'settings.json');
  if (!existsSync(sp)) { err('settings.json: file is missing'); return; }
  let settings;
  try { settings = JSON.parse(readFileSync(sp, 'utf8')); }
  catch (e) { err(`settings.json: invalid JSON (${e.message})`); return; }
  for (const [key, val] of Object.entries(settings.env ?? {})) {
    if (typeof val !== 'string') continue;
    const resolved = resolveEnvPath(val);
    if (resolved === null) continue;          // not a path — nothing to check
    if (existsSync(resolved)) continue;
    // Paths under the config dir go through the same source-vs-runtime
    // classification as every other reference. Paths elsewhere are
    // machine-specific (e.g. a sibling repo) — warn, don't fail.
    const under = val.trim().match(/^(?:\$HOME|\$\{HOME\}|~)\/\.claude\/(.+)$/);
    if (under) missing.push({ rel: under[1], source: `settings.json env.${key}` });
    else warn(`settings.json env.${key}: path does not resolve (machine-specific?) → ${val}`);
  }
}

// A count in prose is a DERIVED fact: true only until the directory it describes
// changes, and nothing about reading the sentence reveals it has gone stale. All
// three counts in README.md had drifted by the time this check was written
// (13/14 skills, 7/8 hooks, 16/18 bin tools). checkRefsInText proves a path
// RESOLVES; this proves a claim made ABOUT that path is still true — the half of
// the problem a path check cannot see.
//
// Each entry says how its directory is counted, because "how many hooks" and
// "how many files in hooks/" are different questions (test-*.sh are fixtures,
// not hooks). Keyed off the README table: the row whose first cell is `<dir>/`
// is the claim; the first integer in its second cell is the number claimed.
const COUNTED = [
  { dir: 'skills',       count: (e) => e.filter((x) => x.isDirectory()).length },
  { dir: 'agents',       count: (e) => e.filter((x) => x.name.endsWith('.md')).length },
  { dir: 'hooks',        count: (e) => e.filter((x) => !x.name.startsWith('test-')).length },
  { dir: 'bin',          count: (e) => e.filter((x) => !/^(?:_|test_)/.test(x.name)
                                                    && x.name !== 'README.md').length },
  { dir: 'systemd/user', count: (e) => e.length },
];

function checkCounts() {
  const readme = join(REPO, 'README.md');
  if (!existsSync(readme)) return;
  const rows = readFileSync(readme, 'utf8').split('\n').filter((l) => l.startsWith('|'));
  for (const { dir, count } of COUNTED) {
    const abs = join(REPO, dir);
    if (!existsSync(abs)) continue;
    // `${dir}/` with the closing backtick right after the slash, so the
    // `bin/README.md` row is not mistaken for a claim about `bin/`.
    const row = rows.find((l) => (l.split('|')[1] ?? '').includes(`\`${dir}/\``));
    if (!row) continue;                                  // undescribed → nothing claimed
    const claimed = (row.split('|')[2] ?? '').match(/\d+/);
    if (!claimed) continue;                              // described without a number
    const actual = count(readdirSync(abs, { withFileTypes: true }));
    if (Number(claimed[0]) !== actual) {
      err(`README.md: claims ${claimed[0]} for ${dir}/, found ${actual}`);
    }
  }
}

// Environment checks emit WARNINGS, never errors: a broken symlink or an
// uninstalled commit gate is a machine-setup issue, not broken committed
// content — surface it (loudly, via SessionStart) without blocking commits.
function checkSymlink() {
  let real;
  try { real = realpathSync(join(HOME, '.claude')); }
  catch { warn('~/.claude does not resolve (missing or dangling) — $HOME/.claude references depend on it'); return; }
  if (real !== REPO) warn(`~/.claude resolves to ${real}, not this repo (${REPO})`);
}

function checkCodexHooks() {
  const expected = join(REPO, 'codex', 'hooks.json');
  const installed = join(HOME, '.codex', 'hooks.json');
  let real;
  try { real = realpathSync(installed); }
  catch {
    warn('~/.codex/hooks.json does not resolve — run: ln -s ~/.claude/codex/hooks.json ~/.codex/hooks.json');
    return;
  }
  if (real !== expected) {
    warn(`~/.codex/hooks.json resolves to ${real}, not this repo (${expected})`);
  }
}

function checkCommitGate() {
  // Only meaningful for a normal .git directory; skip worktrees/submodules where .git is a file.
  let plain = false;
  try { plain = statSync(join(REPO, '.git')).isDirectory(); } catch { return; }
  if (plain && !existsSync(join(REPO, '.git/hooks/pre-commit'))) {
    warn('pre-commit gate not installed — run: ln -sf ../../hooks/pre-commit "$(git rev-parse --git-dir)/hooks/pre-commit"');
  }
}

// --- run --------------------------------------------------------------------
for (const d of SCAN_DIRS) {
  const dir = join(REPO, d);
  if (!existsSync(dir)) continue;
  for (const f of walk(dir)) {
    if (!SCAN_EXTS.has(f.slice(f.lastIndexOf('.')))) continue;
    checkRefsInText(readFileSync(f, 'utf8'), relToRepo(f));
  }
}
const settingsPath = join(REPO, 'settings.json');
if (existsSync(settingsPath)) checkRefsInText(readFileSync(settingsPath, 'utf8'), 'settings.json');
checkSettingsEnv();
classifyMissing();
checkCounts();
checkSymlink();
checkCodexHooks();
checkCommitGate();

const uniqErrors = [...new Set(errors)];
const uniqWarnings = [...new Set(warnings)];

if (uniqErrors.length === 0 && uniqWarnings.length === 0) process.exit(0); // clean → silent

const out = ['claude-config doctor:'];
if (uniqErrors.length) {
  out.push(`\n  ✖ ${uniqErrors.length} error(s):`);
  for (const e of uniqErrors) out.push(`    - ${e}`);
}
if (uniqWarnings.length) {
  out.push(`\n  ⚠ ${uniqWarnings.length} warning(s):`);
  for (const w of uniqWarnings) out.push(`    - ${w}`);
}
process.stdout.write(out.join('\n') + '\n');
process.exit(uniqErrors.length ? 1 : 0);
