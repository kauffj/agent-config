#!/usr/bin/env node
// Tests for the workspace port allocator (portFor / allocatePort).
// Runs in a scratch CWD so it never reads or writes the real state file.
//
//   node lib/test-workspace-port.mjs

import { mkdtempSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:net';

const scratch = mkdtempSync(join(tmpdir(), 'ws-port-'));
process.chdir(scratch);
mkdirSync('.workspaces', { recursive: true });

const { portFor, allocatePort } = await import(
  new URL('./workspace.mjs', import.meta.url).href
);

let pass = 0, fail = 0;
const ok = (cond, label) => {
  if (cond) { pass++; console.log(`  ok    ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); }
};
const setState = (workspaces) =>
  writeFileSync('.workspaces/workspaces.json', JSON.stringify({ workspaces }, null, 2));

const A = '/home/u/projects/app/.workspaces/worktrees/login';
const B = '/home/u/projects/app/.workspaces/worktrees/search';
const C = '/home/u/projects/other/.workspaces/worktrees/login';

console.log('portFor is deterministic and in range:');
ok(portFor(A) === portFor(A), 'same path twice -> same port');
ok(portFor(A) >= 20000 && portFor(A) <= 29999, `in 20000-29999 (${portFor(A)})`);
ok(portFor(A) !== portFor(B), 'two workspaces in one project -> different ports');
ok(portFor(A) !== portFor(C), 'same name, different project -> different ports');

console.log('\nnever lands in the ephemeral range or on a privileged port:');
const many = Array.from({ length: 2000 }, (_, i) => portFor(`/p/ws/${i}`));
ok(many.every((p) => p >= 20000 && p <= 29999), 'all 2000 samples inside the window');
ok(new Set(many).size > 1800, `spread: ${new Set(many).size}/2000 distinct`);

console.log('\nallocatePort honours the hash when nothing is in the way:');
setState([]);
ok((await allocatePort(A)) === portFor(A), 'empty state -> the hashed port');

console.log('\nTHE BUG: a workspace that is NOT running still owns its port:');
setState([{ name: 'login', port: portFor(A) }]);
const afterClaim = await allocatePort(B === A ? C : A);
ok(afterClaim !== portFor(A), `skipped the claimed port (got ${afterClaim})`);
ok(afterClaim === portFor(A) + 1, 'walked forward by one');

console.log('\nand a LIVE listener is skipped too:');
setState([]);
const held = portFor(C);
const srv = createServer();
await new Promise((r) => srv.listen(held, '0.0.0.0', r));
const afterBind = await allocatePort(C);
srv.close();
ok(afterBind !== held, `skipped the bound port (got ${afterBind})`);

console.log('\nboth filters at once:');
setState([{ name: 'x', port: portFor(A) }, { name: 'y', port: portFor(A) + 1 }]);
ok((await allocatePort(A)) === portFor(A) + 2, 'walks past two claims');

console.log('\nrejects a missing key rather than inventing one:');
let threw = false;
try { await allocatePort(''); } catch { threw = true; }
ok(threw, 'empty worktreePath throws');

console.log(`\n${fail === 0 ? 'ALL PASS' : 'FAILURES'} — ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
