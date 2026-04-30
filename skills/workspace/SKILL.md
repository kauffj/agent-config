---
name: workspace
description: Manage parallel work items — worktree, port, env copy, install, state tracking. Generic over features, bugs, refactors, spikes.
argument-hint: "new <description> [--kind feature|bug|refactor|spike] | list [--kind <kind>] | resume <name> | abandon <name> | remove <name> | done <name> | get <name>"
disable-model-invocation: true
---

# /workspace — parallel work items

Owns the lifecycle of isolated working environments. Each workspace = git worktree + dev-server port + state record. Callers like `/feature`, `/propose`, and `/review-pr` use these primitives; they don't reimplement them.

**State files** (relative to repo root):
- `.workspaces/workspaces.json` — `{ workspaces: [...] }`
- `.workspaces/project.json` — cached project profile (see `lib/project.mjs`)
- `.workspaces/plans/<slug>.md` — saved plans from `/propose`

On first run after upgrading, the helpers auto-relocate any legacy state from `.claude/` to `.workspaces/`. Old `.claude/features.json` (legacy format) is also transformed.

**Helper:** `$HOME/.claude/lib/workspace.mjs` — all state reads/writes go through this. Do not inline `node -e` state mutations in other skills.

**Record shape:**
```json
{
  "name": "member-event-rsvp",
  "kind": "feature",
  "description": "...",
  "branch": "feature/member-event-rsvp",
  "worktreePath": "/abs/path",
  "port": 3001,
  "envFile": ".env.local",
  "screenshotDir": "/tmp/...",
  "status": "active|done|abandoned",
  "pipeline": { "skill": "feature", "step": 4, "plan": "..." },
  "createdAt": "...",
  "updatedAt": "..."
}
```

Status is one of three: `active`, `done`, `abandoned`. The `pipeline` sub-object is caller-owned (e.g. `/feature` stores its step and approved plan there). `/workspace` doesn't interpret it.

---

## Parse `$ARGUMENTS`

Migrate legacy state first:

```bash
node $HOME/.claude/lib/workspace.mjs migrate
```

Then dispatch on the first word of `$ARGUMENTS`:

- `new <description>` → **Create**
- `list` → **List**
- `get <name>` → **Get**
- `resume <name>` → **Resume**
- `abandon <name>` → **Abandon**
- `remove <name>` → **Remove**
- `done <name>` → **Done**

If `$ARGUMENTS` is empty, run **List**.

---

## Create — `/workspace new <description> [--kind feature|bug|refactor|spike] [--name <slug>] [--from <branch>]`

Parse `--kind` (default `feature`), `--name` (default: derive from description), and `--from` (default: repo's default branch).

1. **Ensure clean working tree.** If uncommitted changes exist, stop and ask the user how to proceed.

2. **Load project profile** (detects + caches on first run):
   ```bash
   node $HOME/.claude/lib/project.mjs load
   ```
   Parse the returned JSON to get `$REPO_NAME`, `$DEFAULT_BRANCH`, `$INSTALL_CMD`, `$ENV_FILE`, etc.

3. **Derive name.** If `--name <slug>` was passed, use it verbatim. Otherwise pick a short kebab-case name from the description (under ~40 chars). `$NAME` = that kebab. Branch prefix follows `--kind`:
   - `feature` → `feature/$NAME`
   - `bug` → `fix/$NAME`
   - `refactor` → `refactor/$NAME`
   - `spike` → `spike/$NAME`

4. **Fetch and create worktree:**
   ```bash
   git fetch origin $DEFAULT_BRANCH
   git worktree add ../$REPO_NAME-$NAME -b $BRANCH origin/${FROM:-$DEFAULT_BRANCH}
   ```
   `$WORKTREE_PATH` = absolute path of the new worktree.

5. **Allocate port** starting at 3000:
   ```bash
   PORT=3000
   while ss -tlnp 2>/dev/null | grep -q ":${PORT} "; do PORT=$((PORT + 1)); done
   ```

6. **Copy env file and rewrite URLs** (if `$ENV_FILE` is set):
   ```bash
   cp $ENV_FILE $WORKTREE_PATH/$ENV_FILE
   sed -i "s|^APP_URL=.*|APP_URL=http://localhost:$PORT|" $WORKTREE_PATH/$ENV_FILE
   sed -i "s|^BASE_URL=.*|BASE_URL=http://localhost:$PORT|" $WORKTREE_PATH/$ENV_FILE
   sed -i "s|^NEXTAUTH_URL=.*|NEXTAUTH_URL=http://localhost:$PORT|" $WORKTREE_PATH/$ENV_FILE
   ```

7. **Install deps** in the worktree:
   ```bash
   cd $WORKTREE_PATH && $INSTALL_CMD
   ```

8. **Record** the workspace:
   ```bash
   node $HOME/.claude/lib/workspace.mjs create "$(jq -n --arg n "$NAME" --arg k "$KIND" --arg d "$DESCRIPTION" --arg b "$BRANCH" --arg w "$WORKTREE_PATH" --argjson p $PORT --arg e "$ENV_FILE" --arg s "/tmp/${REPO_NAME}-screenshots-${NAME}" \
     '{name:$n, kind:$k, description:$d, branch:$b, worktreePath:$w, port:$p, envFile:$e, screenshotDir:$s, status:"active"}')"
   ```

9. **Report.** Tell the user the workspace name, worktree path, and port. Callers that need to consume the record programmatically should read it back with `node $HOME/.claude/lib/workspace.mjs get <NAME>` — that's the source of truth.

---

## List — `/workspace list [--kind <kind>]`

```bash
node $HOME/.claude/lib/workspace.mjs list ${KIND:+--kind $KIND}
```

Format the result as a table:

| Name | Kind | Status | Branch | Worktree | Port | Updated |
|------|------|--------|--------|----------|------|---------|

If empty, say: "No workspaces tracked. Use `/workspace new \"description\"` to start one."

---

## Get — `/workspace get <name>`

Print the raw record:

```bash
node $HOME/.claude/lib/workspace.mjs get <NAME>
```

Returns non-zero if not found.

---

## Resume — `/workspace resume <name>`

1. Fetch the record:
   ```bash
   node $HOME/.claude/lib/workspace.mjs get <NAME>
   ```
   If not found: "No workspace named '<name>'." **Stop.**

2. Check the worktree still exists:
   ```bash
   test -d "<worktreePath>" && echo EXISTS || echo MISSING
   ```

3. If MISSING, check whether the branch exists:
   ```bash
   git branch --list "<branch>"
   ```
   - Branch exists → recreate worktree:
     ```bash
     git worktree add "<worktreePath>" "<branch>"
     cd "<worktreePath>" && $INSTALL_CMD
     ```
   - Branch gone → say: "Both worktree and branch are gone for '<name>'. Consider `/workspace remove <name>`." **Stop.**

4. Tell the user the workspace is ready. Callers read the record back with `node $HOME/.claude/lib/workspace.mjs get <NAME>`.

---

## Abandon — `/workspace abandon <name>`

1. Remove worktree if it exists:
   ```bash
   git worktree remove "<worktreePath>" --force
   ```

2. Update status:
   ```bash
   node $HOME/.claude/lib/workspace.mjs update <NAME> '{"status":"abandoned","worktreePath":null}'
   ```

Say: "Workspace '<name>' abandoned and worktree cleaned up."

---

## Remove — `/workspace remove <name>`

1. Remove worktree if it exists (same as abandon step 1).
2. Delete the record:
   ```bash
   node $HOME/.claude/lib/workspace.mjs remove <NAME>
   ```

Say: "Workspace '<name>' removed from tracking."

---

## Done — `/workspace done <name>`

Marks a workspace as shipped. **Does not verify anything** — the caller is responsible for confirming it's actually done (e.g. `/feature` verifies the branch is merged before calling this).

1. Remove worktree if it exists.
2. Update status:
   ```bash
   node $HOME/.claude/lib/workspace.mjs update <NAME> '{"status":"done","worktreePath":null}'
   ```

Say: "Workspace '<name>' marked as done. Worktree cleaned up."
