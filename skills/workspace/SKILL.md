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
  "dbName": "myapp_dev_ws_member_event_rsvp",
  "dbIsolation": "template|none",
  "dbAdminUrl": "postgresql://user:pass@localhost:5432/postgres",
  "pipeline": { "skill": "feature", "step": 4, "plan": "..." },
  "createdAt": "...",
  "updatedAt": "..."
}
```

Status is one of three: `active`, `done`, `abandoned`. The `pipeline` sub-object is caller-owned (e.g. `/feature` stores its step and approved plan there). `/workspace` doesn't interpret it.

The `db*` fields track an isolated per-workspace database (see Create Step 6 and the shared **Clean up workspace resources** teardown). `dbName` is null when no DB was provisioned (`dbIsolation: "none"`, or `--db shared`). `dbAdminUrl` is the maintenance connection (the `postgres` DB) used to drop the workspace DB at teardown, derived once at create time so teardown never re-parses the env.

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

## Create — `/workspace new <description> [--kind feature|bug|refactor|spike] [--name <slug>] [--from <branch>] [--db shared]`

Parse `--kind` (default `feature`), `--name` (default: derive from description), `--from` (default: repo's default branch), and `--db` (default `isolated`; pass `--db shared` to skip per-workspace DB provisioning and keep the shared dev DB).

1. **Ensure clean working tree.** If uncommitted changes exist, stop and ask the user how to proceed.

2. **Load project profile** (detects + caches on first run):
   ```bash
   node $HOME/.claude/lib/project.mjs load
   ```
   Parse the returned JSON to get `$REPO_NAME`, `$DEFAULT_BRANCH`, `$INSTALL_CMD`, `$ENV_FILE`, and the DB fields `$DB_ISOLATION` (`dbIsolation`), `$DB_TEMPLATE` (`dbTemplate`), and `$DB_URL_VARS` (`dbUrlVars`, space-joined), etc.

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
   : "${WORKTREE_PATH:?set WORKTREE_PATH before copying env}" "${ENV_FILE:?ENV_FILE is empty}"
   cp "$ENV_FILE" "$WORKTREE_PATH/$ENV_FILE"
   sed -i "s|^APP_URL=.*|APP_URL=http://localhost:$PORT|" "$WORKTREE_PATH/$ENV_FILE"
   sed -i "s|^BASE_URL=.*|BASE_URL=http://localhost:$PORT|" "$WORKTREE_PATH/$ENV_FILE"
   sed -i "s|^NEXTAUTH_URL=.*|NEXTAUTH_URL=http://localhost:$PORT|" "$WORKTREE_PATH/$ENV_FILE"
   ```

   **Isolated database.** Resolve the mode once: use `$DB_ISOLATION` from the profile, but if `--db shared` was passed (or `$DB_ISOLATION` is `none`, or `$ENV_FILE` is empty), set `DB_NAME=""` and skip the rest of this step — the worktree keeps the shared `DATABASE_URL` and the record's `dbName`/`dbAdminUrl` stay null.

   Otherwise (`$DB_ISOLATION` is `template`), clone the dev DB into a per-workspace database and repoint the env at it:
   ```bash
   : "${DB_TEMPLATE:?dbTemplate is empty}" "${DB_URL_VARS:?dbUrlVars is empty}"
   # Per-workspace DB name: <template>_ws_<name>, sanitized + truncated to 63 chars.
   DB_NAME=$(printf '%s_ws_%s' "$DB_TEMPLATE" "$NAME" | tr -c 'a-zA-Z0-9_' '_' | cut -c1-63)
   # Admin URL = primary DB URL with the path swapped to the `postgres` maintenance DB.
   PRIMARY_URL=$(grep -E "^\s*DATABASE_URL\s*=" "$ENV_FILE" | head -1 | sed -E 's/^[^=]*=\s*//; s/^["'"'"']//; s/["'"'"']$//')
   DB_ADMIN_URL=$(node -e 'const u=new URL(process.argv[1]); u.pathname="/postgres"; console.log(u.href)' "$PRIMARY_URL")
   ```

   Provision the clone. Postgres refuses `TEMPLATE` while anything else is
   connected to the template, and on a working machine something usually is (a
   dev server on :3000 holds a pool against `$DB_TEMPLATE`). That is the common
   case, not the exception — so terminate IDLE connections and retry once before
   giving up. If it still fails (no `psql`, no `CREATEDB` privilege, an ACTIVE
   query), **do not abort** — warn, fall back to the shared DB, and clear
   `DB_NAME`:
   ```bash
   create_db() {
     psql "$DB_ADMIN_URL" -v ON_ERROR_STOP=1 \
       -c "CREATE DATABASE \"$DB_NAME\" TEMPLATE \"$DB_TEMPLATE\""
   }
   if create_db; then
     node $HOME/.claude/lib/workspace.mjs rewrite-env-db "$WORKTREE_PATH/$ENV_FILE" "$DB_NAME" $DB_URL_VARS
   else
     # Only state='idle' — never kill a running query, and never this session.
     echo "NOTE: retrying after dropping idle connections to $DB_TEMPLATE…"
     psql "$DB_ADMIN_URL" -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
       WHERE datname = '$DB_TEMPLATE' AND pid <> pg_backend_pid() AND state = 'idle'" >/dev/null 2>&1 || true
     if create_db; then
       node $HOME/.claude/lib/workspace.mjs rewrite-env-db "$WORKTREE_PATH/$ENV_FILE" "$DB_NAME" $DB_URL_VARS
     else
       echo "WARN: DB provisioning failed — worktree will use the shared dev DB ($DB_TEMPLATE)."
       DB_NAME=""
     fi
   fi
   ```
   (Terminating an idle pool connection is safe — the dev server reconnects on
   its next query. If it still fails, something holds an ACTIVE query against
   `$DB_TEMPLATE`: stop that process and retry, or proceed with `--db shared`.
   Falling back means the worktree shares the dev DB, so concurrent migrations
   can collide — see the drizzle-cursor failure `npm run db:audit` catches.)

7. **Install deps** in the worktree:
   ```bash
   : "${WORKTREE_PATH:?set WORKTREE_PATH before installing}"
   cd "$WORKTREE_PATH" && $INSTALL_CMD
   ```

8. **Record** the workspace. When a DB was provisioned, include `dbName`/`dbIsolation`/`dbAdminUrl`; otherwise leave them null:
   ```bash
   node $HOME/.claude/lib/workspace.mjs create "$(jq -n --arg n "$NAME" --arg k "$KIND" --arg d "$DESCRIPTION" --arg b "$BRANCH" --arg w "$WORKTREE_PATH" --argjson p $PORT --arg e "$ENV_FILE" --arg s "/tmp/${REPO_NAME}-screenshots-${NAME}" \
     --arg db "$DB_NAME" --arg dba "$DB_ADMIN_URL" \
     '{name:$n, kind:$k, description:$d, branch:$b, worktreePath:$w, port:$p, envFile:$e, screenshotDir:$s, status:"active",
       dbName:(if $db=="" then null else $db end),
       dbIsolation:(if $db=="" then "none" else "template" end),
       dbAdminUrl:(if $db=="" then null else $dba end)}')"
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
   - Branch exists → recreate worktree **and restore its env file**. `$ENV_FILE`
     (e.g. `.env.local`) is gitignored, so the recreated worktree does NOT get one
     from the branch — without this every tool that reads it fails, e.g.
     `drizzle-kit migrate` dies with `DATABASE_URL` undefined. Copy it from the
     PRIMARY checkout (run these there, before `cd`), then re-apply the same
     rewrites Create does — pointing at the workspace's EXISTING `dbName`:
     ```bash
     git worktree add "<worktreePath>" "<branch>"
     if [ -n "$ENV_FILE" ] && [ ! -f "<worktreePath>/$ENV_FILE" ]; then
       cp "$ENV_FILE" "<worktreePath>/$ENV_FILE"
       sed -i "s|^APP_URL=.*|APP_URL=http://localhost:$PORT|" "<worktreePath>/$ENV_FILE"
       sed -i "s|^BASE_URL=.*|BASE_URL=http://localhost:$PORT|" "<worktreePath>/$ENV_FILE"
       sed -i "s|^NEXTAUTH_URL=.*|NEXTAUTH_URL=http://localhost:$PORT|" "<worktreePath>/$ENV_FILE"
       # Re-point at the EXISTING per-workspace DB only; never provision on resume.
       [ -n "$DB_NAME" ] && node $HOME/.claude/lib/workspace.mjs rewrite-env-db "<worktreePath>/$ENV_FILE" "$DB_NAME" $DB_URL_VARS
     fi
     (cd "<worktreePath>" && $INSTALL_CMD)
     ```
   - Branch gone → say: "Both worktree and branch are gone for '<name>'. Consider `/workspace remove <name>`." **Stop.**

4. Tell the user the workspace is ready. Callers read the record back with `node $HOME/.claude/lib/workspace.mjs get <NAME>`.

   **Do not provision a database on resume.** The workspace's `dbName` already exists from Create — never re-run `CREATE DATABASE`. The env file is restored in step 3 above (it is gitignored, so a recreated worktree never carries it from the branch), and that restore re-points the DB URL at the existing `dbName`. If the worktree env somehow lacks the rewrite, re-apply it against the existing `dbName` only: `node $HOME/.claude/lib/workspace.mjs rewrite-env-db "<worktreePath>/<envFile>" "<dbName>" $DB_URL_VARS`.

---

## Clean up workspace resources

Shared teardown used by **Abandon**, **Remove**, and **Done**. Idempotent — safe to run more than once.

1. **Drop the isolated database** if the record has one. Read `dbName`/`dbAdminUrl` from the record (do not re-parse any env file — teardown is a pure function of the stored record):
   ```bash
   REC=$(node $HOME/.claude/lib/workspace.mjs get <NAME>)
   DB_NAME=$(echo "$REC" | jq -r '.dbName // empty')
   DB_ADMIN_URL=$(echo "$REC" | jq -r '.dbAdminUrl // empty')
   if [ -n "$DB_NAME" ] && [ -n "$DB_ADMIN_URL" ]; then
     psql "$DB_ADMIN_URL" -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE);" \
       || echo "WARN: could not drop database $DB_NAME — drop it manually if it lingers."
   fi
   ```
   (`WITH (FORCE)` terminates the worktree dev server's lingering connections; requires PostgreSQL 13+.)

2. **Remove the worktree** if it exists:
   ```bash
   git worktree remove "<worktreePath>" --force
   ```

---

## Abandon — `/workspace abandon <name>`

1. Run **Clean up workspace resources** for `<name>`.
2. Update status:
   ```bash
   node $HOME/.claude/lib/workspace.mjs update <NAME> '{"status":"abandoned","worktreePath":null}'
   ```

Say: "Workspace '<name>' abandoned, database and worktree cleaned up."

---

## Remove — `/workspace remove <name>`

1. Run **Clean up workspace resources** for `<name>`.
2. Delete the record:
   ```bash
   node $HOME/.claude/lib/workspace.mjs remove <NAME>
   ```

Say: "Workspace '<name>' removed from tracking."

---

## Done — `/workspace done <name>`

Marks a workspace as shipped. **Does not verify anything** — the caller is responsible for confirming it's actually done (e.g. `/feature` verifies the branch is merged before calling this).

1. Run **Clean up workspace resources** for `<name>`.
2. Update status:
   ```bash
   node $HOME/.claude/lib/workspace.mjs update <NAME> '{"status":"done","worktreePath":null}'
   ```

Say: "Workspace '<name>' marked as done. Database and worktree cleaned up."
