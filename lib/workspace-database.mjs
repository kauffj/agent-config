// PostgreSQL workspace provisioning, env rewriting, and teardown preparation.

import { existsSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
import { withWorkspaceLifecycleLock } from './safe-runtime-files.mjs';
import { requireWorkspace, updateRecord } from './workspace-state.mjs';
import { run, validateWorkspaceLocation } from './workspace-git.mjs';

export function rewriteEnvDb(envPath, dbName, vars) {
  let text = readFileSync(envPath, 'utf8');
  for (const variable of vars) {
    const expression = new RegExp(`^(\\s*${variable}\\s*=\\s*)(.*)$`, 'm');
    text = text.replace(expression, (line, prefix, rawValue) => {
      let value = rawValue.trim();
      let quote = '';
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        quote = value[0];
        value = value.slice(1, -1);
      }
      let url;
      try {
        url = new URL(value);
      } catch {
        return line;
      }
      url.pathname = `/${encodeURIComponent(dbName)}`;
      return `${prefix}${quote}${url.href}${quote}`;
    });
  }
  writeFileSync(envPath, text);
  return { envPath, dbName, vars };
}

function readLocalDatabaseUrl(envFile, urlVar, label = 'database') {
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

function normalizedDatabaseEndpoint(databaseUrl) {
  const hostname = databaseUrl.hostname.replace(/^\[|\]$/g, '');
  return {
    transport: hostname ? 'tcp' : 'default-local-socket',
    host: hostname,
    port: databaseUrl.port || '5432',
    user: decodeURIComponent(databaseUrl.username),
    database: decodeURIComponent(databaseUrl.pathname.replace(/^\//, '')),
  };
}

function psqlConnection(databaseUrl, database = 'postgres') {
  const endpoint = normalizedDatabaseEndpoint(databaseUrl);
  const args = ['--dbname', database, '-v', 'ON_ERROR_STOP=1'];
  if (endpoint.transport === 'tcp') args.unshift('--host', endpoint.host);
  if (databaseUrl.port) args.push('--port', databaseUrl.port);
  if (databaseUrl.username) args.push('--username', decodeURIComponent(databaseUrl.username));
  const env = { ...process.env };
  for (const key of [
    'PGHOST', 'PGHOSTADDR', 'PGSERVICE', 'PGSERVICEFILE',
    'PGDATABASE', 'PGPORT', 'PGUSER',
  ]) delete env[key];
  env.PGPASSWORD = decodeURIComponent(databaseUrl.password);
  return { args, env, endpoint };
}

function validateDatabaseIdentifier(value, label) {
  if (!/^[A-Za-z0-9_]{1,63}$/.test(value ?? '')) {
    throw new Error(`${label} must contain only letters, digits, or underscores`);
  }
}

function cloneDatabaseUnlocked(name, urlVar, template, dbName) {
  const record = requireWorkspace(name);
  if (record.status !== 'active') throw new Error(`workspace '${name}' is ${record.status}, not active`);
  validateWorkspaceLocation(record, { clean: false });
  validateDatabaseIdentifier(template, 'database template');
  validateDatabaseIdentifier(dbName, 'workspace database name');
  const expectedName = `${template}_ws_${name}`.replace(/[^A-Za-z0-9_]/g, '_').slice(0, 63);
  if (dbName !== expectedName) {
    throw new Error(`workspace '${name}' database name does not match the requested template`);
  }
  const databaseUrl = readLocalDatabaseUrl(record.envFile, urlVar, 'database provisioning');
  const connection = psqlConnection(databaseUrl);
  if (connection.endpoint.database !== template) {
    throw new Error('database provisioning endpoint does not match the requested template');
  }
  updateRecord(name, {
    dbName,
    dbIsolation: 'template',
    dbTemplate: template,
    dbUrlVar: urlVar,
    dbEndpoint: connection.endpoint,
  });
  const createArgs = [
    ...connection.args,
    '-c', `CREATE DATABASE "${dbName}" TEMPLATE "${template}"`,
  ];
  let created = run('psql', createArgs, {
    env: connection.env,
    redactArgs: true,
    allowFailure: true,
  });
  let retried = false;
  if (created.status !== 0) {
    retried = true;
    run('psql', [
      ...connection.args,
      '-c', `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${template}' AND pid <> pg_backend_pid() AND state = 'idle'`,
    ], { env: connection.env, redactArgs: true });
    created = run('psql', createArgs, {
      env: connection.env,
      redactArgs: true,
      allowFailure: true,
    });
  }
  if (created.status !== 0) {
    updateRecord(name, {
      dbName: null,
      dbIsolation: 'none',
      dbTemplate: null,
      dbUrlVar: null,
      dbEndpoint: null,
    });
    throw new Error('psql failed to create the isolated workspace database');
  }
  return { dbName, dbTemplate: template, dbEndpoint: connection.endpoint, retried };
}

export function cloneDatabase(name, urlVar, template, dbName) {
  return withWorkspaceLifecycleLock(
    name,
    () => cloneDatabaseUnlocked(name, urlVar, template, dbName),
  );
}

export function prepareDatabaseCleanup(record) {
  if (!record.dbName) return null;
  if (record.dbIsolation !== 'template' || !record.dbTemplate || !record.dbEndpoint) {
    throw new Error(`workspace '${record.name}' lacks a recorded template-isolation identity`);
  }
  validateDatabaseIdentifier(record.dbName, `workspace '${record.name}' database name`);
  const expectedName = `${record.dbTemplate}_ws_${record.name}`
    .replace(/[^A-Za-z0-9_]/g, '_')
    .slice(0, 63);
  if (record.dbName !== expectedName) {
    throw new Error(`workspace '${record.name}' database name does not match its recorded template`);
  }
  const urlVar = record.dbUrlVar ?? 'DATABASE_URL';
  const databaseUrl = readLocalDatabaseUrl(record.envFile, urlVar, `workspace '${record.name}'`);
  const endpoint = normalizedDatabaseEndpoint(databaseUrl);
  if (Object.keys(endpoint).some((key) => endpoint[key] !== record.dbEndpoint[key])) {
    throw new Error(`workspace '${record.name}' database endpoint changed since provisioning`);
  }
  if (endpoint.database !== record.dbTemplate) {
    throw new Error(`workspace '${record.name}' database endpoint is not its recorded template`);
  }
  const connection = psqlConnection(databaseUrl);
  return {
    args: [
      ...connection.args,
      '-c', `DROP DATABASE IF EXISTS "${record.dbName}" WITH (FORCE);`,
    ],
    env: connection.env,
  };
}

export function dropWorkspaceDatabase(prepared) {
  if (!prepared) return false;
  run('psql', prepared.args, { env: prepared.env, redactArgs: true });
  return true;
}
