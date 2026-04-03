---
name: review-simplicity
description: Review changed code for unnecessary complexity, complection, premature abstraction, and incidental complexity
argument-hint: "[base-branch]"
disable-model-invocation: true
---

# Review Simplicity

Review code changes for unnecessary complexity.

## Step 1: Get the Diff

Determine which changes to review:

- If `$ARGUMENTS` is provided, use it as the base branch:
  ```bash
  git diff $ARGUMENTS...HEAD
  ```
- Otherwise, check for staged changes:
  ```bash
  git diff --staged
  ```
- If nothing is staged, diff against HEAD:
  ```bash
  git diff HEAD
  ```

If there are no changes at all, report "No changes to review." and stop.

## Step 2: Read Changed Files

From the diff, identify all changed files and read each one **in full** so you understand the surrounding context, not just the diff hunks.

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
