---
name: review-pr
description: Run specialized review agents (security, simplicity, UI, optional visual + QA) over a PR, a worktree, or an explicit file set.
---

# Parallel code review

Runs review agents in parallel over a set of changed files with context. Works against:
- A GitHub PR (fetched via `gh`)
- A local git worktree (diff vs. default branch)
- An explicit file list

When a running dev server and/or screenshot directory are provided, also runs functional-QA and visual-review agents.

**Reviewer role prompts** (in `$HOME/.config/agent-config/agents/`):
- `security-reviewer.md` — whenever any executable code changed
- `simplicity-reviewer.md` — whenever any executable code changed
- `ui-reviewer.md` — if any frontend files changed
- `qa-tester.md` — if `--server <url>` provided
- `visual-reviewer.md` — if `--screenshots <dir>` provided

**On gating.** The only gate on the security and simplicity reviewers is
mechanical: did any executable code change at all (Step 2)? Deciding by topic —
"this diff doesn't touch auth, skip security" — requires having done the review
to know, and the cost of being wrong is asymmetric: a wasted agent versus a
missed vulnerability. Prose-only changes are the one case that needs no judgment
to rule out.

**Reference files:**
- `$HOME/.config/agent-config/hickey-principles.md`
- `$HOME/.config/agent-config/ui-design-principles.md`

---

## Parse the request

Detect the mode:

- If the invocation text starts with `--worktree` → **Worktree mode**
- If the invocation text starts with `--files` → **File-list mode**
- Otherwise → **PR mode** (first token is the PR ref)

Also parse optional flags that apply to all modes:
- `--server <url>` — dev server URL, enables QA agent
- `--screenshots <dir>` — screenshot directory, enables visual agent
- `--acceptance-criteria <text>` — passed to QA agent (optional)
- `--base <branch>` — worktree-mode base override; otherwise resolve the remote default
- `--reviewers <comma-list>` — restrict execution to any of `security`,
  `simplicity`, `ui`, `qa`, or `visual`; otherwise use the classifiers below

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
DEFAULT_BRANCH=${BASE_OVERRIDE:-$(node "$HOME/.config/agent-config/lib/workspace.mjs" default-branch | jq -r .defaultBranch)}
git fetch origin "$DEFAULT_BRANCH" --quiet
# Diff the merge-base against the WORKING TREE, not ...HEAD: a recovered or
# in-progress worktree can hold extensive uncommitted work, and `...HEAD` sees
# only commits — it reports an empty diff and the review silently reviews
# nothing. Identical to the old behavior once everything is committed; adds
# staged + unstaged changes, and unions in untracked files.
BASE=$(git merge-base "origin/$DEFAULT_BRANCH" HEAD)
CHANGED_FILES=$( { git diff --name-only "$BASE"; git ls-files --others --exclude-standard; } | sort -u )
```

Read each file at its current worktree path. The diff is `git diff "$BASE"`.

If `CHANGED_FILES` comes back empty, say so and **stop** rather than reviewing
nothing — an empty diff in worktree mode means the target is wrong, not that the
code is clean.

### File-list mode

Parse comma-separated paths from `--files`. Each path is relative to the current working directory. Read each in full.

---

## Step 2: Classify the changed files

**Is there any code at all?** *Prose-only* = every changed path is documentation
or an asset: `.md`, `.txt`, `.rst`, `LICENSE`, `.gitignore`, or an image
(`.png`, `.jpg`, `.svg`, `.gif`, `.webp`).

If the change is prose-only, skip the security and simplicity reviewers, say so
in one line ("prose-only change — no code reviewers run"), and go to Step 4 with
whatever agents the flags enabled. Otherwise both run.

That is the whole gate. Do not narrow it further by guessing which *kind* of code
changed — a config file, a migration, and a one-line helper are all places a real
finding lives.

**Are there frontend files?** `.tsx`, `.jsx`, `.ts`/`.js` under `components/`,
`app/`, or `pages/`, `.css`, `.scss`, `.vue`, `.svelte`. If any changed, the UI
reviewer runs.

Apply `--reviewers` after these mechanical classifiers: a reviewer runs only if
it is both applicable and selected. This keeps target resolution and frontend
classification in one skill while letting the focused review skills request a
single role.

---

## Step 3: Delegate reviewer roles in parallel

Read each applicable role prompt and delegate all roles concurrently through
the current harness. Use the role prompt as the base and append the context
listed below. If delegation is unavailable, run that review in the current
context. If a required capability such as interactive browser control is
unavailable, report that reviewer as unavailable; do not count it as completed.

### 3a. Security Review (unless prose-only, per Step 2)

Role prompt: `$HOME/.config/agent-config/agents/security-reviewer.md`

Append:
- Changed files with full paths: [LIST]
- Diff for context: [INSERT DIFF]
- Instruction: also read any auth utilities, middleware, or session helpers the changed code interacts with.

### 3b. Simplicity Review (unless prose-only, per Step 2)

Role prompt: `$HOME/.config/agent-config/agents/simplicity-reviewer.md`

Append:
- Changed files with full paths: [LIST]
- Simplicity criteria: `$HOME/.config/agent-config/hickey-principles.md`

### 3c. UI Review (if frontend files changed)

Role prompt: `$HOME/.config/agent-config/agents/ui-reviewer.md`

Append:
- Changed frontend files: [LIST]
- UI criteria: `$HOME/.config/agent-config/ui-design-principles.md`

Skip if no frontend files.

### 3d. QA Review (if `--server <url>` provided)

Role prompt: `$HOME/.config/agent-config/agents/qa-tester.md`

Append:
- Dev server URL: `$SERVER_URL`
- Affected URLs to exercise: [derive from changed route files, or accept from caller]
- Acceptance criteria: [INSERT from `--acceptance-criteria`, or "none provided — exercise the changed surfaces generally"]

Skip if no `--server`.

### 3e. Visual Review (if `--screenshots <dir>` provided)

Role prompt: `$HOME/.config/agent-config/agents/visual-reviewer.md`

Append:
- Screenshot files: [list of paths in `$SCREENSHOTS_DIR`]
- UI criteria: `$HOME/.config/agent-config/ui-design-principles.md`

Skip if no `--screenshots`.

---

## Step 4: Synthesize

Consolidate all findings into a single review.

List each requested reviewer as `completed`, `skipped` (with the rule that
caused the skip), or `unavailable` (with the missing capability). Only completed
reviewers contribute findings or completion counts.

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
