#!/usr/bin/env node
// Project profile detector + cache.
//
// CLI:
//   node lib/project.mjs load       # load cached or detect + cache
//   node lib/project.mjs detect     # re-detect and overwrite cache
//   node lib/project.mjs show       # print effective cached profile (error if none)
//
// Cache: .workspaces/project.json (relative to CWD).
// Durable overrides: .agent/project.json (tracked, relative to CWD).

import { readFileSync, existsSync, statSync, realpathSync } from 'node:fs';
import { execSync, spawnSync } from 'node:child_process';
import { basename, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  assertRegularFile,
  atomicWriteFile,
  ensureRealDirectory,
  moveRegularFile,
  readRegularFile,
} from './safe-runtime-files.mjs';

const CACHE = '.workspaces/project.json';
const PROJECT_CONFIG = '.agent/project.json';
const CONFIGURABLE_FIELDS = new Set([
  'pkgMgr', 'installCmd', 'buildCmd', 'devCmd', 'stack', 'deployModel',
  'hasScreenshots', 'envFile', 'dbKind', 'dbUrlVars', 'dbPrimaryUrlVar',
  'dbTemplate', 'dbIsolation',
]);

// Files whose presence or content decides what detect() returns. The cache
// records their size+mtime so `load()` can tell a still-valid profile from one
// describing a project that has since changed package manager, gained a build
// script, or switched database. Without this the first detection was permanent.
const SENTINELS = [
  'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
  'bun.lockb', 'bun.lock', 'Cargo.toml', 'go.mod', 'requirements.txt',
  'pyproject.toml', 'manage.py', 'vercel.json', 'netlify.toml', 'fly.toml',
  'render.yaml', 'Procfile', 'Makefile', '.github/workflows',
  '.env.local', '.env.development', '.env',
  'scripts/screenshot.ts', 'scripts/screenshot.js',
  PROJECT_CONFIG,
];

// Absent files are omitted rather than recorded as null, so one appearing
// changes the fingerprint just as surely as one changing.
function fingerprint() {
  const fp = {};
  for (const f of SENTINELS) {
    try {
      const s = statSync(f);
      fp[f] = `${s.size}:${Math.floor(s.mtimeMs)}`;
    } catch { /* absent */ }
  }
  return fp;
}

const sameInputs = (a, b) => JSON.stringify(a ?? {}) === JSON.stringify(b ?? {});

function sh(cmd) {
  try {
    return execSync(cmd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return '';
  }
}

function exists(p) { return existsSync(p); }

function pkgScripts() {
  if (!exists('package.json')) return {};
  try {
    return JSON.parse(readFileSync('package.json', 'utf8')).scripts ?? {};
  } catch {
    return {};
  }
}

// A command is only reported if the thing it invokes actually exists. Every
// field here used to be filled in unconditionally, so a project with no `build`
// script still advertised `npm run build`, and any Python project — Django or
// not — was told to run `python manage.py runserver`. Callers ran those
// verbatim. `null` means "this project has no such step": an honest unknown a
// caller can skip, where an invented command is a failure at the worst moment.
function detectPkgMgr() {
  const js = (mgr, install, runner) => {
    const scripts = pkgScripts();
    return {
      pkgMgr: mgr,
      installCmd: install,
      buildCmd: scripts.build ? `${runner} build` : null,
      devCmd: scripts.dev ? `${runner} dev` : (scripts.start ? `${runner} start` : null),
    };
  };
  if (exists('bun.lockb') || exists('bun.lock')) return js('bun', 'bun install', 'bun run');
  if (exists('pnpm-lock.yaml')) return js('pnpm', 'pnpm install', 'pnpm run');
  if (exists('yarn.lock')) return js('yarn', 'yarn install', 'yarn');
  if (exists('package-lock.json')) return js('npm', 'npm ci', 'npm run');
  if (exists('package.json')) return js('npm', 'npm install', 'npm run');
  if (exists('Cargo.toml')) {
    return { pkgMgr: 'cargo', installCmd: 'cargo build', buildCmd: 'cargo build --release', devCmd: 'cargo run' };
  }
  if (exists('go.mod')) {
    return { pkgMgr: 'go', installCmd: 'go mod download', buildCmd: 'go build ./...', devCmd: 'go run .' };
  }
  if (exists('requirements.txt') || exists('pyproject.toml')) {
    // requirements.txt and pyproject.toml are installed differently, and only
    // Django has a runserver command to offer.
    return {
      pkgMgr: 'pip',
      installCmd: exists('requirements.txt') ? 'pip install -r requirements.txt' : 'pip install -e .',
      buildCmd: null,
      devCmd: exists('manage.py') ? 'python manage.py runserver' : null,
    };
  }
  return { pkgMgr: 'unknown', installCmd: null, buildCmd: null, devCmd: null };
}

function detectStack() {
  const bits = [];
  if (exists('package.json')) {
    try {
      const pkg = JSON.parse(readFileSync('package.json', 'utf8'));
      const deps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
      if (deps.next) bits.push(`Next.js ${deps.next.replace(/[\^~]/, '').split('.')[0]}`);
      else if (deps.nuxt) bits.push(`Nuxt ${deps.nuxt.replace(/[\^~]/, '').split('.')[0]}`);
      else if (deps.remix || deps['@remix-run/node']) bits.push('Remix');
      else if (deps.vite) bits.push('Vite');
      if (deps['drizzle-orm']) bits.push('Drizzle ORM');
      else if (deps.prisma || deps['@prisma/client']) bits.push('Prisma');
      else if (deps.knex) bits.push('Knex');
      if (deps.pg || deps.postgres) bits.push('PostgreSQL');
      else if (deps.mysql || deps.mysql2) bits.push('MySQL');
      else if (deps['better-sqlite3'] || deps.sqlite3) bits.push('SQLite');
      if (deps.tailwindcss) bits.push('Tailwind CSS');
      if (deps['next-auth']) bits.push('NextAuth');
      else if (deps['@auth/core']) bits.push('Auth.js');
      else if (deps.clerk || deps['@clerk/nextjs']) bits.push('Clerk');
    } catch { /* ignore */ }
  }
  if (exists('Cargo.toml')) bits.push('Rust');
  if (exists('go.mod')) bits.push('Go');
  if (exists('pyproject.toml') || exists('requirements.txt')) bits.push('Python');
  return bits.length ? bits.join(', ') : 'unknown';
}

function detectDeployModel() {
  if (exists('.github/workflows')) {
    // Look for a workflow that looks like deploy
    try {
      const list = sh('ls .github/workflows').split(/\s+/).filter(Boolean);
      const deploy = list.find((f) => /deploy|release|publish/i.test(f));
      if (deploy) return `GitHub Actions: .github/workflows/${deploy}`;
    } catch { /* ignore */ }
  }
  // Config-file presence names the PLATFORM; it does not prove when or whether
  // that platform deploys. Say what was found, not what it supposedly does —
  // the deploy trigger belongs in the repo's own CLAUDE.md, where a human
  // asserts it.
  if (exists('vercel.json')) return 'Vercel (vercel.json present) — deploy trigger unverified';
  if (exists('netlify.toml')) return 'Netlify (netlify.toml present) — deploy trigger unverified';
  if (exists('fly.toml')) return 'Fly.io (fly.toml present) — deploy via `fly deploy` or CI';
  if (exists('render.yaml')) return 'Render (render.yaml present) — deploy trigger unverified';
  if (exists('Procfile')) return 'Procfile present (Heroku-style) — deploy trigger unverified';
  if (exists('Makefile')) {
    const mk = readFileSync('Makefile', 'utf8');
    if (/^deploy:/m.test(mk)) return 'Makefile: `make deploy` target';
  }
  return 'unknown';
}

function detectEnvFile() {
  for (const f of ['.env.local', '.env.development', '.env']) {
    if (exists(f)) return f;
  }
  return '';
}

// Parse `KEY=value` lines from an env file into a plain object.
// Strips surrounding single/double quotes; ignores comments and blanks.
function parseEnv(file) {
  const out = {};
  if (!file || !exists(file)) return out;
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (!m) continue;
    let val = m[2].trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    out[m[1]] = val;
  }
  return out;
}

function isLocalHost(h) {
  return h === '' || h === 'localhost' || h === '127.0.0.1' || h === '::1';
}

// Detect Postgres DB isolation config from package.json deps + the env file.
// Isolation is only offered for Postgres apps whose dev DB is local-cloneable.
function detectDb(envFile) {
  let dbKind = null;
  if (exists('package.json')) {
    try {
      const pkg = JSON.parse(readFileSync('package.json', 'utf8'));
      const deps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
      if (deps.pg || deps.postgres) dbKind = 'postgres';
    } catch { /* ignore */ }
  }

  const env = parseEnv(envFile);
  // Env vars whose value is a Postgres connection URL (e.g. DATABASE_URL, DIRECT_URL).
  // SHADOW_DATABASE_URL is intentionally excluded — it's a throwaway migrate DB.
  const dbUrlVars = Object.keys(env).filter(
    (k) => k !== 'SHADOW_DATABASE_URL' && /^postgres(ql)?:\/\//.test(env[k]),
  );

  let dbTemplate = null;
  let localHost = false;
  const dbPrimaryUrlVar = dbUrlVars.includes('DATABASE_URL')
    ? 'DATABASE_URL'
    : dbUrlVars[0] ?? null;
  const primary = dbPrimaryUrlVar ? env[dbPrimaryUrlVar] : null;
  if (primary) {
    try {
      const u = new URL(primary);
      dbTemplate = decodeURIComponent(u.pathname.replace(/^\//, '')) || null;
      localHost = isLocalHost(u.hostname);
    } catch { /* unparseable; leave defaults */ }
  }

  const dbIsolation = dbKind === 'postgres' && localHost && dbTemplate ? 'template' : 'none';
  return { dbKind, dbUrlVars, dbPrimaryUrlVar, dbTemplate, dbIsolation };
}

export function detect() {
  const commonDir = sh('git rev-parse --path-format=absolute --git-common-dir');
  const topLevel = sh('git rev-parse --show-toplevel');
  const repoName = commonDir && basename(commonDir) === '.git'
    ? basename(dirname(commonDir))
    : topLevel ? basename(topLevel) : basename(process.cwd());
  const { pkgMgr, installCmd, buildCmd, devCmd } = detectPkgMgr();
  const stack = detectStack();
  const deployModel = detectDeployModel();
  const hasScreenshots = exists('scripts/screenshot.ts') || exists('scripts/screenshot.js');
  const envFile = detectEnvFile();
  const { dbKind, dbUrlVars, dbPrimaryUrlVar, dbTemplate, dbIsolation } = detectDb(envFile);
  return {
    repoName,
    pkgMgr,
    installCmd,
    buildCmd,
    devCmd,
    stack,
    deployModel,
    hasScreenshots,
    envFile,
    dbKind,
    dbUrlVars,
    dbPrimaryUrlVar,
    dbTemplate,
    dbIsolation,
    detectedAt: new Date().toISOString(),
    inputs: fingerprint(),
  };
}

function ensureDir() {
  ensureRealDirectory('.workspaces', { create: true });
}

function gitSucceeds(args) {
  return spawnSync('git', args, { stdio: 'ignore' }).status === 0;
}

// This file is runtime state. Rewriting a tracked or visible cache would dirty
// the user's checkout merely by inspecting the project, so fail before touching
// it. Workflow entry points establish the repository-local exclusion first.
function assertCacheWritable() {
  ensureDir();
  assertRegularFile(CACHE, { required: false });
  if (!gitSucceeds(['rev-parse', '--is-inside-work-tree'])) return;
  if (gitSucceeds(['ls-files', '--error-unmatch', '--', CACHE])) {
    throw new Error(`${CACHE} is tracked; untrack it before using it as runtime cache`);
  }
  if (!gitSucceeds(['check-ignore', '--quiet', '--no-index', '--', CACHE])) {
    throw new Error(`${CACHE} is not ignored; exclude .workspaces/ before loading the project profile`);
  }
}

function relocateFromClaudeDir() {
  const hasLegacy = assertRegularFile('.claude/project.json', { required: false });
  if (hasLegacy && !assertRegularFile(CACHE, { required: false })) {
    assertCacheWritable();
    moveRegularFile('.claude/project.json', CACHE);
  }
}

// The cache is derived runtime state. Human corrections live separately in the
// tracked, client-neutral .agent/project.json file and are applied only in
// memory. This keeps deleting .workspaces/ safe and makes corrections portable
// to every checkout and agent harness.
function projectConfig() {
  if (!assertRegularFile(PROJECT_CONFIG, { required: false })) return {};
  if (gitSucceeds(['rev-parse', '--is-inside-work-tree'])
      && !gitSucceeds(['ls-files', '--error-unmatch', '--', PROJECT_CONFIG])) {
    throw new Error(`${PROJECT_CONFIG} must be tracked so project overrides are durable`);
  }
  let config;
  try {
    config = JSON.parse(readRegularFile(PROJECT_CONFIG));
  } catch (error) {
    throw new Error(`${PROJECT_CONFIG} is invalid JSON: ${error.message}`);
  }
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error(`${PROJECT_CONFIG} must contain one JSON object`);
  }
  const unknown = Object.keys(config).filter((key) => !CONFIGURABLE_FIELDS.has(key));
  if (unknown.length) {
    throw new Error(`${PROJECT_CONFIG} has unsupported field${unknown.length === 1 ? '' : 's'}: ${unknown.join(', ')}`);
  }
  return config;
}

function effectiveProfile(profile) {
  if (Object.hasOwn(profile, 'overrides')) {
    throw new Error(`legacy overrides in ${CACHE} must move to tracked ${PROJECT_CONFIG}`);
  }
  const merged = { ...profile, ...projectConfig() };
  // Cached profiles created before dbPrimaryUrlVar existed still have enough
  // plain data to recover the same deterministic choice without re-detection.
  if (!merged.dbPrimaryUrlVar) {
    merged.dbPrimaryUrlVar = merged.dbUrlVars?.includes('DATABASE_URL')
      ? 'DATABASE_URL'
      : merged.dbUrlVars?.[0] ?? null;
  }
  return merged;
}

function write(profile) {
  assertCacheWritable();
  atomicWriteFile(CACHE, JSON.stringify(profile, null, 2));
  return effectiveProfile(profile);
}

export function load() {
  relocateFromClaudeDir();
  if (assertRegularFile(CACHE, { required: false })) {
    const cached = JSON.parse(readRegularFile(CACHE));
    // A profile is only valid for the tree it was detected from.
    if (sameInputs(cached.inputs, fingerprint())) return effectiveProfile(cached);
    return write(detect());
  }
  return write(detect());
}

export function redetect() {
  if (assertRegularFile(CACHE, { required: false })) {
    const prior = JSON.parse(readRegularFile(CACHE));
    if (Object.hasOwn(prior, 'overrides')) {
      throw new Error(`legacy overrides in ${CACHE} must move to tracked ${PROJECT_CONFIG}`);
    }
  }
  return write(detect());
}

// CLI
if (import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const [, , cmd] = process.argv;
  try {
    let result;
    switch (cmd) {
      case 'load':
        result = load();
        break;
      case 'detect':
        result = redetect();
        break;
      case 'show':
        if (!assertRegularFile(CACHE, { required: false })) { process.stderr.write('no cached profile\n'); process.exit(1); }
        result = effectiveProfile(JSON.parse(readRegularFile(CACHE)));
        break;
      default:
        process.stderr.write('usage: node lib/project.mjs <load|detect|show>\n');
        process.exit(2);
    }
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  } catch (e) {
    process.stderr.write(`error: ${e.message}\n`);
    process.exit(1);
  }
}
