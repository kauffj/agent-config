---
name: review-ui
description: Review changed frontend code against UI design principles (Nielsen heuristics + Atomic Design)
argument-hint: "[base-branch]"
---

# Review UI Changes

Review frontend code changes against the UI design principles.

## Step 1: Get the Diff

Resolve what to review, in this order:

- If `$ARGUMENTS` is given, treat it as the base branch.
- Otherwise use the repository's default branch.

```bash
BASE_REF="$ARGUMENTS"
if [ -z "$BASE_REF" ]; then
  BASE_REF=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
  [ -z "$BASE_REF" ] && { git rev-parse --verify -q origin/main >/dev/null \
      && BASE_REF=main || BASE_REF=master; }
fi
git fetch origin "$BASE_REF" --quiet 2>/dev/null
BASE=$(git merge-base "origin/$BASE_REF" HEAD 2>/dev/null \
       || git merge-base "$BASE_REF" HEAD 2>/dev/null || echo HEAD)

# Diff the merge-base against the WORKING TREE, not `...HEAD`. `...HEAD` sees
# only commits, so a branch whose work is committed but whose tree is clean
# reviews nothing at all — and the moment you most want a review is after
# committing, before opening the PR. This form covers committed, staged and
# unstaged changes at once; untracked files are unioned in separately.
CHANGED_FILES=$( { git diff --name-only "$BASE"; \
                   git ls-files --others --exclude-standard; } | sort -u )
git diff "$BASE"
```

If `CHANGED_FILES` is empty, report "No changes to review." and stop — on a
branch with commits that means the base is wrong, not that the code is clean.

## Step 2: Identify Frontend Files

From the diff, extract only frontend files: `.tsx`, `.jsx`, `.css`, `.scss`, `.html`, `.svelte`, `.vue`.

If no frontend files were changed, report "No frontend files changed — nothing for UI review." and stop.

## Step 3: Run UI Review

Launch the agent defined in `$HOME/.claude/agents/ui-reviewer.md`. Provide:
- The changed frontend files, as **paths** — the agent has Read/Grep/Glob and
  reads them itself, in its own context. Do not read them here and paste the
  contents in: that spends the main conversation's context on exactly the work
  being delegated, pays for every file twice, and caps how large a change can
  be reviewed at all.
- The diff, for what specifically changed
- `$UI_PRINCIPLES` env var points to the principles file

## Step 4: Present Findings

Display the agent's findings verbatim. The agent file owns the output contract
— graded findings, then what it could not evaluate — so it is not restated
here; two copies of one contract drift apart.
