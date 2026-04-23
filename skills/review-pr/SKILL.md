---
name: review-pr
description: Run specialized review agents (security, simplicity, UI, optional visual + QA) over a PR, a worktree, or an explicit file set.
argument-hint: "<PR number/URL> | --worktree <path> | --files <f1,f2,...>  [--server <url>] [--screenshots <dir>]"
---

# /review-pr — parallel code review

Runs review agents in parallel over a set of changed files with context. Works against:
- A GitHub PR (fetched via `gh`)
- A local git worktree (diff vs. default branch)
- An explicit file list

When a running dev server and/or screenshot directory are provided, also runs functional-QA and visual-review agents.

**Agents** (in `$HOME/.claude/agents/`):
- `security-reviewer.md` — always
- `simplicity-reviewer.md` — always
- `ui-reviewer.md` — if any frontend files changed
- `qa-tester.md` — if `--server <url>` provided
- `visual-reviewer.md` — if `--screenshots <dir>` provided

**Reference files:**
- `$HICKEY_PRINCIPLES` and `$UI_PRINCIPLES` are set via settings.json env

---

## Parse `$ARGUMENTS`

Detect the mode:

- If `$ARGUMENTS` starts with `--worktree` → **Worktree mode**
- If `$ARGUMENTS` starts with `--files` → **File-list mode**
- Otherwise → **PR mode** (first token is the PR ref)

Also parse optional flags that apply to all modes:
- `--server <url>` — dev server URL, enables QA agent
- `--screenshots <dir>` — screenshot directory, enables visual agent
- `--acceptance-criteria <text>` — passed to QA agent (optional)

---

## Step 1: Gather changed files

### PR mode

```bash
PR="$PR_REF"
gh pr view "$PR" --json title,body,baseRefName,headRefName,files,additions,deletions
gh pr diff "$PR"
gh pr view "$PR" --json comments,reviews --jq '.comments[].body, .reviews[].body'
```

Use the file list from `gh pr view ... --json files`. Check out the PR locally if needed, or read the files via `gh pr view --json files -q '.files[].path'` + `git show origin/<head>:<path>`.

### Worktree mode

```bash
cd "$WORKTREE_PATH"
DEFAULT_BRANCH=$(node $HOME/.claude/lib/project.mjs load | jq -r .defaultBranch)
git fetch origin "$DEFAULT_BRANCH" --quiet
CHANGED_FILES=$(git diff --name-only "origin/$DEFAULT_BRANCH"...HEAD)
```

Read each file at its current worktree path. The diff is `git diff origin/$DEFAULT_BRANCH...HEAD`.

### File-list mode

Parse comma-separated paths from `--files`. Each path is relative to the current working directory. Read each in full.

---

## Step 2: Identify frontend files

Frontend files = `.tsx`, `.jsx`, `.ts`/`.js` under `components/`, `app/`, or `pages/`, `.css`, `.scss`, `.vue`, `.svelte`. If any changed, the UI reviewer runs.

---

## Step 3: Launch review agents in parallel

**Launch all applicable agents in a single message.** Each agent's persona + criteria are in its agent file — read the file and use it as the base prompt, then append the context listed below.

### 3a. Security Review (always)

Agent: `$HOME/.claude/agents/security-reviewer.md`

Append:
- Changed files with full paths: [LIST]
- Diff for context: [INSERT DIFF]
- Instruction: also read any auth utilities, middleware, or session helpers the changed code interacts with.

### 3b. Simplicity Review (always)

Agent: `$HOME/.claude/agents/simplicity-reviewer.md`

Append:
- Changed files with full paths: [LIST]
- `$HICKEY_PRINCIPLES` is set via settings.json env

### 3c. UI Review (if frontend files changed)

Agent: `$HOME/.claude/agents/ui-reviewer.md`

Append:
- Changed frontend files: [LIST]
- `$UI_PRINCIPLES` is set via settings.json env

Skip if no frontend files.

### 3d. QA Review (if `--server <url>` provided)

Agent: `$HOME/.claude/agents/qa-tester.md`

Append:
- Dev server URL: `$SERVER_URL`
- Affected URLs to exercise: [derive from changed route files, or accept from caller]
- Acceptance criteria: [INSERT from `--acceptance-criteria`, or "none provided — exercise the changed surfaces generally"]

Skip if no `--server`.

### 3e. Visual Review (if `--screenshots <dir>` provided)

Agent: `$HOME/.claude/agents/visual-reviewer.md`

Append:
- Screenshot files: [list of paths in `$SCREENSHOTS_DIR`]
- `$UI_PRINCIPLES` is set via settings.json env

Skip if no `--screenshots`.

---

## Step 4: Synthesize

Consolidate all findings into a single review.

**Review: <title or branch>**

### MUST FIX
- (from all agents)

### SHOULD FIX
- (from all agents)

### CONSIDER
- (from all agents)

### Summary
One paragraph: ready to merge, needs changes, or needs rethinking?

Also print a machine-parseable tail so callers can consume counts:

```
REVIEW_SUMMARY
mustFix=<N>
shouldFix=<N>
consider=<N>
```
