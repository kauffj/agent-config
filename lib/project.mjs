#!/usr/bin/env node
// Project profile detector + cache.
//
// CLI:
//   node lib/project.mjs load       # load cached or detect + cache
//   node lib/project.mjs detect     # re-detect and overwrite cache
//   node lib/project.mjs show       # print cache (error if none)
//
// Cache: .workspaces/project.json (relative to CWD).

import { readFileSync, writeFileSync, existsSync, statSync, mkdirSync, renameSync, realpathSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { basename } from 'node:path';
import { pathToFileURL } from 'node:url';

const CACHE = '.workspaces/project.json';

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
  if (exists('package-lock.json') || exists('package.json')) return js('npm', 'npm install', 'npm run');
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
  const primary = env.DATABASE_URL && /^postgres(ql)?:\/\//.test(env.DATABASE_URL)
    ? env.DATABASE_URL
    : dbUrlVars.length ? env[dbUrlVars[0]] : null;
  if (primary) {
    try {
      const u = new URL(primary);
      dbTemplate = decodeURIComponent(u.pathname.replace(/^\//, '')) || null;
      localHost = isLocalHost(u.hostname);
    } catch { /* unparseable; leave defaults */ }
  }

  const dbIsolation = dbKind === 'postgres' && localHost && dbTemplate ? 'template' : 'none';
  return { dbKind, dbUrlVars, dbTemplate, dbIsolation };
}

export function detect() {
  const repoName = sh('git rev-parse --show-toplevel') ? basename(sh('git rev-parse --show-toplevel')) : basename(process.cwd());
  const defaultBranch = sh('git symbolic-ref refs/remotes/origin/HEAD').replace(/^refs\/remotes\/origin\//, '') || 'main';
  const { pkgMgr, installCmd, buildCmd, devCmd } = detectPkgMgr();
  const stack = detectStack();
  const deployModel = detectDeployModel();
  const hasScreenshots = exists('scripts/screenshot.ts') || exists('scripts/screenshot.js');
  const envFile = detectEnvFile();
  const { dbKind, dbUrlVars, dbTemplate, dbIsolation } = detectDb(envFile);
  return {
    repoName,
    defaultBranch,
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
    dbTemplate,
    dbIsolation,
    detectedAt: new Date().toISOString(),
    inputs: fingerprint(),
  };
}

function ensureDir() {
  if (!existsSync('.workspaces')) mkdirSync('.workspaces', { recursive: true });
}

function relocateFromClaudeDir() {
  if (existsSync('.claude/project.json') && !existsSync(CACHE)) {
    ensureDir();
    renameSync('.claude/project.json', CACHE);
  }
}

// Detection is a guess; `overrides` is the repo's answer. Anything set there
// wins over anything detected, and survives re-detection — so a project whose
// dev command this file cannot infer is corrected once, in
// .workspaces/project.json, instead of every time it is read:
//   { "overrides": { "devCmd": "make serve" }, ... }
function applyOverrides(profile) {
  const overrides = profile.overrides ?? {};
  return { ...profile, ...overrides, overrides };
}

function write(profile, overrides) {
  ensureDir();
  const stored = overrides && Object.keys(overrides).length
    ? { ...profile, overrides }
    : profile;
  writeFileSync(CACHE, JSON.stringify(stored, null, 2));
  return applyOverrides(stored);
}

export function load() {
  relocateFromClaudeDir();
  if (existsSync(CACHE)) {
    const cached = JSON.parse(readFileSync(CACHE, 'utf8'));
    // A profile is only valid for the tree it was detected from.
    if (sameInputs(cached.inputs, fingerprint())) return applyOverrides(cached);
    return write(detect(), cached.overrides);
  }
  return write(detect(), null);
}

export function redetect() {
  const prior = existsSync(CACHE) ? JSON.parse(readFileSync(CACHE, 'utf8')) : {};
  return write(detect(), prior.overrides);
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
        if (!existsSync(CACHE)) { process.stderr.write('no cached profile\n'); process.exit(1); }
        result = JSON.parse(readFileSync(CACHE, 'utf8'));
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
