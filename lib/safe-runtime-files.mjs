import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  unlinkSync,
  writeSync,
  fsyncSync,
} from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, join, resolve, sep } from 'node:path';

function parts(relativePath) {
  if (!relativePath || relativePath.startsWith('/') || relativePath.split(/[\\/]/).some((part) => part === '..')) {
    throw new Error(`runtime path must stay relative to the repository: ${relativePath}`);
  }
  return relativePath.split(/[\\/]/).filter((part) => part && part !== '.');
}

function repositoryRoot() {
  return realpathSync(resolve('.'));
}

function runtimePath(relativePath) {
  const root = repositoryRoot();
  const target = resolve(root, ...parts(relativePath));
  if (target !== root && !target.startsWith(`${root}${sep}`)) {
    throw new Error(`runtime path escapes the repository: ${relativePath}`);
  }
  return target;
}

export function ensureRealDirectory(relativePath, { create = false } = {}) {
  let current = repositoryRoot();
  for (const part of parts(relativePath)) {
    current = join(current, part);
    let stat;
    try {
      stat = lstatSync(current);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      if (!create) return false;
      try {
        mkdirSync(current, { mode: 0o700 });
      } catch (mkdirError) {
        if (mkdirError.code !== 'EEXIST') throw mkdirError;
      }
      stat = lstatSync(current);
    }
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error(`${relativePath} must use real repository directories`);
    }
  }
  return true;
}

export function assertRegularFile(relativePath, { required = true } = {}) {
  const parent = dirname(relativePath);
  if (parent !== '.' && !ensureRealDirectory(parent)) {
    if (!required) return false;
    throw new Error(`${relativePath} does not exist`);
  }
  const target = runtimePath(relativePath);
  let stat;
  try {
    stat = lstatSync(target);
  } catch (error) {
    if (error.code === 'ENOENT' && !required) return false;
    throw error;
  }
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`${relativePath} must be a regular, non-symlink file`);
  }
  return true;
}

export function readRegularFile(relativePath) {
  assertRegularFile(relativePath);
  const target = runtimePath(relativePath);
  const fd = openSync(target, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    if (!fstatSync(fd).isFile()) throw new Error(`${relativePath} must be a regular file`);
    return readFileSync(fd, 'utf8');
  } finally {
    closeSync(fd);
  }
}

export function atomicWriteFile(relativePath, text) {
  const parent = dirname(relativePath);
  if (parent !== '.') ensureRealDirectory(parent, { create: true });
  assertRegularFile(relativePath, { required: false });
  const target = runtimePath(relativePath);
  const temporary = `${target}.tmp-${randomUUID()}`;
  let fd;
  try {
    fd = openSync(
      temporary,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600,
    );
    const bytes = Buffer.from(text);
    let offset = 0;
    while (offset < bytes.length) {
      offset += writeSync(fd, bytes, offset, bytes.length - offset);
    }
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;
    renameSync(temporary, target);
  } catch (error) {
    if (fd !== undefined) closeSync(fd);
    try { unlinkSync(temporary); } catch { /* absent or already renamed */ }
    throw error;
  }
}

export function moveRegularFile(source, destination) {
  assertRegularFile(source);
  const parent = dirname(destination);
  if (parent !== '.') ensureRealDirectory(parent, { create: true });
  assertRegularFile(destination, { required: false });
  renameSync(runtimePath(source), runtimePath(destination));
  assertRegularFile(destination);
}

const lockWait = new Int32Array(new SharedArrayBuffer(4));

export function withRuntimeLock(relativePath, operation, { timeoutMs = 10000 } = {}) {
  const parent = dirname(relativePath);
  if (parent !== '.') ensureRealDirectory(parent, { create: true });
  const target = runtimePath(relativePath);
  const deadline = Date.now() + timeoutMs;
  while (true) {
    let fd;
    try {
      fd = openSync(
        target,
        constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
        0o600,
      );
      writeSync(fd, `${process.pid}\n`);
      closeSync(fd);
      fd = undefined;
      try {
        return operation();
      } finally {
        unlinkSync(target);
      }
    } catch (error) {
      if (fd !== undefined) closeSync(fd);
      if (error.code !== 'EEXIST') throw error;
      try {
        if (!assertRegularFile(relativePath, { required: false })) continue;
        if (Date.now() >= deadline) {
          const owner = readRegularFile(relativePath).trim() || 'unknown';
          throw new Error(
            `${relativePath} is still held by recorded pid ${owner}; `
            + 'refusing an unlocked state update. If its process was terminated, '
            + 'inspect the state and remove this lock deliberately.',
          );
        }
      } catch (inspectionError) {
        // The owner may release the lock between any two inspection calls.
        // Treat that disappearance as contention resolving, then retry.
        if (inspectionError.code === 'ENOENT') continue;
        throw inspectionError;
      }
      Atomics.wait(lockWait, 0, 0, 10);
    }
  }
}

export function withWorkspaceLifecycleLock(name, operation) {
  if (typeof name !== 'string' || !name) throw new Error('workspace name is required');
  const key = createHash('sha256').update(name).digest('hex');
  return withRuntimeLock(`.workspaces/lifecycle-${key}.lock`, operation);
}
