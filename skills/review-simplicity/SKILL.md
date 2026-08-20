---
name: review-simplicity
description: Review changed code for unnecessary complexity, complection, premature abstraction, and incidental complexity
argument-hint: "[base-branch]"
---

# Review Simplicity

Review code changes for unnecessary complexity.

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

## Step 2: Read Changed Files

Read each file in `CHANGED_FILES` **in full** so you understand the surrounding context, not just the diff hunks.

## Step 3: Run Simplicity Review

Launch the agent defined in `$HOME/.claude/agents/simplicity-reviewer.md`. Provide:
- The full content of all changed files
- The diff for context on what specifically changed
- `$HICKEY_PRINCIPLES` env var points to the principles file

## Step 4: Present Findings

Display the agent's findings directly. The output will be structured as:

### MUST FIX
### SHOULD FIX
### CONSIDER
### What I couldn't evaluate
