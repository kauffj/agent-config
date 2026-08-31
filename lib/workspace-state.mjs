// Persistent workspace records and legacy-state migration.

import { readdirSync, rmdirSync } from 'node:fs';
import {
  assertRegularFile,
  atomicWriteFile,
  ensureRealDirectory,
  moveRegularFile,
  readRegularFile,
  withRuntimeLock,
} from './safe-runtime-files.mjs';
import { DEFAULT_DB_URL_VAR, tryEndpointFromEnvFile } from './workspace-db-endpoint.mjs';

const STATE = '.workspaces/workspaces.json';
const LEGACY = '.claude/features.json';
const PLANS_DIR = '.workspaces/plans';
const LOCK = '.workspaces/workspaces.lock';
const DELIVERY_FIELDS = [
  'defaultBranch', 'baseSha', 'remoteFeatureShaBeforeIntegrate',
  'remoteBranchShaBeforeIntegrate', 'integratedSha', 'deployChoice', 'deploySha',
  'repositoryId', 'publishedAt', 'deliveryVerified', 'deliveryVerification',
  'deliveryEvidence',
];

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
  ensureRealDirectory('.workspaces', { create: true });
}

function relocateFromClaudeDir() {
  const moved = [];
  const legacyState = assertRegularFile('.claude/workspaces.json', { required: false });
  if (legacyState && !assertRegularFile(STATE, { required: false })) {
    ensureDir();
    moveRegularFile('.claude/workspaces.json', STATE);
    moved.push('workspaces.json');
  }
  if (ensureRealDirectory('.claude/plans')) {
    ensureRealDirectory(PLANS_DIR, { create: true });
    for (const file of readdirSync('.claude/plans')) {
      const source = `.claude/plans/${file}`;
      const destination = `${PLANS_DIR}/${file}`;
      assertRegularFile(source);
      if (!assertRegularFile(destination, { required: false })) {
        moveRegularFile(source, destination);
        moved.push(`plans/${file}`);
      }
    }
    try { rmdirSync('.claude/plans'); } catch { /* not empty, leave it */ }
  }
  return moved;
}

function migrateUnlocked() {
  const relocated = relocateFromClaudeDir();
  ensureDir();
  const withRelocated = (result) => relocated.length ? { ...result, relocated } : result;
  if (assertRegularFile(STATE, { required: false })) {
    return withRelocated({ migrated: false, reason: 'workspaces.json already exists' });
  }
  if (!assertRegularFile(LEGACY, { required: false })) {
    return withRelocated({ migrated: false, reason: 'no legacy file' });
  }

  let raw;
  try {
    raw = JSON.parse(readRegularFile(LEGACY));
  } catch (error) {
    return withRelocated({ migrated: false, reason: `legacy file unparseable: ${error.message}` });
  }
  const legacy = Array.isArray(raw) ? { project: null, features: raw } : raw;
  const workspaces = (legacy.features || []).map((feature) => ({
    name: feature.name,
    kind: 'feature',
    description: feature.description ?? '',
    branch: feature.branch ?? null,
    worktreePath: feature.worktreePath ?? null,
    port: feature.port ?? null,
    envFile: legacy.project?.envFile ?? null,
    screenshotDir: feature.screenshotDir ?? null,
    status: STATUS_MAP[feature.status] ?? 'active',
    pipeline: {
      skill: 'feature',
      step: feature.step ?? null,
      plan: feature.plan ?? null,
      legacyStatus: feature.status ?? null,
    },
    delivery: {},
    createdAt: feature.createdAt ?? new Date().toISOString(),
    updatedAt: feature.updatedAt ?? new Date().toISOString(),
  }));

  const stamp = new Date().toISOString().slice(0, 10);
  const backup = `${LEGACY}.migrated-${stamp}`;
  if (assertRegularFile(backup, { required: false })) {
    return withRelocated({ migrated: false, reason: `legacy backup already exists: ${backup}` });
  }
  atomicWriteFile(STATE, JSON.stringify({ workspaces }, null, 2));
  moveRegularFile(LEGACY, backup);
  return withRelocated({ migrated: true, backup, count: workspaces.length });
}

export function migrate() {
  return withRuntimeLock(LOCK, migrateUnlocked);
}

function writeState(data) {
  ensureDir();
  atomicWriteFile(STATE, JSON.stringify(data, null, 2));
}

function readStateUnlocked() {
  migrateUnlocked();
  ensureDir();
  if (!assertRegularFile(STATE, { required: false })) {
    atomicWriteFile(STATE, JSON.stringify({ workspaces: [] }, null, 2));
  }
  const data = JSON.parse(readRegularFile(STATE));
  let changed = false;
  for (const workspace of data.workspaces ?? []) {
    if ('dbAdminUrl' in workspace) {
      delete workspace.dbAdminUrl;
      changed = true;
    }
    if (workspace.dbName && workspace.dbIsolation === 'template' && !workspace.dbTemplate) {
      const suffix = `_ws_${workspace.name}`.replace(/[^A-Za-z0-9_]/g, '_');
      if (workspace.dbName.endsWith(suffix) && workspace.dbName.length > suffix.length) {
        workspace.dbTemplate = workspace.dbName.slice(0, -suffix.length);
        changed = true;
      }
    }
    // Same idea one field over: a record provisioned before dbEndpoint was
    // stored has an isolated database it cannot prove the location of, and
    // teardown refused it — so `workspace finish` was impossible and the
    // database leaked. The endpoint is derivable from exactly where
    // provisioning would read it today: the recorded env file's primary URL
    // variable in this checkout. Recovery is deliberately silent when it fails
    // (an ordinary read must not throw); the refusal that follows in
    // workspace-database.mjs is where the operator gets told what to repair.
    // This restores teardown without weakening it: the drift check still holds
    // for every record that HAS an endpoint, and a recovered one still has to
    // be local, override-free, and name the recorded template.
    if (workspace.dbName && workspace.dbIsolation === 'template' && !workspace.dbEndpoint) {
      const { endpoint } = tryEndpointFromEnvFile(
        workspace.envFile,
        workspace.dbUrlVar ?? DEFAULT_DB_URL_VAR,
        `workspace '${workspace.name}'`,
      );
      if (endpoint) {
        workspace.dbEndpoint = endpoint;
        changed = true;
      }
    }
    const legacyDelivery = {};
    for (const field of DELIVERY_FIELDS) {
      if (workspace.pipeline && Object.hasOwn(workspace.pipeline, field)) {
        legacyDelivery[field] = workspace.pipeline[field];
        delete workspace.pipeline[field];
        changed = true;
      }
    }
    if (Object.keys(legacyDelivery).length) {
      workspace.delivery = { ...legacyDelivery, ...(workspace.delivery ?? {}) };
    }
  }
  if (changed) writeState(data);
  return data;
}

export function list({ kind } = {}) {
  return withRuntimeLock(LOCK, () => {
    const { workspaces } = readStateUnlocked();
    return kind ? workspaces.filter((workspace) => workspace.kind === kind) : workspaces;
  });
}

export function get(name) {
  return withRuntimeLock(
    LOCK,
    () => readStateUnlocked().workspaces.find((workspace) => workspace.name === name) ?? null,
  );
}

export function requireWorkspace(name) {
  const workspace = get(name);
  if (!workspace) throw new Error(`workspace '${name}' not found`);
  return workspace;
}

export function create(record) {
  assertCallerOwnedPatch(record, { creating: true });
  return withRuntimeLock(LOCK, () => {
    const data = readStateUnlocked();
    if (data.workspaces.some((workspace) => workspace.name === record.name)) {
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
      dbTemplate: record.dbTemplate ?? null,
      dbUrlVar: record.dbUrlVar ?? null,
      dbEndpoint: record.dbEndpoint ?? null,
      pipeline: record.pipeline ?? {},
      delivery: {},
      createdAt: now,
      updatedAt: now,
    };
    data.workspaces.push(full);
    writeState(data);
    return full;
  });
}

function assertCallerOwnedPatch(patch, { creating = false } = {}) {
  if (Object.hasOwn(patch, 'delivery') && Object.keys(patch.delivery ?? {}).length) {
    throw new Error('delivery state is lifecycle-owned; use integrate/publish/verify-delivery');
  }
  if (Object.hasOwn(patch, 'status') && (patch.status !== 'active' || !creating)) {
    throw new Error('workspace status is lifecycle-owned; use finish/abandon/remove');
  }
  const reservedPipeline = Object.keys(patch.pipeline ?? {}).filter((field) => DELIVERY_FIELDS.includes(field));
  if (reservedPipeline.length) {
    throw new Error(`pipeline contains lifecycle-owned delivery field: ${reservedPipeline.join(', ')}`);
  }
  if (!creating) {
    const reservedTopLevel = DELIVERY_FIELDS.filter((field) => Object.hasOwn(patch, field));
    if (reservedTopLevel.length) {
      throw new Error(`update contains lifecycle-owned delivery field: ${reservedTopLevel.join(', ')}`);
    }
  }
}

export function updateRecord(name, patch) {
  return withRuntimeLock(LOCK, () => {
    const data = readStateUnlocked();
    const index = data.workspaces.findIndex((workspace) => workspace.name === name);
    if (index === -1) throw new Error(`workspace '${name}' not found`);
    const current = data.workspaces[index];
    const merged = { ...current, ...patch, updatedAt: new Date().toISOString() };
    if (patch.pipeline) merged.pipeline = { ...(current.pipeline ?? {}), ...patch.pipeline };
    if (patch.delivery) merged.delivery = { ...(current.delivery ?? {}), ...patch.delivery };
    data.workspaces[index] = merged;
    writeState(data);
    return merged;
  });
}

export function update(name, patch) {
  assertCallerOwnedPatch(patch);
  return updateRecord(name, patch);
}

export function removeRecord(name) {
  return withRuntimeLock(LOCK, () => {
    const data = readStateUnlocked();
    const index = data.workspaces.findIndex((workspace) => workspace.name === name);
    if (index === -1) throw new Error(`workspace '${name}' not found`);
    const [removed] = data.workspaces.splice(index, 1);
    writeState(data);
    return removed;
  });
}
