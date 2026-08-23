#!/usr/bin/env node
// git clean filter for settings.json — drops `autoMode.environment` on the way
// into git, and leaves the working copy alone.
//
// Auto mode writes a generated dossier of this machine's infrastructure into
// settings.json: production hostnames, where secrets live, how deploys work.
// It belongs in the working file (auto mode reads it) and never in a public
// repo. Every one of the 24 commits that touched settings.json stripped it by
// hand first, with hooks/pre-commit as the net for when someone forgot.
//
// A clean filter ends the ritual: git filters the working file on its way into
// the index, so the block cannot reach a commit even if every human and hook
// forgets. `git add settings.json` when only the block changed stages nothing.
//
// What it does NOT do is quiet `git status`. Git short-circuits its modified
// check on file size (index_fd is never reached when sizes differ), and this
// filter always shrinks the file — so settings.json reads as modified until an
// `git add` refreshes the cached stat, after which it stays clean. That is the
// same thing status reported before this filter existed, so nothing regressed;
// it just is not the extra win it looks like it should be.
//
// Install (see README):
//   git config filter.strip-automode.clean "node lib/strip-automode.mjs"
//
// Usage: content on stdin, filtered content on stdout.
//
// Note there is deliberately no smudge filter. Checking settings.json out
// writes the stripped form — correct for a fresh clone, and harmless here:
// the block is generated context, not configuration anyone authored.

import { readFileSync } from 'node:fs';

const raw = readFileSync(0, 'utf8');

// A filter that throws takes the commit with it, and a filter that "fixes"
// malformed input silently corrupts the file. Anything unparseable passes
// through untouched — pre-commit is still there to catch what this misses.
let settings;
try {
  settings = JSON.parse(raw);
} catch {
  process.stdout.write(raw);
  process.exit(0);
}

if (settings?.autoMode && 'environment' in settings.autoMode) {
  delete settings.autoMode.environment;
  // Key order survives JSON.parse/stringify, so the output differs from the
  // input by exactly the removed key — no reformatting noise in the diff.
  process.stdout.write(JSON.stringify(settings, null, 2) + '\n');
} else {
  process.stdout.write(raw);
}
