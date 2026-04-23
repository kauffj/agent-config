#!/usr/bin/env node
// Workspace state store. Invoke as CLI or import as module.
//
// CLI:
//   node lib/workspace.mjs list [--kind <kind>]
//   node lib/workspace.mjs get <name>
//   node lib/workspace.mjs create <json-record>
//   node lib/workspace.mjs update <name> <json-patch>
//   node lib/workspace.mjs remove <name>
//   node lib/workspace.mjs migrate
//
// State file: .claude/workspaces.json (relative to repo root CWD).
// On first access, migrates .claude/features.json if present.

import { readFileSync, writeFileSync, existsSync, renameSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const STATE = '.claude/workspaces.json';
const LEGACY = '.claude/features.json';
const PROJECT = '.claude/project.json';

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
  if (!existsSync('.claude')) mkdirSync('.claude', { recursive: true });
}

export function migrate() {
  ensureDir();
  if (existsSync(STATE)) return { migrated: false, reason: 'workspaces.json already exists' };
  if (!existsSync(LEGACY)) return { migrated: false, reason: 'no legacy file' };

  let raw;
  try {
    raw = JSON.parse(readFileSync(LEGACY, 'utf8'));
  } catch (e) {
    return { migrated: false, reason: `legacy file unparseable: ${e.message}` };
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

  return { migrated: true, backup, count: workspaces.length };
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

export function remove(name) {
  const data = readState();
  const i = data.workspaces.findIndex((w) => w.name === name);
  if (i === -1) throw new Error(`workspace '${name}' not found`);
  const [removed] = data.workspaces.splice(i, 1);
  writeState(data);
  return removed;
}

// CLI
if (import.meta.url === `file://${process.argv[1]}`) {
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
      case 'migrate':
        result = migrate();
        break;
      default:
        process.stderr.write(`unknown command: ${cmd}\n`);
        process.stderr.write(`usage: node lib/workspace.mjs <list|get|create|update|remove|migrate> [...]\n`);
        process.exit(2);
    }
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  } catch (e) {
    process.stderr.write(`error: ${e.message}\n`);
    process.exit(1);
  }
}
