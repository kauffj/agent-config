#!/usr/bin/env node
// Project profile detector + cache.
//
// CLI:
//   node lib/project.mjs load       # load cached or detect + cache
//   node lib/project.mjs detect     # re-detect and overwrite cache
//   node lib/project.mjs show       # print cache (error if none)
//
// Cache: .claude/project.json (relative to CWD).

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { basename } from 'node:path';

const CACHE = '.claude/project.json';

function sh(cmd) {
  try {
    return execSync(cmd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return '';
  }
}

function exists(p) { return existsSync(p); }

function detectPkgMgr() {
  if (exists('bun.lockb') || exists('bun.lock')) {
    return { pkgMgr: 'bun', installCmd: 'bun install', buildCmd: 'bun run build', devCmd: 'bun run dev' };
  }
  if (exists('pnpm-lock.yaml')) {
    return { pkgMgr: 'pnpm', installCmd: 'pnpm install', buildCmd: 'pnpm run build', devCmd: 'pnpm run dev' };
  }
  if (exists('yarn.lock')) {
    return { pkgMgr: 'yarn', installCmd: 'yarn install', buildCmd: 'yarn build', devCmd: 'yarn dev' };
  }
  if (exists('package-lock.json') || exists('package.json')) {
    return { pkgMgr: 'npm', installCmd: 'npm install', buildCmd: 'npm run build', devCmd: 'npm run dev' };
  }
  if (exists('Cargo.toml')) {
    return { pkgMgr: 'cargo', installCmd: 'cargo build', buildCmd: 'cargo build --release', devCmd: 'cargo run' };
  }
  if (exists('go.mod')) {
    return { pkgMgr: 'go', installCmd: 'go mod download', buildCmd: 'go build ./...', devCmd: 'go run .' };
  }
  if (exists('requirements.txt') || exists('pyproject.toml')) {
    return { pkgMgr: 'pip', installCmd: 'pip install -r requirements.txt', buildCmd: "echo 'no build step'", devCmd: 'python manage.py runserver' };
  }
  return { pkgMgr: 'unknown', installCmd: "echo 'no install step'", buildCmd: "echo 'no build step'", devCmd: "echo 'no dev command'" };
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
  if (exists('vercel.json')) return 'Vercel: auto-deploys on push to default branch';
  if (exists('netlify.toml')) return 'Netlify: auto-deploys on push to default branch';
  if (exists('fly.toml')) return 'Fly.io: deploy via `fly deploy` or CI';
  if (exists('render.yaml')) return 'Render: auto-deploys on push';
  if (exists('Procfile')) return 'Procfile-based (Heroku-style): push to deploy';
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

export function detect() {
  const repoName = sh('git rev-parse --show-toplevel') ? basename(sh('git rev-parse --show-toplevel')) : basename(process.cwd());
  const defaultBranch = sh('git symbolic-ref refs/remotes/origin/HEAD').replace(/^refs\/remotes\/origin\//, '') || 'main';
  const { pkgMgr, installCmd, buildCmd, devCmd } = detectPkgMgr();
  const stack = detectStack();
  const deployModel = detectDeployModel();
  const hasScreenshots = exists('scripts/screenshot.ts') || exists('scripts/screenshot.js');
  const envFile = detectEnvFile();
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
    detectedAt: new Date().toISOString(),
  };
}

function ensureDir() {
  if (!existsSync('.claude')) mkdirSync('.claude', { recursive: true });
}

export function load() {
  if (existsSync(CACHE)) {
    return JSON.parse(readFileSync(CACHE, 'utf8'));
  }
  const profile = detect();
  ensureDir();
  writeFileSync(CACHE, JSON.stringify(profile, null, 2));
  return profile;
}

export function redetect() {
  const profile = detect();
  ensureDir();
  writeFileSync(CACHE, JSON.stringify(profile, null, 2));
  return profile;
}

// CLI
if (import.meta.url === `file://${process.argv[1]}`) {
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
