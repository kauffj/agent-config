---
name: workspace
description: Manage isolated parallel work items — each a git worktree + dev-server port + copied env + isolated DB + state record. Use when starting work that should live off the main checkout (a feature, bug fix, refactor, or spike on its own branch), when resuming or listing in-flight work items, or when finishing/abandoning/removing one and tearing down its resources. Also the primitive layer the feature, propose, and review-pr skills use instead of reimplementing worktree, port, or DB setup. Subcommands — new, list, get, resume, done, abandon, remove.
---

# Parallel workspaces

Owns the lifecycle of isolated working environments. Each workspace = git worktree + dev-server port + state record. The `feature`, `propose`, and `review-pr` skills use these primitives; they don't reimplement them.

**State files** (relative to repo root):
- `.workspaces/workspaces.json` — `{ workspaces: [...] }`
- `.workspaces/project.json` — derived project-profile cache (see `lib/project.mjs`)
- `.workspaces/plans/<slug>.md` — saved plans from `propose`
- `.workspaces/worktrees/<name>/` — the worktrees themselves

Worktrees live INSIDE the repo (not as `../<repo>-<name>` siblings — those bloated `~/projects`). The whole `.workspaces/` dir is kept out of git via `.git/info/exclude`, not `.gitignore`: an exclude entry needs no commit, and on self-deploying projects a `.gitignore` commit pushed to main would deploy. The dot-prefixed path also keeps `tsc`/lint wildcard globs in the main checkout from descending into nested worktrees.

On first run after upgrading, the helpers auto-relocate any legacy state from `.claude/` to `.workspaces/`. Old `.claude/features.json` (legacy format) is also transformed.

**Helper:** `$HOME/.config/agent-config/lib/workspace.mjs` — all state reads/writes and
direct-delivery Git mutations go through this. Do not inline state mutations or
reimplement its integrate/publish/verify/finish guards in other skills.

**Record shape:**
```json
{
  "name": "member-event-rsvp",
  "kind": "feature",
  "description": "...",
  "branch": "feature/member-event-rsvp",
  "worktreePath": "/abs/path/to/repo/.workspaces/worktrees/member-event-rsvp",
  "port": 3001,
  "envFile": ".env.local",
  "screenshotDir": "/tmp/...",
  "status": "active|done|abandoned",
  "dbName": "myapp_dev_ws_member_event_rsvp",
  "dbIsolation": "template|none",
  "dbTemplate": "myapp_dev",
  "dbUrlVar": "DATABASE_URL",
  "dbEndpoint": { "transport": "tcp", "host": "localhost", "port": "5432", "user": "dev", "database": "myapp_dev" },
  "pipeline": { "skill": "feature", "step": 4, "plan": "..." },
  "delivery": { "defaultBranch": "main", "integratedSha": "...", "deploySha": "...", "deliveryVerified": false },
  "createdAt": "...",
  "updatedAt": "..."
}
```

Status is one of three: `active`, `done`, `abandoned`. The `pipeline` sub-object
is caller-owned (e.g. `feature` stores its step and approved plan there).
Generic integrate/publish/verify helpers own only semantic facts in the separate
`delivery` sub-object; they never infer or write a caller's numeric step.

The `db*` fields bind an isolated per-workspace database to the declared primary
URL variable and template it was cloned from so the shared helper can validate
teardown. `dbName` is null when no DB was provisioned (`dbIsolation: "none"`,
or `--db shared`).

**No connection string is ever stored in the record.** The maintenance URL used
to drop the database carries the dev password, and this record is printed
verbatim by the `workspace get` operation — into the transcript, and from there into anything
that exports one. It is derived on demand from the recorded env file and primary
variable in the main checkout. Provisioning stores the credential-free identity
before creating the database, so every later failure remains recoverable through
the record.

---

## Parse the request

Treat the text supplied with the invocation as the request arguments.

Bootstrap ignored runtime directories and migrate legacy state first:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" bootstrap
```

Then dispatch on the first word of the request arguments:

- `new <description>` → **Create**
- `list` → **List**
- `get <name>` → **Get**
- `resume <name>` → **Resume**
- `abandon <name>` → **Abandon**
- `remove <name>` → **Remove**
- `done <name>` → **Done**

If the request arguments are empty, run **List**.

---

## Create — `new <description> [--kind feature|bug|refactor|spike] [--name <slug>] [--from <branch>] [--db shared]`

Parse `--kind` (default `feature`), `--name` (default: derive from description), `--from` (default: repo's default branch), and `--db` (default `isolated`; pass `--db shared` to skip per-workspace DB provisioning and keep the shared dev DB).

1. **Load the project profile** (detects + caches on first run). Runtime state
   was already excluded before migration, so this never dirties the checkout:
   ```bash
   node "$HOME/.config/agent-config/lib/project.mjs" load
   ```
   Parse the returned JSON to get `$REPO_NAME`, `$INSTALL_CMD`, `$ENV_FILE`, and the DB fields `$DB_ISOLATION` (`dbIsolation`), `$DB_TEMPLATE` (`dbTemplate`), `$DB_PRIMARY_URL_VAR` (`dbPrimaryUrlVar`), and `$DB_URL_VARS` (`dbUrlVars`, space-joined), etc. Resolve the authoritative remote default separately with the workspace helper when needed.

2. **Derive name.** If `--name <slug>` was passed, use it verbatim. Otherwise pick a short kebab-case name from the description (under ~40 chars). `$NAME` = that kebab. Branch prefix follows `--kind`:
   - `feature` → `feature/$NAME`
   - `bug` → `fix/$NAME`
   - `refactor` → `refactor/$NAME`
   - `spike` → `spike/$NAME`

3. **Resolve the actual remote base, then allocate and record the intended
   workspace before creating resources.** Run from the main checkout's root.
   An explicit `--from` overrides the remote default:
   ```bash
   REMOTE_DEFAULT=$(node "$HOME/.config/agent-config/lib/workspace.mjs" default-branch | jq -r .defaultBranch)
   FROM=${FROM:-$REMOTE_DEFAULT}
   WORKTREE_PATH="$(pwd)/.workspaces/worktrees/$NAME"
   case "$NAME" in (*[!a-z0-9-]*|'') echo "ABORTED: workspace name must be kebab-case." >&2; exit 1;; esac
   git check-ref-format --branch "$BRANCH" >/dev/null
   git check-ref-format --branch "$FROM" >/dev/null
   [ ! -e "$WORKTREE_PATH" ]
   ! git show-ref --verify --quiet "refs/heads/$BRANCH"
   PORT=$(node "$HOME/.config/agent-config/lib/workspace.mjs" port "$WORKTREE_PATH" | jq -r .port)
   node "$HOME/.config/agent-config/lib/workspace.mjs" create "$(jq -n \
     --arg n "$NAME" --arg k "$KIND" --arg d "$DESCRIPTION" \
     --arg b "$BRANCH" --arg w "$WORKTREE_PATH" --argjson p "$PORT" \
     --arg e "$ENV_FILE" --arg s "/tmp/${REPO_NAME}-screenshots-${NAME}" \
     '{name:$n,kind:$k,description:$d,branch:$b,worktreePath:$w,port:$p,
       envFile:$e,screenshotDir:$s,status:"active",dbName:null,
       dbIsolation:"none",dbTemplate:null,dbUrlVar:null,dbEndpoint:null}')"
   ```
   This provisional record is intentional: a crash or ordinary command failure
   from here onward leaves enough identity for `workspace remove <NAME>` to
   clean up. If any remaining Create command fails, invoke that cleanup
   immediately before reporting the failure.

4. **Fetch and create the worktree from that exact remote ref:**
   ```bash
   if ! git fetch origin "+refs/heads/$FROM:refs/remotes/origin/$FROM" ||
      ! git worktree add "$WORKTREE_PATH" -b "$BRANCH" "origin/$FROM"; then
     node "$HOME/.config/agent-config/lib/workspace.mjs" remove "$NAME"
     echo "ABORTED: could not create workspace '$NAME'." >&2
     exit 1
   fi
   ```
   Do **not** scan upward from 3000. That asked `ss` what was *listening*, which
   cannot see a workspace whose dev server is stopped — its port is still baked
   into its `.env`, so the scan handed the same number to two workspaces and the
   collision only appeared later, as a bind failure. `port` reads the claimed
   ports out of the state file and bind-tests the candidate.

5. **Copy env file and rewrite URLs** (if `$ENV_FILE` is set):
   ```bash
   : "${WORKTREE_PATH:?set WORKTREE_PATH before copying env}"
   if [ -n "$ENV_FILE" ] && {
     ! cp "$ENV_FILE" "$WORKTREE_PATH/$ENV_FILE" ||
     ! sed -i "s|^APP_URL=.*|APP_URL=http://localhost:$PORT|" "$WORKTREE_PATH/$ENV_FILE" ||
     ! sed -i "s|^BASE_URL=.*|BASE_URL=http://localhost:$PORT|" "$WORKTREE_PATH/$ENV_FILE" ||
     ! sed -i "s|^NEXTAUTH_URL=.*|NEXTAUTH_URL=http://localhost:$PORT|" "$WORKTREE_PATH/$ENV_FILE";
   }; then
     node "$HOME/.config/agent-config/lib/workspace.mjs" remove "$NAME"
     echo "ABORTED: could not prepare the workspace env file for '$NAME'." >&2
     exit 1
   fi
   ```

   **Isolated database.** Resolve the mode once: use `$DB_ISOLATION` from the profile, but if `--db shared` was passed (or `$DB_ISOLATION` is `none`, or `$ENV_FILE` is empty), set `DB_NAME=""` and skip the rest of this step — the worktree keeps its shared connection URLs and the provisional record's database fields stay null.

   Otherwise (`$DB_ISOLATION` is `template`), clone the dev DB into a per-workspace database and repoint the env at it:
   ```bash
   : "${DB_TEMPLATE:?dbTemplate is empty}" "${DB_PRIMARY_URL_VAR:?dbPrimaryUrlVar is empty}" "${DB_URL_VARS:?dbUrlVars is empty}"
   # Per-workspace DB name: <template>_ws_<name>, sanitized + truncated to 63 chars.
   DB_NAME=$(printf '%s_ws_%s' "$DB_TEMPLATE" "$NAME" | tr -c 'a-zA-Z0-9_' '_' | cut -c1-63)
   if node "$HOME/.config/agent-config/lib/workspace.mjs" clone-database \
        "$NAME" "$DB_PRIMARY_URL_VAR" "$DB_TEMPLATE" "$DB_NAME" &&
      node "$HOME/.config/agent-config/lib/workspace.mjs" rewrite-env-db \
        "$WORKTREE_PATH/$ENV_FILE" "$DB_NAME" $DB_URL_VARS; then
     : # The helper already persisted the database identity in the record.
   else
     node "$HOME/.config/agent-config/lib/workspace.mjs" remove "$NAME"
     echo "ABORTED: could not create a validated local isolated database for '$NAME'." >&2
     echo "Fix the local endpoint or retry deliberately without isolation:" >&2
     echo "  workspace new \"$DESCRIPTION\" --name $NAME --db shared" >&2
     exit 1
   fi
   ```
   The helper reads the profile's declared primary URL variable and rejects
   non-local endpoints, libpq target overrides, unsafe identifiers, and
   inherited `PG*` target variables. It records the credential-free identity,
   retries once after terminating only idle template connections, and leaves
   teardown enough information to refuse ordinary local cluster drift.

6. **Install deps** in the worktree. `$INSTALL_CMD` is `null` for a project
   with no dependency step — skip this rather than inventing one:
   ```bash
   : "${WORKTREE_PATH:?set WORKTREE_PATH before installing}"
   if [ "$INSTALL_CMD" != "null" ] && ! (cd "$WORKTREE_PATH" && $INSTALL_CMD); then
     node "$HOME/.config/agent-config/lib/workspace.mjs" remove "$NAME"
     echo "ABORTED: dependency installation failed for '$NAME'; resources were cleaned up." >&2
     exit 1
   fi
   ```

7. **Read back the completed record.** This is a verification step, not the
   first persistence point:
   ```bash
   node "$HOME/.config/agent-config/lib/workspace.mjs" get "$NAME"
   ```

8. **Report.** Tell the user the workspace name, worktree path, and port. Callers that need to consume the record programmatically should read it back with `node "$HOME/.config/agent-config/lib/workspace.mjs" get <NAME>` — that's the source of truth.

---

## List — `list [--kind <kind>]`

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" list ${KIND:+--kind $KIND}
```

Format the result as a table:

| Name | Kind | Status | Branch | Worktree | Port | Updated |
|------|------|--------|--------|----------|------|---------|

If empty, say: "No workspaces tracked. Invoke `workspace` with `new \"description\"` to start one."

---

## Get — `get <name>`

Print the raw record:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" get <NAME>
```

Returns non-zero if not found.

---

## Resume — `resume <name>`

1. Fetch the record **and bind the variables step 3 uses.** The block below
   writes `$PORT`/`$ENV_FILE`/`$DB_NAME` into the restored env file; printing the
   record does not set them, and an unset `$PORT` silently writes
   `APP_URL=http://localhost:` — same shape as the teardown block further down:
   ```bash
   REC=$(node "$HOME/.config/agent-config/lib/workspace.mjs" get <NAME>)
   PORT=$(echo "$REC" | jq -r '.port // empty')
   ENV_FILE=$(echo "$REC" | jq -r '.envFile // empty')
   DB_NAME=$(echo "$REC" | jq -r '.dbName // empty')
   PROFILE=$(node "$HOME/.config/agent-config/lib/project.mjs" load)
   DB_URL_VARS=$(echo "$PROFILE" | jq -r '.dbUrlVars // [] | join(" ")')
   INSTALL_CMD=$(echo "$PROFILE" | jq -r '.installCmd // "null"')
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
       [ -n "$DB_NAME" ] && node "$HOME/.config/agent-config/lib/workspace.mjs" rewrite-env-db "<worktreePath>/$ENV_FILE" "$DB_NAME" $DB_URL_VARS
     fi
     [ "$INSTALL_CMD" = "null" ] || (cd "<worktreePath>" && $INSTALL_CMD)
     ```
   - Branch gone → say: "Both worktree and branch are gone for '<name>'. Consider invoking `workspace` with `remove <name>`." **Stop.**

4. Tell the user the workspace is ready. Callers read the record back with `node "$HOME/.config/agent-config/lib/workspace.mjs" get <NAME>`.

   **Do not provision a database on resume.** The workspace's `dbName` already exists from Create — never re-run `CREATE DATABASE`. The env file is restored in step 3 above (it is gitignored, so a recreated worktree never carries it from the branch), and that restore re-points the DB URL at the existing `dbName`. If the worktree env somehow lacks the rewrite, re-apply it against the existing `dbName` only: `node "$HOME/.config/agent-config/lib/workspace.mjs" rewrite-env-db "<worktreePath>/<envFile>" "<dbName>" $DB_URL_VARS`.

---

## Abandon — `abandon <name>`

Run from the main checkout. The shared helper validates the recorded worktree
and database identity, then removes those resources and marks the record
abandoned. This intentionally permits a dirty worktree because abandon means
discarding it:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" abandon <NAME>
```

Say: "Workspace '<name>' abandoned, database and worktree cleaned up."

---

## Remove — `remove <name>`

Run from the main checkout. The same validated teardown removes the database
and worktree, then deletes the state record:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" remove <NAME>
```

Say: "Workspace '<name>' removed from tracking."

---

## Done — `done <name>`

Run this from the main checkout. One idempotent helper validates verified
feature delivery, resolves and fetches the remote's actual default, proves the
local feature ref, remote feature ref, and deployed SHA are all contained in
it, then drops the isolated database and removes the worktree and landed refs.
It refuses before teardown on a dirty/wrong worktree, divergent ref, missing
delivery verification, invalid target, or unavailable database cleanup:

```bash
node "$HOME/.config/agent-config/lib/workspace.mjs" finish <NAME>
```

Say: "Workspace '<name>' marked as done. Database, worktree, and landed feature branch cleaned up."
