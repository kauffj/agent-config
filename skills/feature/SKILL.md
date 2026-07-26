---
name: feature
description: Build a user-facing feature end-to-end via propose → implement → review → manual-review → ship. Thin orchestrator over /workspace, /propose, and /review-pr.
argument-hint: "<description> | list | resume <name> | complete <name> | abandon <name> | remove <name>"
disable-model-invocation: true
---

# /feature — feature pipeline

Orchestrates the end-to-end flow for shipping a feature. Delegates workspace management, planning, and review to dedicated skills:

- **`/workspace`** — worktree, port, env, install, state tracking
- **`/propose`** — plan + simplicity review + user approval
- **`/review-pr`** — parallel review agents

`/feature` itself owns: implementation agent invocation, screenshots, manual review, commit & push, deploy decision, merge gate on `complete`.

**Feature request:** $ARGUMENTS

---

## Parse command

First, ensure any legacy state is migrated:

```bash
node $HOME/.claude/lib/workspace.mjs migrate
```

Then dispatch on `$ARGUMENTS`:

- empty or `list` → **List features** (filter to kind=feature)
- `resume <name>` → **Resume**
- `complete <name>` → **Complete** (merge-gate + mark done)
- `abandon <name>` → **Abandon**
- `remove <name>` → **Remove**
- otherwise → **New feature** (continue to Step 0)

### List

```bash
node $HOME/.claude/lib/workspace.mjs list --kind feature
```

Render as a table: Name, Status, Step (from `pipeline.step`), Branch, Worktree, Port, Updated.

If empty: "No features tracked. Use `/feature \"description\"` to start one." **Stop.**

### Resume

1. Fetch the record:
   ```bash
   node $HOME/.claude/lib/workspace.mjs get <NAME>
   ```
   **Refuse to advance a finished pipeline.** If the record's `status` is `done`
   (or the branch is already merged), do NOT jump to a step or re-run any agent —
   a compacted resume can land here one step from re-implementing shipped work.
   Say: "Feature '<name>' is already done (status=done, step <pipeline.step>,
   branch `<branch>` merged). Nothing to resume — use `/feature list` to see it or
   `/feature \"…\"` to start new work." **Stop.**
2. If worktree is missing, let `/workspace resume` handle recreation:
   ```
   Invoke /workspace resume <NAME>
   ```
3. Set variables from the record: `$WORKTREE_PATH`, `$PORT`, `$SCREENSHOT_DIR`, `$BRANCH`, `$NAME`.
4. **Load the plan.** Prefer `pipeline.planPath` (new format) and `cat` the file. Fall back to `pipeline.plan` (legacy: plan was stored inline). Set `$PLAN`.
5. Load project profile via `node $HOME/.claude/lib/project.mjs load`.
6. **Jump to the step recorded in `pipeline.step`** — if 4, resume at Step 4; if 6, resume at Step 6; etc.

### Complete

1. Fetch the record. If not found, say so and stop.
2. **Merge gate** — verify the feature branch is merged into the default branch:
   ```bash
   DEFAULT_BRANCH=$(node $HOME/.claude/lib/project.mjs load | jq -r .defaultBranch)
   git fetch origin $DEFAULT_BRANCH
   git branch --merged origin/$DEFAULT_BRANCH | grep -q "<BRANCH>" && echo MERGED || echo NOT_MERGED
   ```
3. If NOT_MERGED, say: "Feature '<name>' branch `<branch>` is not yet merged into `<default>`. Merge or get the PR merged first, then run `/feature complete <name>` again." **Stop.**
4. If MERGED, delegate cleanup to `/workspace done`:
   ```
   Invoke /workspace done <NAME>
   ```
5. Say: "Feature '<name>' is merged and marked as done." **Stop.**

### Abandon

Delegate to `/workspace abandon <NAME>`. **Stop.**

### Remove

Delegate to `/workspace remove <NAME>`. **Stop.**

---

## Step 0: Create workspace

1. **Derive a slug** from `$ARGUMENTS` — short kebab-case, under ~40 chars. Set `$NAME` to this slug.
2. **Invoke** `/workspace new "$ARGUMENTS" --kind feature --name $NAME`. (Passing `--name` explicitly means you already know what the workspace will be called.) `/workspace` provisions an isolated per-workspace database when the project supports it; if the user asked to share the dev DB, append `--db shared`. The feature's DB is dropped automatically when you later `complete`/`abandon` it (those delegate to `/workspace`).
3. **Read the record** back — this is the source of truth:
   ```bash
   node $HOME/.claude/lib/workspace.mjs get $NAME
   ```
   Parse the JSON to get `$BRANCH`, `$WORKTREE_PATH`, `$PORT`, `$SCREENSHOT_DIR`, `$ENV_FILE`.

4. **Load the project profile:**
   ```bash
   node $HOME/.claude/lib/project.mjs load
   ```
   Extract `$BUILD_CMD`, `$DEV_CMD`, `$INSTALL_CMD`, `$STACK`, `$DEPLOY_MODEL`, `$HAS_SCREENSHOTS`, `$DEFAULT_BRANCH`.

Update pipeline progress:

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"pipeline":{"skill":"feature","step":0}}'
```

---

## Step 1: Propose

Invoke `/propose "$ARGUMENTS" --workspace $NAME`. When it returns, the approved plan is at `.workspaces/plans/$NAME.md` and the path is recorded in `pipeline.planPath` on the workspace record.

Read the plan:

```bash
cat .workspaces/plans/$NAME.md
```

(Or, equivalently, read `pipeline.planPath` from the workspace record and `cat` that.)

Capture the contents as `$PLAN`.

Update step:

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"pipeline":{"step":1}}'
```

---

## Step 2: Implement

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"status":"active","pipeline":{"step":2}}'
```

Launch an Agent (subagent_type: "general-purpose"):

> You are a senior full-stack engineer implementing a feature. Follow this approved plan exactly:
>
> [INSERT $PLAN]
>
> **Working directory:** `$WORKTREE_PATH` — all file operations must happen in this directory.
> **Tech stack:** $STACK
>
> **Implementation rules:**
> - Follow existing codebase patterns and conventions — match what's already there
> - Use the project's existing ORM/query approach for any DB queries
> - Use the project's existing CSS/styling approach
> - Handle empty, loading, and error states for any new UI
> - Do NOT add unnecessary abstractions, comments, or over-engineering
>
> **Verification (required before returning):**
> 1. Run `cd $WORKTREE_PATH && $BUILD_CMD` and confirm zero errors. If there are build errors, fix them.
> 2. Start the dev server (`cd $WORKTREE_PATH && PORT=$PORT $DEV_CMD`) and verify each affected URL loads without errors at `http://localhost:$PORT/...`. Check the terminal output for server-side errors.
> 3. Walk through the acceptance criteria from the plan and confirm each one is met. If any aren't met, fix the implementation.
>
> **Output:**
> 1. **Changed files** — list of all files created or modified (paths relative to worktree root)
> 2. **Affected URLs** — every route whose UI changed, as full clickable URLs (e.g. `http://localhost:$PORT/events`, `http://localhost:$PORT/events/123`). Include both new routes AND existing routes whose rendered output changed. For dynamic routes (e.g. `/events/[id]`), substitute a real ID from the dev database so the URL is directly visitable. If a route requires specific state (e.g. logged-in user, specific role), note that next to the URL.
> 3. **Decisions made** — anything you chose during implementation that wasn't specified in the plan (and why)
> 4. **Verification results** — confirm build passed, URLs load, and acceptance criteria met

Capture `$CHANGED_FILES` and `$AFFECTED_URLS`.

Print the affected URLs immediately to the user:

> **Implementation complete.** Affected URLs (visit these to see what changed):
> [LIST EVERY AFFECTED URL AS A FULL LINK]

If more than 20 URLs, group by route pattern with representative examples.

---

## Step 3: Screenshots

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"pipeline":{"step":3}}'
```

Skip this step if `$HAS_SCREENSHOTS` is false.

Ensure the dev server is running:

```bash
: "${WORKTREE_PATH:?set WORKTREE_PATH first}" "${PORT:?PORT is empty}"
cd "$WORKTREE_PATH" && PORT=$PORT $DEV_CMD &
for i in $(seq 1 30); do curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT && break || sleep 1; done
```

Run the screenshot script with the right auth mode for the affected pages (admin / member / public):

```bash
npx tsx scripts/screenshot.ts --output-dir $SCREENSHOT_DIR [--user EMAIL | --no-auth] [AFFECTED_URLS]
```

If the script fails, diagnose and fix before proceeding.

Read the screenshot images from `$SCREENSHOT_DIR` so they're available for the visual review.

---

## Step 4: Review

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"status":"active","pipeline":{"step":4}}'
```

Invoke `/review-pr` over the worktree, with server + screenshots so QA and visual agents run:

```
Invoke: /review-pr --worktree $WORKTREE_PATH --server http://localhost:$PORT --screenshots $SCREENSHOT_DIR --acceptance-criteria "<from $PLAN>"
```

Capture the `REVIEW_SUMMARY` counts and the full feedback (MUST FIX / SHOULD FIX / CONSIDER sections).

---

## Step 5: Revise

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"pipeline":{"step":5}}'
```

Launch an Agent to address the review feedback:

> You are revising a feature implementation based on review feedback.
>
> **Working directory:** `$WORKTREE_PATH`
>
> Here is the consolidated feedback from `/review-pr`:
>
> [INSERT FULL REVIEW OUTPUT]
>
> Rules:
> - **MUST FIX**: Address all of these. No exceptions.
> - **SHOULD FIX**: Address by default. Only skip with a strong documented reason.
> - **CONSIDER**: Document your decision; these are optional.
>
> Make the changes, then:
> 1. Run `cd $WORKTREE_PATH && $BUILD_CMD` and confirm zero errors.
> 2. Output a summary of what you changed and any CONSIDER items you chose not to address (with reasoning).
> 3. List any files where you changed CSS, JS, or view-related code.

If `$HAS_SCREENSHOTS` is true and view-related code changed, re-run the screenshot script on affected URLs so the manual review shows the latest.

---

## Step 6: Manual review

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"status":"active","pipeline":{"step":6}}'
```

Ensure the dev server is still running (start if not). Wait for it to respond.

Present to the user via `AskUserQuestion`:

> **Ready for manual review!**
>
> The dev server is running at **http://localhost:$PORT**
>
> **Pages to review** (every UI that changed):
> [LIST EVERY AFFECTED URL]
>
> **What was built:** [SUMMARY FROM $PLAN]
>
> **Automated review summary:**
> - MUST FIX: [count] found, all resolved
> - SHOULD FIX: [count] found, [count] resolved
> - CONSIDER: [count] suggestions ([count] addressed, [count] skipped)
>
> Reply:
> - **"approved"** → proceed to commit & push
> - **anything else** → I'll make changes and re-present

If the user provides feedback:
1. Launch an Agent to address it, working in `$WORKTREE_PATH`.
2. Run `$BUILD_CMD` to verify build still passes.
3. Loop back to the top of this step.

Max 3 feedback rounds. After 3, ask the user whether to proceed as-is or keep iterating.

---

## Step 7: Commit & push

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"pipeline":{"step":7}}'
```

```bash
: "${WORKTREE_PATH:?set WORKTREE_PATH first}" "${BRANCH:?BRANCH is empty}"
cd "$WORKTREE_PATH"
git add <specific files...>
git commit -m "$(cat <<'EOF'
feat: <short description>

<Brief summary of what was built and why.>
EOF
)"
git push -u origin $BRANCH
```

---

## Step 8: Deploy decision

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME '{"pipeline":{"step":8}}'
```

Ask the user via `AskUserQuestion`:

> Feature branch `$BRANCH` has been pushed.
>
> **Deploy model:** $DEPLOY_MODEL
>
> What would you like to do?
> 1. **Create a PR** — open a pull request for review
> 2. **Merge directly** — merge to `$DEFAULT_BRANCH` (triggers deploy)
> 3. **Skip** — leave the branch alone

**Option 1 (PR):**
```bash
: "${WORKTREE_PATH:?set WORKTREE_PATH first}"
cd "$WORKTREE_PATH"
gh pr create --title "feat: <short>" --body "$(cat <<'EOF'
## Summary
<bullets from plan>

## Review findings addressed
- <count> MUST FIX resolved
- <count> SHOULD FIX resolved
- <count> CONSIDER (addressed/skipped)

## Test plan
- [ ] <acceptance criteria>
EOF
)"
```
Set `$DEPLOY_CHOICE=pr`.

**Option 2 (merge):**
```bash
: "${WORKTREE_PATH:?set WORKTREE_PATH first}" "${BRANCH:?BRANCH is empty}" "${DEFAULT_BRANCH:?DEFAULT_BRANCH is empty}"
cd "$WORKTREE_PATH"
git checkout $DEFAULT_BRANCH
git merge $BRANCH --no-ff -m "Merge $BRANCH"
git push origin $DEFAULT_BRANCH
```
Set `$DEPLOY_CHOICE=merged`.

**Option 3 (skip):** Set `$DEPLOY_CHOICE=skipped`.

---

## Step 9: Report

Map `$DEPLOY_CHOICE` to a final status:
- `merged` → call `/workspace done $NAME`, status becomes `done`
- `pr` → leave workspace `active`, pipeline.step=9, note `pr-open` in description
- `skipped` → leave workspace `active`, pipeline.step=9

Update:

```bash
node $HOME/.claude/lib/workspace.mjs update $NAME "$(jq -n --arg c "$DEPLOY_CHOICE" '{pipeline: {step: 9, deployChoice: $c}}')"
```

Summarize to the user:

1. What was built
2. Branch name
3. Files changed
4. Review findings (MUST / SHOULD / CONSIDER counts + resolutions)
5. Remaining CONSIDER items with reasoning
6. Deploy status
7. Next steps:
   - **merged** → "Done. Worktree cleaned up."
   - **PR** → "PR is open. Run `/feature complete $NAME` after it merges."
   - **skipped** → "Branch `$BRANCH` is pushed but not merged. Run `/feature complete $NAME` after merging."

Done.
