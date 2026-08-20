---
name: review-security
description: Review changed code for authorization gaps, input validation failures, data exposure, and injection vulnerabilities
argument-hint: "[base-branch]"
---

# Review Security

Review code changes for security vulnerabilities.

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

## Step 2: Run Security Review

Launch the agent defined in `$HOME/.claude/agents/security-reviewer.md`. Provide:
- The changed files, as **paths** (`CHANGED_FILES`) — the agent has Read/Grep/Glob
  and reads them itself, in its own context. Do not read them here and paste the
  contents in: that spends the main conversation's context on exactly the work
  being delegated, pays for every file twice, and caps how large a change can be
  reviewed at all.
- The diff, for what specifically changed
- The instruction to also read any auth utilities, middleware, or session
  helpers the changed code imports or interacts with — reaching past the diff is
  the whole job here, since the vulnerability is usually in what the change
  *assumes* rather than in the changed lines.

## Step 3: Present Findings

Display the agent's findings verbatim. The agent file owns the output contract
— graded findings, then what it could not evaluate — so it is not restated
here; two copies of one contract drift apart.
