// The local PostgreSQL endpoint a checkout's env file describes.
//
// Deliberately a leaf: no connection string is ever stored in a workspace
// record, so both the database lifecycle (workspace-database.mjs) and the
// legacy-record migration (workspace-state.mjs) have to re-derive the endpoint
// from the main checkout's env file. Keeping that derivation here lets the
// state module repair an old record without depending on the database module
// that imports it.

import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';

export function readLocalDatabaseUrl(envFile, urlVar, label = 'database') {
  if (!envFile || isAbsolute(envFile)) {
    throw new Error(`${label} has no safe main-checkout env file`);
  }
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(urlVar ?? '')) {
    throw new Error(`${label} has no safe primary URL variable`);
  }
  const envPath = resolve(envFile);
  const root = resolve('.');
  if (!envPath.startsWith(`${root}/`) || !existsSync(envPath)) {
    throw new Error(`${label} env file is outside or missing from the main checkout`);
  }
  if (realpathSync(envPath) !== envPath) {
    throw new Error(`${label} env file must not be a symlink`);
  }
  const line = readFileSync(envPath, 'utf8')
    .split('\n')
    .find((candidate) => {
      const match = candidate.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/);
      return match?.[1] === urlVar;
    });
  if (!line) throw new Error(`${label} main env has no ${urlVar}`);
  let value = line.replace(/^[^=]*=\s*/, '').trim();
  if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
    value = value.slice(1, -1);
  }
  const databaseUrl = new URL(value);
  if (!['postgres:', 'postgresql:'].includes(databaseUrl.protocol)) {
    throw new Error(`${label} ${urlVar} is not PostgreSQL`);
  }
  const forbiddenOverrides = new Set(['host', 'hostaddr', 'service', 'dbname']);
  for (const key of databaseUrl.searchParams.keys()) {
    if (forbiddenOverrides.has(key.toLowerCase())) {
      throw new Error(`${label} ${urlVar} contains unsafe libpq target override '${key}'`);
    }
  }
  const hostname = databaseUrl.hostname.replace(/^\[|\]$/g, '');
  if (!['', 'localhost', '127.0.0.1', '::1'].includes(hostname)) {
    throw new Error(`${label} ${urlVar} is not local`);
  }
  return databaseUrl;
}

export function normalizedDatabaseEndpoint(databaseUrl) {
  const hostname = databaseUrl.hostname.replace(/^\[|\]$/g, '');
  return {
    transport: hostname ? 'tcp' : 'default-local-socket',
    host: hostname,
    port: databaseUrl.port || '5432',
    user: decodeURIComponent(databaseUrl.username),
    database: decodeURIComponent(databaseUrl.pathname.replace(/^\//, '')),
  };
}

// The default primary URL variable, used when a record predates dbUrlVar.
export const DEFAULT_DB_URL_VAR = 'DATABASE_URL';

// Recover-or-explain: returns { endpoint } or { error }. Callers that are only
// migrating old data ignore the error; the one that has to refuse quotes it, so
// the operator is told what to repair instead of just being told 'no'.
export function tryEndpointFromEnvFile(envFile, urlVar, label = 'database endpoint') {
  try {
    return { endpoint: normalizedDatabaseEndpoint(readLocalDatabaseUrl(envFile, urlVar, label)) };
  } catch (error) {
    return { error: error.message };
  }
}
