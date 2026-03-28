---
name: review-pr
description: Review a pull request using specialized review agents (security, simplicity, UI)
argument-hint: "<PR number or URL>"
disable-model-invocation: true
---

# Review a Pull Request

Review PR `$ARGUMENTS` using the project's specialized review agents.

## Step 1: Gather PR Context

Collect all PR information:

```bash
PR="$ARGUMENTS"
gh pr view "$PR" --json title,body,baseRefName,headRefName,files,additions,deletions
```

```bash
gh pr diff "$PR"
```

```bash
gh pr view "$PR" --json comments,reviews --jq '.comments[].body, .reviews[].body'
```

## Step 2: Identify Changed Files

From the diff, identify all changed files and their paths in the local repo. Read each changed file in full so you understand the surrounding context, not just the diff.

## Step 3: Run Reviews in Parallel

Launch these review agents **simultaneously** (all in a single message):

### Security Review
Use the agent defined in `$HOME/.claude/agents/security-reviewer.md`. Provide:
- The full content of all changed files
- The diff for context on what specifically changed
- Instructions to also read any auth utilities or middleware the changed code interacts with

### Simplicity Review
Use the agent defined in `$HOME/.claude/agents/simplicity-reviewer.md`. Provide:
- The full content of all changed files
- `$HICKEY_PRINCIPLES` env var points to the principles file

### UI Review (only if frontend files changed)
Use the agent defined in `$HOME/.claude/agents/ui-reviewer.md`. Provide:
- The full content of changed frontend files (.tsx, .jsx, .css, .scss)
- `$UI_PRINCIPLES` env var points to the principles file

Skip this agent if no frontend files were changed.

## Step 4: Synthesize

Consolidate all findings into a single review:

**PR: <title>** (`<head> → <base>`)

### MUST FIX
- (from all agents)

### SHOULD FIX
- (from all agents)

### CONSIDER
- (from all agents)

### Summary
One paragraph assessment: is this PR ready to merge, needs changes, or needs rethinking?
