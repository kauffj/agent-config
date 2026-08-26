#!/usr/bin/env node
// Workspace state store. Invoke as CLI or import as module.
//
// CLI:
//   node lib/workspace.mjs list [--kind <kind>]
//   node lib/workspace.mjs get <name>
//   node lib/workspace.mjs create <json-record>
//   node lib/workspace.mjs update <name> <json-patch>
//   node lib/workspace.mjs remove <name>
//   node lib/workspace.mjs rewrite-env-db <envPath> <dbName> <var...>
//   node lib/workspace.mjs port <worktreePath>
//   node lib/workspace.mjs migrate
//
// State file: .workspaces/workspaces.json (relative to repo root CWD).
// On first access:
//  - relocates legacy .claude/{workspaces.json,project.json,plans/} to .workspaces/
//  - transforms legacy .claude/features.json (old format) to .workspaces/workspaces.json

import { readFileSync, writeFileSync, existsSync, renameSync, mkdirSync, readdirSync, rmdirSync, realpathSync, openSync, writeSync, fsyncSync, closeSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { createServer } from 'node:net';

const STATE = '.workspaces/workspaces.json';
const LEGACY = '.claude/features.json';
const PROJECT = '.workspaces/project.json';
const PLANS_DIR = '.workspaces/plans';

const STATUS_MAP = {
  planning: 'active',
  implementing: 'active',
  reviewing: 'active',
  'manual-review': 'active',
  pushed: 'active',
  'pr-open': 'active',
  complete: 'done',
  abandoned: 'abandoned',
  active: 'active',
  done: 'done',
};

function ensureDir() {
  if (!existsSync('.workspaces')) mkdirSync('.workspaces', { recursive: true });
}

function relocateFromClaudeDir() {
  const moved = [];
  if (existsSync('.claude/workspaces.json') && !existsSync(STATE)) {
    ensureDir();
    renameSync('.claude/workspaces.json', STATE);
    moved.push('workspaces.json');
  }
  if (existsSync('.claude/project.json') && !existsSync(PROJECT)) {
    ensureDir();
    renameSync('.claude/project.json', PROJECT);
    moved.push('project.json');
  }
  if (existsSync('.claude/plans')) {
    if (!existsSync(PLANS_DIR)) mkdirSync(PLANS_DIR, { recursive: true });
    for (const f of readdirSync('.claude/plans')) {
      const src = `.claude/plans/${f}`;
      const dst = `${PLANS_DIR}/${f}`;
      if (!existsSync(dst)) {
        renameSync(src, dst);
        moved.push(`plans/${f}`);
      }
    }
    try { rmdirSync('.claude/plans'); } catch { /* not empty, leave it */ }
  }
  return moved;
}

export function migrate() {
  const relocated = relocateFromClaudeDir();
  ensureDir();
  const withRelocated = (r) => relocated.length ? { ...r, relocated } : r;
  if (existsSync(STATE)) return withRelocated({ migrated: false, reason: 'workspaces.json already exists' });
  if (!existsSync(LEGACY)) return withRelocated({ migrated: false, reason: 'no legacy file' });

  let raw;
  try {
    raw = JSON.parse(readFileSync(LEGACY, 'utf8'));
  } catch (e) {
    return withRelocated({ migrated: false, reason: `legacy file unparseable: ${e.message}` });
  }

  // Normalize old flat-array form
  let legacy;
  if (Array.isArray(raw)) {
    legacy = { project: null, features: raw };
  } else {
    legacy = raw;
  }

  // Split project out
  if (legacy.project && !existsSync(PROJECT)) {
    writeFileSync(PROJECT, JSON.stringify(legacy.project, null, 2));
  }

  // Transform features -> workspaces
  const workspaces = (legacy.features || []).map((f) => ({
    name: f.name,
    kind: 'feature',
    description: f.description ?? '',
    branch: f.branch ?? null,
    worktreePath: f.worktreePath ?? null,
    port: f.port ?? null,
    envFile: legacy.project?.envFile ?? null,
    screenshotDir: f.screenshotDir ?? null,
    status: STATUS_MAP[f.status] ?? 'active',
    pipeline: {
      skill: 'feature',
      step: f.step ?? null,
      plan: f.plan ?? null,
      legacyStatus: f.status ?? null,
    },
    createdAt: f.createdAt ?? new Date().toISOString(),
    updatedAt: f.updatedAt ?? new Date().toISOString(),
  }));

  writeFileSync(STATE, JSON.stringify({ workspaces }, null, 2));

  const stamp = new Date().toISOString().slice(0, 10);
  const backup = `${LEGACY}.migrated-${stamp}`;
  renameSync(LEGACY, backup);

  return withRelocated({ migrated: true, backup, count: workspaces.length });
}

function readState() {
  migrate(); // idempotent
  ensureDir();
  if (!existsSync(STATE)) {
    writeFileSync(STATE, JSON.stringify({ workspaces: [] }, null, 2));
  }
  const data = JSON.parse(readFileSync(STATE, 'utf8'));
  // Records written before teardown derived its own connection still hold a
  // password-bearing dbAdminUrl. Drop it the first time such a file is read,
  // so the credential leaves disk without anyone having to go looking for it.
  const stale = (data.workspaces ?? []).filter((w) => 'dbAdminUrl' in w);
  if (stale.length) {
    for (const w of stale) delete w.dbAdminUrl;
    writeState(data);
  }
  return data;
}

function writeState(data) {
  ensureDir();
  // Atomic write: write to a temp file, fsync, then rename over STATE so a
  // crash/power-loss mid-write can never truncate or corrupt the state file.
  const tmp = STATE + '.tmp';
  const fd = openSync(tmp, 'w');
  try {
    writeSync(fd, JSON.stringify(data, null, 2));
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, STATE);
}

export function list({ kind } = {}) {
  const { workspaces } = readState();
  return kind ? workspaces.filter((w) => w.kind === kind) : workspaces;
}

export function get(name) {
  const { workspaces } = readState();
  return workspaces.find((w) => w.name === name) ?? null;
}

// --- port allocation ---------------------------------------------------------
// A workspace's dev-server port is DERIVED from its worktree path, not found by
// scanning upward from 3000. Two reasons, and the second is the defect that
// motivated the change:
//
//  1. Deterministic. The same worktree yields the same port on every machine
//     and after every reboot, so the port baked into .env can be re-derived
//     instead of looked up, and two projects' workspaces do not pile onto the
//     same low numbers.
//  2. The upward scan asked `ss` what was LISTENING. A workspace whose dev
//     server is stopped still owns its port — in its .env and in this record —
//     and `ss` cannot see that. So the scan handed out a port already written
//     into another workspace's env, and the collision surfaced later, as a
//     bind failure when both servers ran. Claimed ports now come from the state
//     file; liveness is a second, independent check.
//
// Hashing the PROJECT alone would give every workspace in it the same port, so
// the key is the worktree path, which is unique per workspace by construction.
//
// The window sits above the registered-port clutter and below Linux's ephemeral
// range (net.ipv4.ip_local_port_range, default 32768) — allocating inside that
// range means racing the kernel for the same number.
const PORT_MIN = 20000;
const PORT_MAX = 29999;
const PORT_SPAN = PORT_MAX - PORT_MIN + 1;

export function portFor(key) {
  const digest = createHash('sha256').update(key).digest();
  return PORT_MIN + (digest.readUInt32BE(0) % PORT_SPAN);
}

// Authoritative liveness check. Grepping `ss` misses a port held on one
// interface, held by another user, or held by a socket it did not parse — and
// it fails in the flattering direction, reporting free. Binding is the same
// operation the dev server will perform, so it answers the actual question.
function bindable(port) {
  return new Promise((resolve) => {
    const srv = createServer();
    srv.once('error', () => resolve(false));
    srv.once('listening', () => srv.close(() => resolve(true)));
    srv.listen(port, '0.0.0.0');
  });
}

// The hash picks the starting point; the walk resolves the rare hash collision
// (~2% at 20 workspaces over a 10k window). Both filters are load-bearing:
// `claimed` catches a workspace that exists but is not running, `bindable`
// catches everything else on the box.
export async function allocatePort(worktreePath, { exclude = [] } = {}) {
  if (!worktreePath) throw new Error('allocatePort: worktreePath is required');
  const claimed = new Set([
    ...readState().workspaces.map((w) => w.port).filter((p) => Number.isInteger(p)),
    ...exclude,
  ]);
  const start = portFor(worktreePath);
  for (let i = 0; i < PORT_SPAN; i++) {
    const port = PORT_MIN + ((start - PORT_MIN + i) % PORT_SPAN);
    if (claimed.has(port)) continue;
    if (await bindable(port)) return port;
  }
  throw new Error(`no free port in ${PORT_MIN}-${PORT_MAX}`);
}

export function create(record) {
  const data = readState();
  if (data.workspaces.some((w) => w.name === record.name)) {
    throw new Error(`workspace '${record.name}' already exists`);
  }
  const now = new Date().toISOString();
  const full = {
    name: record.name,
    kind: record.kind ?? 'feature',
    description: record.description ?? '',
    branch: record.branch ?? null,
    worktreePath: record.worktreePath ?? null,
    port: record.port ?? null,
    envFile: record.envFile ?? null,
    screenshotDir: record.screenshotDir ?? null,
    status: record.status ?? 'active',
    dbName: record.dbName ?? null,
    dbIsolation: record.dbIsolation ?? null,
    // No dbAdminUrl. A maintenance connection string carries the dev password,
    // and this record is printed verbatim by `/workspace get` — into the
    // transcript, and into anything that exports one. Teardown re-derives it
    // from the main checkout's env instead; the record holds identity, not
    // credentials.
    pipeline: record.pipeline ?? {},
    createdAt: now,
    updatedAt: now,
  };
  data.workspaces.push(full);
  writeState(data);
  return full;
}

export function update(name, patch) {
  const data = readState();
  const i = data.workspaces.findIndex((w) => w.name === name);
  if (i === -1) throw new Error(`workspace '${name}' not found`);
  // Shallow merge top-level, shallow merge pipeline sub-object
  const current = data.workspaces[i];
  const merged = { ...current, ...patch, updatedAt: new Date().toISOString() };
  if (patch.pipeline) {
    merged.pipeline = { ...(current.pipeline ?? {}), ...patch.pipeline };
  }
  data.workspaces[i] = merged;
  writeState(data);
  return merged;
}

// Rewrite the database name in connection-URL env vars, in place.
// For each var, parse its value as a URL and swap ONLY the last path segment
// (the DB name), preserving user/password/host/port/query. Surrounding quotes
// are preserved. Vars that are absent or don't parse as URLs are left untouched.
export function rewriteEnvDb(envPath, dbName, vars) {
  let text = readFileSync(envPath, 'utf8');
  for (const v of vars) {
    const re = new RegExp(`^(\\s*${v}\\s*=\\s*)(.*)$`, 'm');
    text = text.replace(re, (line, prefix, rawVal) => {
      let val = rawVal.trim();
      let quote = '';
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        quote = val[0];
        val = val.slice(1, -1);
      }
      let url;
      try {
        url = new URL(val);
      } catch {
        return line; // not a URL — leave as-is
      }
      url.pathname = '/' + encodeURIComponent(dbName);
      return `${prefix}${quote}${url.href}${quote}`;
    });
  }
  writeFileSync(envPath, text);
  return { envPath, dbName, vars };
}

export function remove(name) {
  const data = readState();
  const i = data.workspaces.findIndex((w) => w.name === name);
  if (i === -1) throw new Error(`workspace '${name}' not found`);
  const [removed] = data.workspaces.splice(i, 1);
  writeState(data);
  return removed;
}

// CLI
if (import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const [, , cmd, ...args] = process.argv;
  try {
    let result;
    switch (cmd) {
      case 'list': {
        const kindIdx = args.indexOf('--kind');
        const kind = kindIdx >= 0 ? args[kindIdx + 1] : undefined;
        result = list({ kind });
        break;
      }
      case 'get':
        result = get(args[0]);
        if (!result) { process.exitCode = 1; result = { error: `not found: ${args[0]}` }; }
        break;
      case 'create':
        result = create(JSON.parse(args[0]));
        break;
      case 'update':
        result = update(args[0], JSON.parse(args[1]));
        break;
      case 'remove':
        result = remove(args[0]);
        break;
      case 'port':
        result = { port: await allocatePort(args[0]) };
        break;
      case 'rewrite-env-db':
        result = rewriteEnvDb(args[0], args[1], args.slice(2));
        break;
      case 'migrate':
        result = migrate();
        break;
      default:
        process.stderr.write(`unknown command: ${cmd}\n`);
        process.stderr.write(`usage: node lib/workspace.mjs <list|get|create|update|remove|port|rewrite-env-db|migrate> [...]\n`);
        process.exit(2);
    }
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  } catch (e) {
    process.stderr.write(`error: ${e.message}\n`);
    process.exit(1);
  }
}
