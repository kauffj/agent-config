// PostgreSQL workspace provisioning, env rewriting, and teardown preparation.

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { withWorkspaceLifecycleLock } from './safe-runtime-files.mjs';
import { requireWorkspace, updateRecord } from './workspace-state.mjs';
import {
  DEFAULT_DB_URL_VAR,
  normalizedDatabaseEndpoint,
  readLocalDatabaseUrl,
  tryEndpointFromEnvFile,
} from './workspace-db-endpoint.mjs';
import { run, validateWorkspaceLocation } from './workspace-git.mjs';

const HELPER = fileURLToPath(new URL('./workspace.mjs', import.meta.url));

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
  if (record.dbIsolation !== 'template' || !record.dbTemplate) {
    throw new Error(`workspace '${record.name}' lacks a recorded template-isolation identity`);
  }
  const urlVar = record.dbUrlVar ?? DEFAULT_DB_URL_VAR;
  // A record written before dbEndpoint was stored is repaired on read (see the
  // migration in workspace-state.mjs). Getting here means that recovery failed,
  // so name the reason and the exact repair: a bare refusal leaves the operator
  // with a workspace that cannot be torn down at all.
  if (!record.dbEndpoint) {
    const { error } = tryEndpointFromEnvFile(record.envFile, urlVar, `workspace '${record.name}'`);
    throw new Error([
      `workspace '${record.name}' has an isolated database (${record.dbName}) but no recorded endpoint,`,
      `and none could be recovered from its env file: ${error}.`,
      `Point the record at the main-checkout env file that defines ${urlVar}, then retry:`,
      `  node ${HELPER} update ${record.name} '{"envFile":"${record.envFile || '.env'}","dbUrlVar":"${urlVar}"}'`,
      'If that database is already gone, clear its identity instead:',
      `  node ${HELPER} update ${record.name} '{"dbName":null,"dbIsolation":"none","dbTemplate":null,"dbUrlVar":null,"dbEndpoint":null}'`,
    ].join('\n'));
  }
  validateDatabaseIdentifier(record.dbName, `workspace '${record.name}' database name`);
  const expectedName = `${record.dbTemplate}_ws_${record.name}`
    .replace(/[^A-Za-z0-9_]/g, '_')
    .slice(0, 63);
  if (record.dbName !== expectedName) {
    throw new Error(`workspace '${record.name}' database name does not match its recorded template`);
  }
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
