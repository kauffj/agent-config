---
name: fix-ci
description: Diagnose and fix failing CI checks on the current branch
argument-hint: "[PR number]"
---

# Fix Failing CI

Diagnose and fix CI failures autonomously. No hand-holding needed.

## Step 1: Identify the Failure

If a PR number is given (`$ARGUMENTS`), check that PR's CI status:
```bash
gh pr checks "$ARGUMENTS" --json name,state,link
```

Otherwise check the current branch:
```bash
gh run list --branch "$(git symbolic-ref --short HEAD)" --limit 5 --json status,conclusion,name,databaseId
```

Find the most recent failing run and get its logs:
```bash
gh run view <RUN_ID> --log-failed
```

## Step 2: Diagnose

Read the error output carefully. Common categories:
- **Build failure**: type errors, missing imports, syntax errors
- **Test failure**: read the failing test, understand what it asserts, read the code it tests
- **Lint failure**: style violations, unused imports
- **Type check failure**: TypeScript errors

For each failure, find and read the relevant source file to understand context.

## Step 3: Fix

Make the minimal fix for each failure. Do not refactor surrounding code. Do not add features.

After fixing, verify locally:
1. Run the same check that failed (build, test, lint, typecheck)
2. Confirm it passes
3. If the fix introduced new failures, fix those too

## Step 4: Commit and Push

Stage only the files you changed. Commit with a clear message:

```
fix: resolve CI failure in <what failed>

<one line explaining the root cause>
```

Push to the current branch. If a PR number was given, report the fix on the PR.
