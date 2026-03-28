---
name: feature
description: Multi-agent feature pipeline with worktree isolation, parallel reviews, and push-to-deploy
argument-hint: "<feature description>" | list | resume <name> | complete <name> | abandon <name> | remove <name>
disable-model-invocation: true
---

# /feature — Multi-Agent Feature Pipeline

Build a feature using specialized review agents. Each feature runs in an isolated git worktree with its own dev server port, so multiple features can be developed simultaneously. Follows a push-to-deploy model: merge to the default branch triggers CI/CD deployment.

**Reference files:**
- `$HICKEY_PRINCIPLES` and `$UI_PRINCIPLES` are set via settings.json env
- Agent prompts are in `$HOME/.claude/agents/`

---

**Usage:**
- `/feature "description of the feature to build"` — start a new feature
- `/feature list` — show all tracked features with status
- `/feature resume <name>` — resume an in-progress feature
- `/feature complete <name>` — mark a feature as complete
- `/feature abandon <name>` — mark as abandoned and clean up worktree
- `/feature remove <name>` — remove from tracking entirely (and clean up worktree)

**Feature request:** $ARGUMENTS

**State file:** `.claude/features.json` (relative to the main repo root)

The state file uses this structure:
```json
{
  "project": {
    "repoName": "...",
    "defaultBranch": "main",
    "pkgMgr": "bun",
    "installCmd": "bun install",
    "buildCmd": "bun run build",
    "devCmd": "bun run dev",
    "stack": "Next.js 16, Drizzle ORM, PostgreSQL, Tailwind CSS",
    "deployModel": "GitHub Actions: push to main triggers deploy",
    "hasScreenshots": true,
    "envFile": ".env.local",
    "detectedAt": "2026-03-24T..."
  },
  "features": [...]
}
```

The `project` key caches project-level detection so it only runs once per repo. Individual features are in the `features` array. When reading/writing feature records, always access `.features` (not the root).

---

## Step -1: Parse Command

Before doing anything else, **migrate the state file** if it uses the old flat-array format:

```bash
if [ -f .claude/features.json ]; then
  node -e "
  const fs = require('fs');
  const p = '.claude/features.json';
  const d = JSON.parse(fs.readFileSync(p,'utf8'));
  if (Array.isArray(d)) {
    fs.writeFileSync(p, JSON.stringify({project: null, features: d}, null, 2));
    console.log('Migrated features.json to new format');
  }
  "
fi
```

Then parse `$ARGUMENTS` to determine the subcommand.

### If `$ARGUMENTS` is empty or is exactly `list`:

Read `.claude/features.json` and display all tracked features (from the `.features` array) in a table format:

| Name | Status | Step | Branch | Worktree | Port | Updated |
|------|--------|------|--------|----------|------|---------|

If there are no features (or the file doesn't exist), say: "No features tracked. Use `/feature \"description\"` to start one."

**Stop here. Do not continue to Step 0.**

### If `$ARGUMENTS` starts with `resume `:

Extract the feature name (everything after `resume `). Read `.claude/features.json` and find the matching feature record.

If not found, say: "No feature named '<name>' found. Run `/feature list` to see tracked features." **Stop here.**

If found:
1. Check if the worktree still exists at the recorded `worktreePath`:
   ```bash
   test -d "<worktreePath>" && echo "EXISTS" || echo "MISSING"
   ```
2. If the worktree is MISSING, check if the branch still exists:
   ```bash
   git branch --list "<branch>"
   ```
   - If the branch exists, recreate the worktree:
     ```bash
     git worktree add "<worktreePath>" "<branch>"
     cd "<worktreePath>" && <INSTALL_CMD>
     ```
   - If the branch is also gone, say: "Both worktree and branch are gone for '<name>'. Consider removing it with `/feature remove <name>`." **Stop here.**
3. Set variables from the feature record:
   - `$WORKTREE_PATH` = `worktreePath`
   - `$PORT` = `port`
   - `$SCREENSHOT_DIR` = `screenshotDir`
   - `$FEATURE_NAME` = `name`
   - `$PLAN` = `plan` (if stored)
4. Load project-level variables from the `.project` key in `features.json`:
   - `$REPO_NAME`, `$DEFAULT_BRANCH`, `$PKG_MGR`, `$INSTALL_CMD`, `$BUILD_CMD`, `$DEV_CMD`, `$STACK_SUMMARY`, `$DEPLOY_MODEL`, `$HAS_SCREENSHOTS`, `$ENV_FILE`
5. **Jump to the step recorded in `step`** (skip all prior steps). If the step is `4` (implementing), resume at Step 4. If `6` (reviewing), resume at Step 6. If `8` (manual-review), resume at Step 8. Etc.

### If `$ARGUMENTS` starts with `complete `:

Extract the feature name. Read `.claude/features.json`, find the matching record.

If not found, say: "No feature named '<name>' found." **Stop here.**

Load the default branch from the project profile (`d.project.defaultBranch`).

**Verify the feature branch is merged into the default branch:**
```bash
git fetch origin $DEFAULT_BRANCH
git branch --merged origin/$DEFAULT_BRANCH | grep -q "feature/<NAME>" && echo "MERGED" || echo "NOT_MERGED"
```

**If NOT_MERGED:** Say: "Feature '<name>' branch `feature/<NAME>` is not yet merged into `$DEFAULT_BRANCH`. Merge or get the PR merged first, then run `/feature complete <name>` again." **Stop here.**

**If MERGED:** Update the record and clean up the worktree:
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '<NAME>');
if (i === -1) { console.log('Not found'); process.exit(1); }
d.features[i].status = 'complete';
d.features[i].updatedAt = new Date().toISOString();
fs.writeFileSync(p, JSON.stringify(d, null, 2));
console.log('Marked', d.features[i].name, 'as complete');
"
```

If the feature has a worktree, clean it up:
```bash
if [ -d "<worktreePath>" ]; then
  git worktree remove "<worktreePath>" --force
fi
```

Say: "Feature '<name>' is merged and marked as complete. Worktree cleaned up." **Stop here.**

### If `$ARGUMENTS` starts with `abandon `:

Extract the feature name. Read `.claude/features.json`, find the matching record.

If not found, say: "No feature named '<name>' found." **Stop here.**

1. If the worktree exists, remove it:
   ```bash
   git worktree remove "<worktreePath>" --force
   ```
2. Update the record:
   ```bash
   node -e "
   const fs = require('fs');
   const p = '.claude/features.json';
   const d = JSON.parse(fs.readFileSync(p,'utf8'));
   const i = d.features.findIndex(x => x.name === '<NAME>');
   if (i === -1) { console.log('Not found'); process.exit(1); }
   d.features[i].status = 'abandoned';
   d.features[i].worktreePath = null;
   d.features[i].updatedAt = new Date().toISOString();
   fs.writeFileSync(p, JSON.stringify(d, null, 2));
   console.log('Abandoned', d.features[i].name);
   "
   ```

Say: "Feature '<name>' abandoned and worktree cleaned up." **Stop here.**

### If `$ARGUMENTS` starts with `remove `:

Extract the feature name. Read `.claude/features.json`, find the matching record.

If not found, say: "No feature named '<name>' found." **Stop here.**

1. If the record has a `worktreePath` and it exists, remove it:
   ```bash
   git worktree remove "<worktreePath>" --force
   ```
2. Remove the record from the array:
   ```bash
   node -e "
   const fs = require('fs');
   const p = '.claude/features.json';
   const d = JSON.parse(fs.readFileSync(p,'utf8'));
   const i = d.features.findIndex(x => x.name === '<NAME>');
   if (i === -1) { console.log('Not found'); process.exit(1); }
   const name = d.features[i].name;
   d.features.splice(i, 1);
   fs.writeFileSync(p, JSON.stringify(d, null, 2));
   console.log('Removed', name);
   "
   ```

Say: "Feature '<name>' removed from tracking." **Stop here.**

### Otherwise (new feature):

Treat `$ARGUMENTS` as a new feature description. Continue to Step 0.

---

## Step 0: Detect Project & Create Worktree

### 0a: Project Detection (cached)

Project characteristics are detected once and cached in `.claude/features.json` under the `project` key. On subsequent features in the same repo, the cached profile is reused.

**First, check for a cached project profile:**

```bash
mkdir -p .claude
if [ -f .claude/features.json ]; then
  node -e "
  const d = JSON.parse(require('fs').readFileSync('.claude/features.json','utf8'));
  if (d.project) { console.log('CACHED'); console.log(JSON.stringify(d.project)); }
  else console.log('NONE');
  "
fi
```

**If CACHED:** Load all variables from the cached `project` object:
- `$REPO_NAME` = `project.repoName`
- `$DEFAULT_BRANCH` = `project.defaultBranch`
- `$PKG_MGR` = `project.pkgMgr`
- `$INSTALL_CMD` = `project.installCmd`
- `$BUILD_CMD` = `project.buildCmd`
- `$DEV_CMD` = `project.devCmd`
- `$STACK_SUMMARY` = `project.stack`
- `$DEPLOY_MODEL` = `project.deployModel`
- `$HAS_SCREENSHOTS` = `project.hasScreenshots`
- `$ENV_FILE` = `project.envFile`

Skip the detection steps below and proceed to **0b: Create Worktree**.

**If NONE (no cached profile):** Run full detection:

1. **Detect repo name** from the git remote or directory name:
   ```bash
   REPO_NAME=$(basename $(git rev-parse --show-toplevel))
   ```

2. **Detect default branch** (main or master):
   ```bash
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")
   ```

3. **Detect package manager**:
   ```bash
   if [ -f "bun.lockb" ] || [ -f "bun.lock" ]; then PKG_MGR="bun"; INSTALL_CMD="bun install"; BUILD_CMD="bun run build"; DEV_CMD="bun run dev"
   elif [ -f "pnpm-lock.yaml" ]; then PKG_MGR="pnpm"; INSTALL_CMD="pnpm install"; BUILD_CMD="pnpm run build"; DEV_CMD="pnpm run dev"
   elif [ -f "yarn.lock" ]; then PKG_MGR="yarn"; INSTALL_CMD="yarn install"; BUILD_CMD="yarn build"; DEV_CMD="yarn dev"
   elif [ -f "package-lock.json" ] || [ -f "package.json" ]; then PKG_MGR="npm"; INSTALL_CMD="npm install"; BUILD_CMD="npm run build"; DEV_CMD="npm run dev"
   elif [ -f "Cargo.toml" ]; then PKG_MGR="cargo"; INSTALL_CMD="cargo build"; BUILD_CMD="cargo build --release"; DEV_CMD="cargo run"
   elif [ -f "go.mod" ]; then PKG_MGR="go"; INSTALL_CMD="go mod download"; BUILD_CMD="go build ./..."; DEV_CMD="go run ."
   elif [ -f "requirements.txt" ] || [ -f "pyproject.toml" ]; then PKG_MGR="pip"; INSTALL_CMD="pip install -r requirements.txt"; BUILD_CMD="echo 'no build step'"; DEV_CMD="python manage.py runserver"
   else PKG_MGR="unknown"; INSTALL_CMD="echo 'no install step'"; BUILD_CMD="echo 'no build step'"; DEV_CMD="echo 'no dev command'"
   fi
   ```

4. **Detect tech stack** by reading key config files. Explore:
   - `package.json` (frameworks, key dependencies)
   - Database config files (drizzle.config.ts, prisma/schema.prisma, knexfile.js, etc.)
   - Framework config (next.config.*, nuxt.config.*, vite.config.*, etc.)
   - CSS approach (tailwind.config.*, postcss.config.*, etc.)
   - Auth setup (any auth library in dependencies)

   Produce a short `$STACK_SUMMARY` string, e.g.: "Next.js 16, Drizzle ORM, PostgreSQL, Tailwind CSS, NextAuth v5"

5. **Detect screenshot capability**: Check if `scripts/screenshot.ts` or `scripts/screenshot.js` exists. Set `$HAS_SCREENSHOTS` to true/false.

6. **Detect env files to copy**: Check for `.env.local`, `.env.development`, or `.env`. Set `$ENV_FILE` to the first one found (or empty if none).

7. **Detect deploy model**: Check for:
   - `.github/workflows/deploy.yml` or similar CI deploy config
   - `Procfile`, `fly.toml`, `vercel.json`, `netlify.toml`, `render.yaml`
   - Makefile with deploy target

   Set `$DEPLOY_MODEL` to a description like "GitHub Actions: push to main triggers deploy via SSH to production server" or "Vercel: auto-deploys on push to main".

**After detection, cache the project profile** (this runs only on first feature in a repo):
```bash
[ -f .claude/features.json ] || echo '{"project":null,"features":[]}' > .claude/features.json
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
// Migrate from old array format if needed
if (Array.isArray(d)) { const features = d; var data = {project: null, features}; } else { var data = d; }
data.project = {
  repoName: '$REPO_NAME',
  defaultBranch: '$DEFAULT_BRANCH',
  pkgMgr: '$PKG_MGR',
  installCmd: '$INSTALL_CMD',
  buildCmd: '$BUILD_CMD',
  devCmd: '$DEV_CMD',
  stack: '$STACK_SUMMARY',
  deployModel: $(printf '%s' '$DEPLOY_MODEL' | node -e \"process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))\"),
  hasScreenshots: $HAS_SCREENSHOTS,
  envFile: '$ENV_FILE',
  detectedAt: new Date().toISOString()
};
fs.writeFileSync(p, JSON.stringify(data, null, 2));
console.log('Project profile cached');
"
```

### 0b: Create Worktree

Set up an isolated working environment for this feature:

1. Ensure the working tree is clean (no uncommitted changes). If there are uncommitted changes, ask the user how to proceed before continuing.
2. Fetch latest default branch:
   ```bash
   git fetch origin $DEFAULT_BRANCH
   ```
3. Derive a branch name: `feature/<short-kebab-case-description>` (under 50 chars total). Example: `feature/member-event-rsvp`
4. Derive the feature short name from the branch: the kebab-case part after `feature/`. Set `$FEATURE_NAME` to this value.
5. Create a worktree with the feature branch:
   ```bash
   git worktree add ../$REPO_NAME-$FEATURE_NAME -b feature/$FEATURE_NAME origin/$DEFAULT_BRANCH
   ```
   Set `$WORKTREE_PATH` to the absolute path of the new worktree.
6. Find the first available port starting from 3000:
   ```bash
   PORT=3000
   while ss -tlnp | grep -q ":${PORT} "; do PORT=$((PORT + 1)); done
   echo $PORT
   ```
   Set `$PORT` to the result.
7. If `$ENV_FILE` is not empty, copy it into the worktree and update any `APP_URL` or `BASE_URL` or `NEXTAUTH_URL` setting:
   ```bash
   cp $ENV_FILE $WORKTREE_PATH/$ENV_FILE
   sed -i "s|APP_URL=.*|APP_URL=http://localhost:$PORT|" $WORKTREE_PATH/$ENV_FILE
   sed -i "s|BASE_URL=.*|BASE_URL=http://localhost:$PORT|" $WORKTREE_PATH/$ENV_FILE
   sed -i "s|NEXTAUTH_URL=.*|NEXTAUTH_URL=http://localhost:$PORT|" $WORKTREE_PATH/$ENV_FILE
   ```
8. Install dependencies in the worktree:
   ```bash
   cd $WORKTREE_PATH && $INSTALL_CMD
   ```
9. Set the screenshot directory:
   ```bash
   SCREENSHOT_DIR="/tmp/${REPO_NAME}-screenshots-${FEATURE_NAME}"
   ```

10. **Track the feature** — add a record to the `features` array in `.claude/features.json`:
    ```bash
    node -e "
    const fs = require('fs');
    const p = '.claude/features.json';
    const d = JSON.parse(fs.readFileSync(p,'utf8'));
    d.features.push({
      name: '$FEATURE_NAME',
      description: $(printf '%s' '$ARGUMENTS' | node -e "process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))"),
      branch: 'feature/$FEATURE_NAME',
      worktreePath: '$WORKTREE_PATH',
      port: $PORT,
      screenshotDir: '$SCREENSHOT_DIR',
      status: 'planning',
      step: 0,
      plan: null,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString()
    });
    fs.writeFileSync(p, JSON.stringify(d, null, 2));
    console.log('Tracking feature:', '$FEATURE_NAME');
    "
    ```

Remember all variables (`$WORKTREE_PATH`, `$PORT`, `$SCREENSHOT_DIR`, `$FEATURE_NAME`, `$DEFAULT_BRANCH`, `$STACK_SUMMARY`, `$DEPLOY_MODEL`, `$BUILD_CMD`, `$DEV_CMD`, `$INSTALL_CMD`, `$HAS_SCREENSHOTS`, `$REPO_NAME`) for all subsequent steps.

---

## Step 1: Plan

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 1; d.features[i].status = 'planning'; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Launch an Agent (subagent_type: "Plan") to explore the codebase and produce a structured plan.

Prompt the agent with:
> You are a senior software architect planning a feature.
>
> **Working directory:** `$WORKTREE_PATH`
> **Tech stack:** $STACK_SUMMARY
> **Deploy model:** $DEPLOY_MODEL
>
> Feature request: "$ARGUMENTS"
>
> **Phase 1 — Describe the current state:**
> Explore the codebase to understand the area this feature touches. Document what exists today — relevant files, patterns, data models, and UI. Do NOT jump to solutions yet.
>
> **Phase 2 — Plan the change:**
> Based on your understanding, produce a plan with:
> 1. **Summary** — one paragraph describing what will be built
> 2. **Approach** — step-by-step implementation plan
> 3. **Files to create/modify** — list each file and what changes
> 4. **DB changes** — any new tables, columns, or migrations (if none, say "None")
> 5. **UI components** — new components needed, following existing patterns (if none, say "None")
> 6. **Affected URLs** — routes that will be added or changed
> 7. **Acceptance criteria** — how will we verify this feature works? List specific things a user should be able to do.
> 8. **Unknowns/risks** — anything you're unsure about or that could go wrong
>
> Be specific. Reference existing patterns you find in the codebase.

Save the plan output to a variable for the next steps.

**Store the plan in tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].plan = $(printf '%s' '$PLAN' | node -e "process.stdout.write(JSON.stringify(require('fs').readFileSync('/dev/stdin','utf8')))"); d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

---

## Step 2: Design Review (Hickey)

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 2; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Launch an Agent to review the PLAN (not code) against simplicity principles.

Prompt the agent with:
> You are a design reviewer focused on simplicity and avoiding complection.
>
> First, read the file at `$HOME/.claude/hickey-principles.md` to load your review criteria.
>
> Then review this plan:
>
> [INSERT PLAN FROM STEP 1]
>
> Evaluate:
> - Is anything being complected that should be separate?
> - Are there easy-over-simple tradeoffs being made?
> - Are there unnecessary abstractions or premature generalizations?
> - Could the approach be simpler while meeting the requirements?
> - Are there any "functions doing two things" in the proposed design?
>
> Output one of:
> - **APPROVED** — the plan is sound, with any minor notes
> - **BLOCKED** — specific issues that must be addressed, with concrete suggestions
>
> Be rigorous but practical. Don't block for theoretical concerns.

If BLOCKED, send the feedback back to the Plan agent (re-run Step 1 with the feedback appended). Maximum 2 re-plan iterations. After 2 rejections, present the situation to the user and ask how to proceed.

---

## Step 3: User Checkpoint

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 3; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Present to the user:
1. The approved plan (from Step 1)
2. The design review notes (from Step 2)
3. The worktree path (`$WORKTREE_PATH`) and dev server port (`$PORT`)

Use `AskUserQuestion` to ask: "Does this plan look good? Reply 'yes' to proceed, or provide feedback to adjust."

If the user provides feedback, incorporate it and re-run from Step 1 (or just adjust the plan if changes are minor).

**Do not proceed to implementation without user approval.**

---

## Step 4: Implement

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 4; d.features[i].status = 'implementing'; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Launch an Agent (subagent_type: "general-purpose") to build the feature.

Prompt the agent with:
> You are a senior full-stack engineer implementing a feature. Follow this approved plan exactly:
>
> [INSERT APPROVED PLAN]
>
> **Working directory:** `$WORKTREE_PATH` — all file operations must happen in this directory.
> **Tech stack:** $STACK_SUMMARY
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
> 2. **Affected URLs** — all routes that were added or changed (full paths like http://localhost:$PORT/path)
> 3. **Decisions made** — anything you chose during implementation that wasn't specified in the plan (and why)
> 4. **Verification results** — confirm build passed, URLs load, and acceptance criteria met

Save the list of changed files and affected URLs.

---

## Step 5: Screenshots

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 5; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

**If `$HAS_SCREENSHOTS` is false:** Skip this step entirely and go to Step 6. Note that visual review (Step 6b) will be limited without screenshots.

**If `$HAS_SCREENSHOTS` is true:**

First, confirm the dev server is running in the worktree. If not, start it:
```bash
cd $WORKTREE_PATH && PORT=$PORT $DEV_CMD
```

Wait for it to be ready, then run the screenshot script to capture visual output of affected URLs. Choose the right auth mode based on which user role the feature targets:

- **Admin pages** (default): `--user admin@<domain>` (look at the screenshot script for default user)
- **Member pages**: `--user <a-member-email>` (pick a non-admin user from the DB)
- **Public pages**: `--no-auth`

```bash
npx tsx scripts/screenshot.ts --output-dir $SCREENSHOT_DIR [--user EMAIL | --no-auth] [AFFECTED_URLS using localhost:$PORT]
```

If the screenshot script fails, diagnose the error and fix before proceeding.

Read the screenshot images from `$SCREENSHOT_DIR` so they're available for the visual review.

---

## Step 6: Parallel Reviews

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 6; d.features[i].status = 'reviewing'; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Launch review agents **simultaneously** (all in a single message). Launch all 5 if screenshots are available; skip 6b (Visual Review) if no screenshots.

Each agent's full persona and review criteria are defined in its agent file under `$HOME/.claude/agents/`. Read the agent file and use it as the base prompt, then append the context-specific details listed below.

### 6a. UI Review Agent

Use the agent defined in `$HOME/.claude/agents/ui-reviewer.md`.

Append to the prompt:
- Set `$UI_PRINCIPLES` = `$HOME/.claude/ui-design-principles.md`
- Changed files to review (in `$WORKTREE_PATH`): [LIST OF CHANGED FILES WITH FULL WORKTREE PATHS]

### 6b. Visual Review Agent (skip if no screenshots)

Use the agent defined in `$HOME/.claude/agents/visual-reviewer.md`.

Append to the prompt:
- Set `$UI_PRINCIPLES` = `$HOME/.claude/ui-design-principles.md`
- Screenshot images from `$SCREENSHOT_DIR`: [LIST OF SCREENSHOT FILE PATHS]

### 6c. Simplicity Review Agent

Use the agent defined in `$HOME/.claude/agents/simplicity-reviewer.md`.

Append to the prompt:
- Set `$HICKEY_PRINCIPLES` = `$HOME/.claude/hickey-principles.md`
- Changed files to review (in `$WORKTREE_PATH`): [LIST OF CHANGED FILES WITH FULL WORKTREE PATHS]

### 6d. Security Review Agent

Use the agent defined in `$HOME/.claude/agents/security-reviewer.md`.

Append to the prompt:
- Changed files to review (in `$WORKTREE_PATH`): [LIST OF CHANGED FILES WITH FULL WORKTREE PATHS]
- Also tell the agent to read any existing auth utilities, middleware, or session helpers

### 6e. Functional QA Agent

Use the agent defined in `$HOME/.claude/agents/qa-tester.md`.

Append to the prompt:
- Dev server URL: `http://localhost:$PORT`
- Affected URLs: [LIST OF AFFECTED URLS]
- Acceptance criteria: [INSERT ACCEPTANCE CRITERIA FROM PLAN]

---

## Step 7: Revise

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 7; d.features[i].status = 'implementing'; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Consolidate feedback from all review agents. Launch an Agent to address the feedback.

Prompt the agent with:
> You are revising a feature implementation based on review feedback.
>
> **Working directory:** `$WORKTREE_PATH` — all file operations must happen in this directory.
>
> Here is the consolidated feedback:
>
> **UI Review:**
> [INSERT UI REVIEW FEEDBACK]
>
> **Visual Review:**
> [INSERT VISUAL REVIEW FEEDBACK, or "Skipped — no screenshots available"]
>
> **Simplicity Review:**
> [INSERT SIMPLICITY REVIEW FEEDBACK]
>
> **Security Review:**
> [INSERT SECURITY REVIEW FEEDBACK]
>
> **Functional QA:**
> [INSERT QA FEEDBACK]
>
> Rules:
> - **MUST FIX**: Address all of these. No exceptions.
> - **SHOULD FIX**: Address these by default. Only skip if you have a strong reason (document why).
> - **CONSIDER**: Document your decision but these are optional.
>
> Make the changes, then:
> 1. Run `cd $WORKTREE_PATH && $BUILD_CMD` and confirm zero errors. If there are build errors, fix them.
> 2. Output a summary of what you changed and any CONSIDER items you chose not to address (with reasoning).
> 3. List any files where you changed CSS, JS, or view-related code.

After the revision agent returns, if `$HAS_SCREENSHOTS` is true and any view-related code was changed, re-run the screenshot script on affected URLs and read the new screenshots to verify visual fixes landed correctly.

---

## Step 8: Manual Review

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 8; d.features[i].status = 'manual-review'; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Ensure the dev server is running in the worktree. If not, start it:
```bash
cd $WORKTREE_PATH && PORT=$PORT $DEV_CMD &
```

Wait for it to be ready (check that `http://localhost:$PORT` responds):
```bash
for i in $(seq 1 30); do curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT && break || sleep 1; done
```

Present the feature to the user for manual review using `AskUserQuestion`:

> **Ready for manual review!**
>
> The dev server is running at **http://localhost:$PORT**
>
> **Affected URLs:**
> [LIST EACH AFFECTED URL AS A FULL CLICKABLE LINK, e.g.:]
> - http://localhost:$PORT/path/one
> - http://localhost:$PORT/path/two
>
> **What was built:** [ONE-LINE SUMMARY FROM PLAN]
>
> **Automated review summary:**
> - MUST FIX: [count] found, all resolved
> - SHOULD FIX: [count] found, [count] resolved
> - CONSIDER: [count] suggestions ([count] addressed, [count] skipped)
>
> Please open the links above and review the feature. Reply:
> - **"approved"** — proceed to commit & push
> - **Any other feedback** — I'll make changes and present again for review

If the user provides feedback (anything other than "approved"):
1. Launch an Agent to address the feedback, working in `$WORKTREE_PATH`
2. After changes are made, run `$BUILD_CMD` to verify the build still passes
3. Loop back to the top of this step (re-present the URLs and ask again)

Maximum 3 feedback rounds. After 3 rounds, ask the user if they'd like to proceed to commit as-is or continue iterating.

If the user replies "approved", proceed to Step 9.

---

## Step 9: Commit & Push

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 9; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

Commit all changes from the worktree and push the feature branch:

1. Stage all new and modified files relevant to the feature (use `git add` with specific file paths — do NOT use `git add -A` or `git add .`).
2. Create a commit with a clear message using conventional commit format:

```bash
cd $WORKTREE_PATH
git add <files...>
git commit -m "$(cat <<'EOF'
feat: <short description>

<Brief summary of what was built and why.>
EOF
)"
git push -u origin feature/$FEATURE_NAME
```

---

## Step 10: Deploy

**Update tracking:**
```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) { d.features[i].step = 10; d.features[i].updatedAt = new Date().toISOString(); fs.writeFileSync(p, JSON.stringify(d, null, 2)); }
"
```

The project uses a push-to-deploy model: merging to `$DEFAULT_BRANCH` triggers CI/CD which deploys to production.

Present the user with options using `AskUserQuestion`:

> Feature branch `feature/$FEATURE_NAME` has been pushed.
>
> **Deploy model:** $DEPLOY_MODEL
>
> What would you like to do?
> 1. **Create a PR** — I'll create a pull request from `feature/$FEATURE_NAME` to `$DEFAULT_BRANCH` for review
> 2. **Merge directly** — I'll merge the branch to `$DEFAULT_BRANCH` and push (triggers deploy)
> 3. **Skip deploy** — Leave the branch as-is for now
>
> Reply with 1, 2, or 3.

**If the user chooses 1 (Create PR):**
```bash
cd $WORKTREE_PATH
gh pr create --title "feat: <short description>" --body "$(cat <<'PREOF'
## Summary
<1-3 bullet points from the plan>

## Review findings addressed
- <count> MUST FIX items resolved
- <count> SHOULD FIX items resolved
- <count> CONSIDER items (addressed/skipped with reasoning)

## Test plan
- [ ] <acceptance criteria items>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
PREOF
)"
```

Report the PR URL to the user. Set `$DEPLOY_CHOICE` to `pr`.

**If the user chooses 2 (Merge directly):**
```bash
cd $WORKTREE_PATH
git checkout $DEFAULT_BRANCH
git merge feature/$FEATURE_NAME --no-ff -m "Merge feature/$FEATURE_NAME"
git push origin $DEFAULT_BRANCH
```

Report that deploy has been triggered via CI/CD. Set `$DEPLOY_CHOICE` to `merged`.

**If the user chooses 3 (Skip):**
Note the branch name and move on. Set `$DEPLOY_CHOICE` to `skipped`.

---

## Step 11: Report

**Update tracking based on whether the branch is merged:**

- If `$DEPLOY_CHOICE` is `merged`: status = `complete` (branch is in default branch)
- If `$DEPLOY_CHOICE` is `pr`: status = `pr-open` (waiting for PR merge)
- If `$DEPLOY_CHOICE` is `skipped`: status = `pushed` (branch pushed but not merged)

```bash
node -e "
const fs = require('fs');
const p = '.claude/features.json';
const d = JSON.parse(fs.readFileSync(p,'utf8'));
const i = d.features.findIndex(x => x.name === '$FEATURE_NAME');
if (i !== -1) {
  d.features[i].step = 11;
  d.features[i].status = '$STATUS';
  d.features[i].updatedAt = new Date().toISOString();
  fs.writeFileSync(p, JSON.stringify(d, null, 2));
}
"
```

Where `$STATUS` is set from `$DEPLOY_CHOICE` as described above.

If the feature was directly merged (`complete` status), also clean up the worktree:
```bash
if [ -d "$WORKTREE_PATH" ]; then
  git worktree remove "$WORKTREE_PATH" --force
fi
```

Summarize to the user:

1. **What was built** — brief description
2. **Branch** — the feature branch name
3. **Files changed** — final list
4. **Review findings** — how many MUST/SHOULD/CONSIDER items were found and addressed
5. **Remaining CONSIDER items** — any unaddressed suggestions with reasoning
6. **Deploy status** — PR created / merged & deploying / skipped
7. **Next steps** — based on deploy choice:
    - If **merged**: "Feature is complete and deployed. Worktree cleaned up."
    - If **PR created**: "Feature is waiting for PR merge. Run `/feature complete $FEATURE_NAME` after the PR is merged to finalize."
    - If **skipped**: "Branch `feature/$FEATURE_NAME` is pushed but not merged. Run `/feature complete $FEATURE_NAME` after merging to finalize."

Done!
