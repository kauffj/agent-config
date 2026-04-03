---
name: review-security
description: Review changed code for authorization gaps, input validation failures, data exposure, and injection vulnerabilities
argument-hint: "[base-branch]"
disable-model-invocation: true
---

# Review Security

Review code changes for security vulnerabilities.

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

Also read any auth utilities, middleware, or session helpers that the changed code imports or interacts with.

## Step 3: Run Security Review

Launch the agent defined in `$HOME/.claude/agents/security-reviewer.md`. Provide:
- The full content of all changed files
- The diff for context on what specifically changed
- Instructions to also read any auth utilities or middleware the changed code interacts with

## Step 4: Present Findings

Display the agent's findings directly. The output will be structured as:

### MUST FIX
### SHOULD FIX
### CONSIDER
### What I couldn't evaluate
