#!/usr/bin/env node
// Stable CLI façade for isolated workspace state, ports, databases, and delivery.
//
// State: workspace-state.mjs
// Git/worktree invariants: workspace-git.mjs
// PostgreSQL lifecycle: workspace-database.mjs
// Publication and teardown: workspace-delivery.mjs

import { appendFileSync, existsSync, mkdirSync, readFileSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { createServer } from 'node:net';
import { ensureRealDirectory } from './safe-runtime-files.mjs';
import { create, get, list, migrate, update } from './workspace-state.mjs';
import { actualDefaultBranch, git, stdout } from './workspace-git.mjs';
import { cloneDatabase, rewriteEnvDb } from './workspace-database.mjs';
import {
  abandon,
  finish,
  integrate,
  publish,
  remove,
  verifyDelivery,
} from './workspace-delivery.mjs';

export {
  abandon,
  actualDefaultBranch,
  cloneDatabase,
  create,
  finish,
  get,
  integrate,
  list,
  migrate,
  publish,
  remove,
  rewriteEnvDb,
  update,
  verifyDelivery,
};

const PORT_MIN = 20000;
const PORT_MAX = 29999;
const PORT_SPAN = PORT_MAX - PORT_MIN + 1;

export function portFor(key) {
  const digest = createHash('sha256').update(key).digest();
  return PORT_MIN + (digest.readUInt32BE(0) % PORT_SPAN);
}

function bindable(port) {
  return new Promise((resolve) => {
    const server = createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => server.close(() => resolve(true)));
    server.listen(port, '0.0.0.0');
  });
}

export async function allocatePort(worktreePath, { exclude = [] } = {}) {
  if (!worktreePath) throw new Error('allocatePort: worktreePath is required');
  const claimed = new Set([
    ...list().map((workspace) => workspace.port).filter((port) => Number.isInteger(port)),
    ...exclude,
  ]);
  const start = portFor(worktreePath);
  for (let offset = 0; offset < PORT_SPAN; offset++) {
    const port = PORT_MIN + ((start - PORT_MIN + offset) % PORT_SPAN);
    if (claimed.has(port)) continue;
    if (await bindable(port)) return port;
  }
  throw new Error(`no free port in ${PORT_MIN}-${PORT_MAX}`);
}

export function bootstrap() {
  const commonDir = stdout(git(['rev-parse', '--path-format=absolute', '--git-common-dir']));
  const infoDir = join(commonDir, 'info');
  const excludeFile = join(infoDir, 'exclude');
  mkdirSync(infoDir, { recursive: true });
  const current = existsSync(excludeFile) ? readFileSync(excludeFile, 'utf8') : '';
  const addedExclude = !current.split('\n').includes('.workspaces/');
  if (addedExclude) {
    appendFileSync(excludeFile, `${current && !current.endsWith('\n') ? '\n' : ''}.workspaces/\n`);
  }
  ensureRealDirectory('.workspaces/worktrees', { create: true });
  return { addedExclude, migration: migrate() };
}

if (import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const [, , command, ...args] = process.argv;
  try {
    let result;
    switch (command) {
      case 'bootstrap':
        result = bootstrap();
        break;
      case 'list': {
        const kindIndex = args.indexOf('--kind');
        result = list({ kind: kindIndex >= 0 ? args[kindIndex + 1] : undefined });
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
      case 'default-branch':
        result = { defaultBranch: actualDefaultBranch() };
        break;
      case 'abandon':
        result = abandon(args[0]);
        break;
      case 'rewrite-env-db':
        result = rewriteEnvDb(args[0], args[1], args.slice(2));
        break;
      case 'clone-database':
        result = cloneDatabase(args[0], args[1], args[2], args[3]);
        break;
      case 'integrate':
        result = integrate(args[0]);
        break;
      case 'publish':
        result = publish(args[0], args[1]);
        break;
      case 'verify-delivery':
        result = verifyDelivery(args[0], args[1]);
        break;
      case 'finish':
        result = finish(args[0]);
        break;
      case 'migrate':
        result = migrate();
        break;
      default:
        process.stderr.write(`unknown command: ${command}\n`);
        process.stderr.write('usage: node lib/workspace.mjs <bootstrap|list|get|create|update|remove|port|default-branch|abandon|rewrite-env-db|clone-database|integrate|publish|verify-delivery|finish|migrate> [...]\n');
        process.exit(2);
    }
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    process.exit(1);
  }
}
