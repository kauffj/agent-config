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

Use the search script (no other transcript spelunking needed):

```
$HOME/projects/claude-beep/claude-grep [--since 6h|2d|1w] [--all] [-m N] PATTERN
```

- Searches user + assistant text of session transcripts (what the user could
  scroll through in a terminal), newest first.
- PATTERN is a Python regex; smart-case (case-insensitive unless it contains
  an uppercase letter).
- Default window is the last 2 days. Exit code is nonzero with a "no matches"
  message when nothing hits.
- Each hit prints the project dir, git branch, last-active age, first prompt,
  matching lines, and a `claude --resume <session-id>` command.
- Hits that are currently open in a terminal tab are annotated
  `open on pts/N` with a `mark tab:` command.
- `claude-grep --mark <session-id-prefix>` renames that session's terminal
  tab to `🎯 <project> — FOUND 🎯` and rings its bell, so the user can spot
  it in their tab bar. `claude-grep --unmark <prefix>` restores the title
  (`<branch> <project>`). Both write to the tab's tty; they may need
  sandbox override / permission approval. If the user says they found the
  tab (or asks to clean up), unmark it.

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

3. **Widen if needed.** If the default 2-day window yields nothing or only
   weak hits, retry with `--since 1w`, then `--all`. If the user's wording
   implies age ("a while back", "last month"), start wider.

4. **Merge and rank.** Combine hits across patterns by session (the resume
   id). Rank by: number of distinct patterns that hit > recency > match
   count. A session matched by several independent patterns is almost
   certainly the one.

5. **Point at the tab.** If the best match is annotated `open on pts/N` and
   it is clearly the session the user described (one strong match, or
   several patterns agree), run `claude-grep --mark <id-prefix>` on it so
   its tab lights up — that is usually what the user actually wants. If
   multiple candidates are equally plausible, don't mark; list them and let
   the user choose.

## Output

Lead with the best match. For each candidate (best first, at most ~5):

- Project directory and branch, how recently it was active
- Whether it's open right now (`open on pts/N`) and, if you marked it, that
  its tab is now named `🎯 <project> — FOUND 🎯`
- Its first prompt (this is what identifies the terminal window)
- The one or two most telling matching lines
- The exact resume command: `claude --resume <session-id>`

If nothing matched even with `--all`, say so and show which patterns you
tried, so the user can correct your guesses.
