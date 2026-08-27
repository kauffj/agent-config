---
name: feature
description: Build and ship a user-facing feature end-to-end — workspace → plan → implement → automated review → screenshots → manual review → direct delivery. Use when asked to build, add, or implement a feature substantial enough to want a branch and a review pass (not a one-line tweak), or to list, resume, complete, abandon, or remove an in-flight feature. Thin orchestrator over the workspace, propose, and review-pr skills; stops for user approval at the plan and manual review, then ships directly.
---

# Feature pipeline

Orchestrates the end-to-end flow for shipping a feature. Delegates workspace management, planning, and review to dedicated skills:

- **`workspace`** — worktree, port, env, install, state tracking
- **`propose`** — plan + simplicity review + user approval
- **`review-pr`** — parallel review agents

The `feature` skill itself owns implementation delegation, screenshots, manual review, commit, direct-to-default shipping, delivery monitoring, and the completion gate.

Treat the text supplied with the invocation as the request arguments. Bind its
freeform feature description as `DESCRIPTION` after removing any subcommand and
flags described below.

---

## Parse command

First, ensure any legacy state is migrated:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" bootstrap
```

Then dispatch on the request arguments:

- empty or `list` → **List features** (filter to kind=feature)
- `resume <name>` → **Resume**
- `complete <name>` → **Complete** (delivery gate + mark done)
- `abandon <name>` → **Abandon**
- `remove <name>` → **Remove**
- otherwise → **New feature** (continue to Step 0)

### List

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" list --kind feature
```

Render as a table: Name, Status, Step (from `pipeline.step`), Branch, Worktree, Port, Updated.

If empty: "No features tracked. Invoke `feature` with a description to start one." **Stop.**

### Resume

1. Fetch the record:
   ```bash
   node "$HOME/.config/agent-config/lib/workspace.mjs" get <NAME>
   ```
   **Refuse to advance a finished pipeline.** If the record's `status` is `done`,
   do NOT jump to a step or re-run any agent —
   a compacted resume can land here one step from re-implementing shipped work.
   Say: "Feature '<name>' is already done (status=done, step <pipeline.step>,
   branch `<branch>` delivered). Nothing to resume — invoke `feature` with `list` to see it or
   invoke `feature` with a description to start new work." **Stop.**
2. If `pipeline.step == 9` and `delivery.deliveryVerified == true`, do not
   recreate a missing worktree. Apply the `workspace` skill with `done <NAME>`
   directly; its
   finish operation is idempotent across partial cleanup. Propagate a refusal or,
   on success, report completion and **stop**.
3. If the worktree is missing, let the `workspace` skill's `resume` operation handle recreation:
   ```
   Apply the `workspace` skill with `resume <NAME>`.
   ```
4. Set variables from the record: `$WORKTREE_PATH`, `$PORT`, `$SCREENSHOT_DIR`,
   `$BRANCH`, `$NAME`, and the semantic delivery fields `$DEPLOY_SHA` and
   `$DELIVERY_VERIFIED`. An active branch already contained
   in the default branch is not terminal: resume Step 8/9 to verify delivery and
   clean up.
5. **Load the plan.** Prefer `pipeline.planPath` (new format) and `cat` the file. Fall back to `pipeline.plan` (legacy: plan was stored inline). Set `$PLAN`.
6. Load the project profile via `node "$HOME/.config/agent-config/lib/project.mjs" load`.
7. **Jump to the step recorded in `pipeline.step`** — if 4, resume at Step 4; if 6, resume at Step 6; etc.

### Complete

1. Fetch the record. If not found, say so and stop.
2. Apply the `workspace` skill with `done <NAME>`; its delivery,
   remote-default ancestry, and clean-worktree
   checks are the authoritative completion gate:
   ```
   Apply the `workspace` skill with `done <NAME>`.
   ```
3. Propagate any refusal without tearing down or claiming completion. On
   success say: "Feature '<name>' is delivered and marked as done." **Stop.**

### Abandon

Apply the `workspace` skill with `abandon <NAME>`. **Stop.**

### Remove

Apply the `workspace` skill with `remove <NAME>`. **Stop.**

---

## Step 0: Create workspace

1. **Derive a slug** from `DESCRIPTION` — short kebab-case, under ~40 chars. Set `$NAME` to this slug.
2. **Apply the `workspace` skill** with `new "<DESCRIPTION>" --kind feature --name <NAME>`. Passing `--name` explicitly means the name is already known. The workspace skill provisions an isolated per-workspace database when the project supports it; if the user asked to share the dev DB, append `--db shared`. The database is dropped automatically when the feature is completed or abandoned.
3. **Read the record** back — this is the source of truth:
   ```bash
   node "$HOME/.config/agent-config/lib/workspace.mjs" get "$NAME"
   ```
   Parse the JSON to get `$BRANCH`, `$WORKTREE_PATH`, `$PORT`, `$SCREENSHOT_DIR`, `$ENV_FILE`.

4. **Load the project profile:**
   ```bash
   node "$HOME/.config/agent-config/lib/project.mjs" load
   ```
   Extract `$BUILD_CMD`, `$DEV_CMD`, `$INSTALL_CMD`, `$STACK`, `$DEPLOY_MODEL`, and `$HAS_SCREENSHOTS`.

   **A command field can be `null`** — it means the project genuinely has no such
   step (no `build` script, not a Django app). Skip that step rather than
   inventing a command; a `null` is a detected fact, not a detection failure. If
   it is wrong, correct it once in tracked `.agent/project.json` as a plain
   field override (for example `{ "devCmd": "make serve" }`) instead of
   working around it each run. The ignored `.workspaces/project.json` cache is
   derived state and must remain disposable.

Update pipeline progress:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" \
  '{"pipeline":{"skill":"feature","step":0}}'
```

---

## Step 1: Propose

Apply the `propose` skill with `<DESCRIPTION> --workspace <NAME>`. When it
returns, the approved plan is at `.workspaces/plans/$NAME.md` and the path is
recorded in `pipeline.planPath` on the workspace record.

Read the plan:

```bash
cat .workspaces/plans/$NAME.md
```

(Or, equivalently, read `pipeline.planPath` from the workspace record and `cat` that.)

Capture the contents as `$PLAN`.

Update step:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" '{"pipeline":{"step":1}}'
```

---

## Step 2: Implement

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" '{"status":"active","pipeline":{"step":2}}'
```

Delegate implementation to an available general-purpose subagent with this
prompt. If the current harness cannot delegate, perform the same work in the
current context and report that limitation:

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
> 1. If `$BUILD_CMD` is not null, run `cd $WORKTREE_PATH && $BUILD_CMD` and confirm zero errors. If there are build errors, fix them.
> 2. If `$DEV_CMD` is not null, start the dev server as a foreground long-running tool command (`cd "$WORKTREE_PATH" && PORT="$PORT" "$HOME/.config/agent-config/bin/agent-session-server" -- bash -lc "$DEV_CMD"`) and verify each affected URL loads without errors at `http://localhost:$PORT/...`. Use the harness's background/session facility while continuing work; do not append shell `&`. Check the terminal output for server-side errors. If it is null, verify by the project's own test command instead and say so.
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
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" '{"pipeline":{"step":3}}'
```

Skip this step if `$HAS_SCREENSHOTS` is false.

Ensure the dev server is running. If it is not, start this as a foreground
long-running tool command and use the tool's background/session facility while
continuing; do not append shell `&`:

```bash
: "${WORKTREE_PATH:?set WORKTREE_PATH first}" "${PORT:?PORT is empty}"
[ "$DEV_CMD" = "null" ] && { echo "no dev command for this project — skipping server checks"; exit 0; }
cd "$WORKTREE_PATH" && PORT="$PORT" "$HOME/.config/agent-config/bin/agent-session-server" -- bash -lc "$DEV_CMD"
```

Then wait for it from a separate tool call:

```bash
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
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" '{"status":"active","pipeline":{"step":4}}'
```

Apply the `review-pr` skill over the worktree, with server and screenshots so QA and visual reviewers run:

```
Apply `review-pr` with: `--worktree $WORKTREE_PATH --server http://localhost:$PORT --screenshots $SCREENSHOT_DIR --acceptance-criteria "<from $PLAN>"`
```

Capture the `REVIEW_SUMMARY` counts and the full feedback (MUST FIX / SHOULD FIX / CONSIDER sections).

---

## Step 5: Revise

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" '{"pipeline":{"step":5}}'
```

Delegate the revision to an available implementation subagent with this prompt.
If delegation is unavailable, perform it in the current context and report that limitation:

> You are revising a feature implementation based on review feedback.
>
> **Working directory:** `$WORKTREE_PATH`
>
> Here is the consolidated feedback from `review-pr`:
>
> [INSERT FULL REVIEW OUTPUT]
>
> Rules:
> - **MUST FIX**: Address all of these. No exceptions.
> - **SHOULD FIX**: Address by default. Only skip with a strong documented reason.
> - **CONSIDER**: Document your decision; these are optional.
>
> Make the changes, then:
> 1. If `$BUILD_CMD` is not null, run `cd $WORKTREE_PATH && $BUILD_CMD` and confirm zero errors.
> 2. Output a summary of what you changed and any CONSIDER items you chose not to address (with reasoning).
> 3. List any files where you changed CSS, JS, or view-related code.

If `$HAS_SCREENSHOTS` is true and view-related code changed, re-run the screenshot script on affected URLs so the manual review shows the latest.

---

## Step 6: Manual review

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" '{"status":"active","pipeline":{"step":6}}'
```

Ensure the dev server is still running (start if not). Wait for it to respond.

Yield to the user through the current harness with:

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
> - **"approved"** → proceed to commit and ship
> - **anything else** → I'll make changes and re-present

If the user provides feedback:
1. Delegate the requested revision to an available implementation subagent,
   working in `$WORKTREE_PATH`; if delegation is unavailable, do it directly.
2. Run `$BUILD_CMD` (when it is not null) to verify the build still passes.
3. Loop back to the top of this step.

Max 3 feedback rounds. After 3, ask the user whether to proceed as-is or keep iterating.

---

## Step 7: Commit

Direct delivery has no PR body, so a non-trivial commit message is the durable
review record. Preserve what was wrong, why this implementation has its shape,
meaningful rejected alternatives or tradeoffs when they exist, and any
operational fact the next reader would otherwise have to re-derive. Keep a
trivial commit trivial; do not invent alternatives merely to fill a template.

```bash
set -euo pipefail
: "${WORKTREE_PATH:?set WORKTREE_PATH first}" "${BRANCH:?BRANCH is empty}"
MAIN_CHECKOUT=$(git worktree list --porcelain | awk '
  index($0, "worktree ") == 1 { print substr($0, 10); exit }
')
cd "$WORKTREE_PATH"
CURRENT_BRANCH=$(git branch --show-current)
[ "$CURRENT_BRANCH" = "$BRANCH" ] || {
  echo "REFUSED: expected $BRANCH, found ${CURRENT_BRANCH:-detached HEAD}." >&2
  exit 1
}

# Resume-safe: a clean feature HEAD means the commit already succeeded before
# pipeline state was recorded. Otherwise commit only the reviewed file set.
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  git add <specific reviewed files...>
  git commit -m "$(cat <<'EOF'
feat: <short description>

<What was wrong and what changed.>

<Why this shape; meaningful tradeoffs or rejected alternatives, if any.>
EOF
)"
fi

[ -z "$(git status --porcelain --untracked-files=all)" ] || {
  echo "REFUSED: uncommitted or untracked work remains in $WORKTREE_PATH." >&2
  exit 1
}
COMMIT_SHA=$(git rev-parse HEAD)
(cd "$MAIN_CHECKOUT" && node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" \
  "$(jq -n --arg sha "$COMMIT_SHA" '{pipeline:{step:7,commitSha:$sha}}')")
```

---

## Step 8: Merge, push, and monitor CI

Finished work ships directly. Resolve the main checkout first:

```bash
MAIN_CHECKOUT=$(git worktree list --porcelain | awk '
  index($0, "worktree ") == 1 { print substr($0, 10); exit }
')
```

Run the tested delivery helper from the main checkout. It validates the recorded
worktree and branch, resolves the remote's actual default branch, fetches it,
and integrates it without pushing:

```bash
set -euo pipefail
INTEGRATION=$(cd "$MAIN_CHECKOUT" && \
  node "$HOME/.config/agent-config/lib/workspace.mjs" integrate "$NAME")
WORKTREE_PATH=$(echo "$INTEGRATION" | jq -r .worktreePath)
DEFAULT_BRANCH=$(echo "$INTEGRATION" | jq -r .defaultBranch)
INTEGRATED_SHA=$(echo "$INTEGRATION" | jq -r .integratedSha)
(cd "$MAIN_CHECKOUT" && node "$HOME/.config/agent-config/lib/workspace.mjs" \
  update "$NAME" '{"pipeline":{"step":8}}')
```

An integration conflict is a re-plan point: resolve it, rerun automated review
on the integrated tree, and restart this step. Otherwise, run `$BUILD_CMD` when
it is non-null and every automated verification command named in the approved
plan from `$WORKTREE_PATH`. These are hard gates. Then prove the commands left
the same clean commit and publish it with the helper:

```bash
set -euo pipefail
cd "$WORKTREE_PATH"
[ "$BUILD_CMD" = "null" ] || $BUILD_CMD
# Run each approved-plan verification command here. Stop on the first failure.
[ -z "$(git status --porcelain --untracked-files=all)" ]
[ "$(git rev-parse HEAD)" = "$INTEGRATED_SHA" ]
PUBLISHED=$(cd "$MAIN_CHECKOUT" && \
  node "$HOME/.config/agent-config/lib/workspace.mjs" publish "$NAME" "$INTEGRATED_SHA")
DEPLOY_SHA=$(echo "$PUBLISHED" | jq -r .deploySha)
REPOSITORY_ID=$(echo "$PUBLISHED" | jq -r .repositoryId)
```

The helper refuses a stale integration if the default branch advanced before
the push. Restart Step 8 in that case. It intentionally leaves every local
default checkout untouched; publication depends only on the reviewed SHA and
the captured remote default SHA.

Pushing the default branch may trigger CI, deployment, or both. Determine
`$CI_PROVIDER` (`github-actions`, `documented-command`, or `none`) and
`$DEPLOY_APPLICABLE` independently from the project instructions and deployment
reference; do not infer that a green CI run proves deployment. GitHub lookup
failures are hard only when GitHub Actions is the selected CI provider:

```bash
set -euo pipefail
case "${CI_PROVIDER:-none}" in
  github-actions)
    RUNS_JSON='[]'
    LAST_RUN_IDS=""
    STABLE_POLLS=0
    for _ in $(seq 1 60); do
      RUNS_JSON=$(gh run list --event push --branch "$DEFAULT_BRANCH" --commit "$DEPLOY_SHA" \
        --limit 100 --json databaseId,headSha,status,conclusion)
      RUN_IDS=$(echo "$RUNS_JSON" | jq -r '.[].databaseId' | sort -n | paste -sd, -)
      if [ -n "$RUN_IDS" ] && [ "$RUN_IDS" = "$LAST_RUN_IDS" ]; then
        STABLE_POLLS=$((STABLE_POLLS + 1))
      else
        STABLE_POLLS=0
      fi
      LAST_RUN_IDS="$RUN_IDS"
      [ "$STABLE_POLLS" -ge 15 ] && break
      sleep 2
    done
    [ "$STABLE_POLLS" -ge 15 ] || {
      echo "REFUSED: expected GitHub Actions runs were absent or never stabilized." >&2
      exit 1
    }
    while read -r RUN_ID; do
      gh run watch "$RUN_ID" --exit-status
    done < <(echo "$RUNS_JSON" | jq -r '.[].databaseId')
    FINAL_RUNS=$(gh run list --event push --branch "$DEFAULT_BRANCH" --commit "$DEPLOY_SHA" \
      --limit 100 --json databaseId,headSha,status,conclusion)
    FINAL_RUN_IDS=$(echo "$FINAL_RUNS" | jq -r '.[].databaseId' | sort -n | paste -sd, -)
    [ "$FINAL_RUN_IDS" = "$LAST_RUN_IDS" ] || {
      echo "REFUSED: a new Actions run appeared after monitoring; repeat the CI gate." >&2
      exit 1
    }
    [ "$(echo "$FINAL_RUNS" | jq --arg sha "$DEPLOY_SHA" '[.[] | select(.headSha != $sha or .status != "completed" or (.conclusion | IN("success", "neutral", "skipped") | not))] | length')" -eq 0 ] || exit 1
    CI_EVIDENCE=$(echo "$FINAL_RUNS" | jq -c \
      '{status:"passed",provider:"github-actions",runs:[.[]|{id:.databaseId,headSha,status,conclusion}]}')
    ;;
  documented-command)
    : "${CI_VERIFY_CMD:?CI is applicable but no GitHub run or documented verification command was found}"
    (cd "$MAIN_CHECKOUT" && bash -lc "$CI_VERIFY_CMD")
    CI_COMMAND_SHA=$(printf '%s' "$CI_VERIFY_CMD" | sha256sum | awk '{print $1}')
    CI_EVIDENCE=$(jq -cn --arg commandSha "$CI_COMMAND_SHA" \
      '{status:"passed",provider:"documented-command",command:("sha256:"+$commandSha),exitStatus:0}')
    ;;
  none)
    CI_EVIDENCE='{"status":"not-applicable","reason":"project has no configured post-push CI"}'
    ;;
  *) echo "REFUSED: unknown CI provider '$CI_PROVIDER'." >&2; exit 1 ;;
esac

if [ "${DEPLOY_APPLICABLE:-false}" = "true" ]; then
  : "${DEPLOY_VERIFY_CMD:?Deployment is applicable but no documented verification command was found}"
  (cd "$MAIN_CHECKOUT" && bash -lc "$DEPLOY_VERIFY_CMD")
  VERIFY_COMMAND_SHA=$(printf '%s' "$DEPLOY_VERIFY_CMD" | sha256sum | awk '{print $1}')
  DEPLOY_EVIDENCE=$(jq -cn --arg commandSha "$VERIFY_COMMAND_SHA" \
    '{status:"passed",command:("sha256:"+$commandSha),exitStatus:0}')
else
  DEPLOY_EVIDENCE='{"status":"not-applicable","reason":"project has no deployment for this push"}'
fi

DELIVERY_EVIDENCE=$(jq -cn --arg repositoryId "$REPOSITORY_ID" \
  --arg defaultBranch "$DEFAULT_BRANCH" --arg deploySha "$DEPLOY_SHA" \
  --argjson ci "$CI_EVIDENCE" --argjson deployment "$DEPLOY_EVIDENCE" \
  '{checkedAt:(now|todateiso8601),repositoryId:$repositoryId,
    defaultBranch:$defaultBranch,deploySha:$deploySha,ci:$ci,deployment:$deployment}')

cd "$MAIN_CHECKOUT"
node "$HOME/.config/agent-config/lib/workspace.mjs" verify-delivery \
  "$NAME" "$DELIVERY_EVIDENCE"
node "$HOME/.config/agent-config/lib/workspace.mjs" update "$NAME" \
  '{"pipeline":{"step":9}}'
```

If CI or deployment fails, diagnose the captured
default-branch run directly in this worktree. Commit the fix, then restart Step
8 at fetch/rebase/verification and push explicitly to the default branch again.
Do not apply the `fix-ci` skill unqualified: it targets the current feature branch and
its plain push contract is incompatible with this flow. The feature is not done
until the default branch is healthy.

---

## Step 9: Report

Apply the `workspace` skill with `done <NAME>`. Its tested helper
independently verifies ancestry and tears down the database, worktree, and
landed feature branch in one idempotent operation:

```
Apply the `workspace` skill with `done <NAME>`.
```

Summarize to the user:

1. What was built
2. Branch name
3. Files changed
4. Review findings (MUST / SHOULD / CONSIDER counts + resolutions)
5. Remaining CONSIDER items with reasoning
6. Delivery result, SHA, and CI/deploy status when shipped
7. "Done. Worktree and landed feature branch cleaned up."

Done.
