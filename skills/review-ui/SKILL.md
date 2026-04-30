---
name: review-ui
description: Review changed frontend code against UI design principles (Nielsen heuristics + Atomic Design)
argument-hint: "[base-branch]"
---

# Review UI Changes

Review frontend code changes against the UI design principles.

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

## Step 2: Identify Frontend Files

From the diff, extract only frontend files: `.tsx`, `.jsx`, `.css`, `.scss`, `.html`, `.svelte`, `.vue`.

If no frontend files were changed, report "No frontend files changed — nothing for UI review." and stop.

## Step 3: Read Changed Files

Read each changed frontend file **in full** so you understand the surrounding context, not just the diff hunks.

## Step 4: Run UI Review

Launch the agent defined in `$HOME/.claude/agents/ui-reviewer.md`. Provide:
- The full content of all changed frontend files
- The diff for context on what specifically changed
- `$UI_PRINCIPLES` env var points to the principles file

## Step 5: Present Findings

Display the agent's findings directly. The output will be structured as:

### MUST FIX
### SHOULD FIX
### CONSIDER
### What I couldn't evaluate
