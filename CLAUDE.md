## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don’t keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: “Would a staff engineer approve this?”
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask “is there a more elegant way?”
- If a fix feels hacky: “Knowing everything I know now, implement the elegant solution”
- Skip this for simple, obvious fixes — don’t over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don’t ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

### 7. Papercuts
- A papercut is anything annoying you notice that you aren’t fixing right now — because it’s out of scope, not worth stopping for, or environment/tooling friction (a dead-end tool call, a broken link, a missing helper script, misleading docs, a flaky command)
- Append one line to `tasks/papercuts.md` at the repo root: `- YYYY-MM-DD — what you noticed, and what would fix or have avoided it`
- Log it the moment you notice it, then keep working. Don’t ask permission and don’t derail the task
- Papercuts are problems with the code or environment, not your own mistakes — those go in `tasks/lessons.md`


## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections


## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what’s necessary. Avoid introducing bugs.
- **Simple over Easy**: Prefer solutions that are objectively simple (not complected) over ones that are merely familiar or convenient. Ask "am I braiding things together that should be separate?"
- **Data over Types**: Prefer plain data (maps, arrays) over custom classes when the structure is simple. Don’t create a wrapper when the underlying value carries the meaning.
- **UI for Humans**: Show system status, speak the user’s language, give control and clear exits, be consistent, prevent errors before they happen. Every element must earn its place on screen. Full reference (Nielsen + Atomic Design): `~/.claude/ui-design-principles.md` (aka `ui-principles.md`, and `$UI_PRINCIPLES`) — treat any "per ui-principles.md" as pointing here.


## Deploys & Servers

Every production site runs on one shared droplet. **`~/projects/server-config/SERVER.md`
is the single reference** — read it before touching deploys, nginx, systemd, cron, DNS,
or backups for any project. Point at it from a project CLAUDE.md; never restate it, or
the two copies drift.

- **Deploying means pushing to GitHub `main`.** The standard is self-pull: the box
  checks `main` every minute and deploys it, so a commit ships regardless of origin —
  laptop, a PR merged in the web UI, another machine, an agent. Never build a deploy
  path that makes one developer's push the only trigger. *Check the SERVER.md inventory
  row before assuming a given site is on it yet* — sites still on a dual-pushurl `origin`
  only deploy from that one machine.
- **Never edit `server-config`'s `etc/ usr/ var/ home/ opt/ secrets/`** — one-way
  live→repo mirror; your edit changes nothing and is reverted. Repo-root `*.md` and
  `snapshot.sh` *are* source of truth. Pull before editing.
- **Migrations run after the build, never before.** A commit that fails to build must
  not have already changed the production schema.
- **One copy of any deploy mechanism.** If adding a site means copying a script that
  exists for another site, parameterize it instead — a per-site copy is how three
  divergent deploy hooks happened here. Mechanism lives in
  `server-config/templates/dynamic-site/`.
- **Adding or removing a site → update the SERVER.md inventory table in the same change.**
- **Never commit a Claude Code transcript.** `/export` writes into the cwd; they contain
  connection strings and keys from the session.
