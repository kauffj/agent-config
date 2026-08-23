---
name: find-session
description: Find a past or active Claude Code session by searching transcript text — accepts a grep string, a regex, or a natural language description of the session
argument-hint: "<pattern or description of the session you're looking for>"
context: fork
---

# Find a Claude Code Session

You are searching the user's Claude Code session transcripts to find the
session(s) they're thinking of. They often have 15+ terminal windows with
active sessions and remember only a fragment of what was said.

**Query:** $ARGUMENTS

## Tool

Use the search script that ships with this config (no other transcript
spelunking needed):

```
$HOME/.claude/bin/claude-search PATTERN [--all] [--since 6h|2d|1w] [-m N] [--paths]
```

- Searches the user + assistant text of session transcripts — what the user
  could scroll through in a terminal, not tool payloads — across Claude, Codex
  and Grok.
- PATTERN is a Python regex, always case-insensitive.
- **Scope defaults to LIVE sessions only.** Pass `--all` to sweep history; that
  is the right default for "find the session where we…" questions about
  anything older than the tabs open right now.
- `--since 6h|2d|1w` limits by how recently the transcript was touched.
- Each hit prints the session id, project directory, match count, last-active
  age, the opening prompt (this is what identifies the terminal window),
  matching lines, and the exact command that reopens it.
- Exit code is nonzero with a "no matches" message when nothing hits.

**Optional local extra.** If `$HOME/projects/claude-beep/claude-grep` exists on
this machine, it can additionally point at the tab:
`claude-grep --mark <session-id-prefix>` renames that session's terminal tab to
`🎯 <project> — FOUND 🎯` and rings its bell; `--unmark <prefix>` restores the
title. It writes to the tab's tty and may need sandbox override / permission
approval. It is a separate personal tool, **not part of this config** — check it
exists (`test -x`) before reaching for it, and just skip the marking step if it
does not. If the user says they found the tab, unmark it.

## Approach

1. **Classify the query.**
   - If it reads as a literal phrase or regex (quoted text, regex
     metacharacters, an exact error message or identifier), run it as-is —
     one search.
   - If it reads as a natural language description ("the session where we
     debugged the disk filling up"), derive 3–6 search patterns and run a
     search per pattern.

2. **Derive good patterns from descriptions.** Prefer distinctive, rare
   tokens over common words: filenames, function names, error fragments,
   domain terms, project-specific vocabulary. Include synonyms the
   conversation might have used instead of the user's wording (e.g.
   "disk full" → `disk space`, `df -h`, `100%.*use`, `filled up`). Use
   alternation (`foo|bar`) to keep the number of runs small.

3. **Widen if needed.** Start with `--all --since 2d` for a recent-sounding
   query; widen to `--since 1w`, then to a bare `--all`, when that yields
   nothing or only weak hits. If the user's wording implies age ("a while
   back", "last month"), start wide. Drop `--all` only when the user says the
   session is open right now.

4. **Merge and rank.** Combine hits across patterns by session (the resume
   id). Rank by: number of distinct patterns that hit > recency > match
   count. A session matched by several independent patterns is almost
   certainly the one.

5. **Point at the tab.** A session that still appears in a default (live)
   `claude-search` run is open right now. If the best match is clearly the
   session the user described (one strong match, or several patterns agree)
   and the optional `claude-grep` is installed, run `claude-grep --mark
   <id-prefix>` so its tab lights up — that is usually what the user actually
   wants. If multiple candidates are equally plausible, don't mark; list them
   and let the user choose.

## Output

Lead with the best match. For each candidate (best first, at most ~5):

- Project directory, how recently it was active
- Whether it's open right now (it appears in a live-scope search) and, if you
  marked it, that its tab is now named `🎯 <project> — FOUND 🎯`
- Its first prompt (this is what identifies the terminal window)
- The one or two most telling matching lines
- The exact resume command: `claude --resume <session-id>`

If nothing matched even with `--all`, say so and show which patterns you
tried, so the user can correct your guesses.
