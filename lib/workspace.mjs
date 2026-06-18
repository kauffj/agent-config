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
//   node lib/workspace.mjs migrate
//
// State file: .workspaces/workspaces.json (relative to repo root CWD).
// On first access:
//  - relocates legacy .claude/{workspaces.json,project.json,plans/} to .workspaces/
//  - transforms legacy .claude/features.json (old format) to .workspaces/workspaces.json

import { readFileSync, writeFileSync, existsSync, renameSync, mkdirSync, readdirSync, rmdirSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

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
  return JSON.parse(readFileSync(STATE, 'utf8'));
}

function writeState(data) {
  ensureDir();
  writeFileSync(STATE, JSON.stringify(data, null, 2));
}

export function list({ kind } = {}) {
  const { workspaces } = readState();
  return kind ? workspaces.filter((w) => w.kind === kind) : workspaces;
}

export function get(name) {
  const { workspaces } = readState();
  return workspaces.find((w) => w.name === name) ?? null;
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
    dbAdminUrl: record.dbAdminUrl ?? null,
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
      case 'rewrite-env-db':
        result = rewriteEnvDb(args[0], args[1], args.slice(2));
        break;
      case 'migrate':
        result = migrate();
        break;
      default:
        process.stderr.write(`unknown command: ${cmd}\n`);
        process.stderr.write(`usage: node lib/workspace.mjs <list|get|create|update|remove|rewrite-env-db|migrate> [...]\n`);
        process.exit(2);
    }
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  } catch (e) {
    process.stderr.write(`error: ${e.message}\n`);
    process.exit(1);
  }
}
