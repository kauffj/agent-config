## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for any non-trivial task — architectural decisions, multi-file features, anything hard to reverse. Bug fixes are exempt (see 6)
- Write detailed specs upfront to reduce ambiguity. Plan approval is the user’s checkpoint: after it, run autonomously to a finished result (see 8)
- Track the plan in `tasks/todo.md` as checkable items; mark them off as you go and end with a short review section of what actually changed
- If something goes sideways, STOP and re-plan immediately — don’t keep pushing
- Use plan mode for verification steps, not just building

### 2. Subagent Strategy
- Use subagents for work that is **read-heavy or parallel**: research, exploration, sweeping many files, independent analyses that can run at once. The win is keeping their output out of the main context, not the delegation itself
- Do the work directly when you already know which file to open, when the task is a single edit, or when you'd spend more context briefing the agent than doing it. Reaching for one by default is its own kind of ceremony
- One task per subagent for focused execution; for genuinely hard problems, throw more compute at it
- Concurrent agents share the filesystem. Namespace temporary/helper files by task, or give each task its own scratch directory, so one agent cannot silently overwrite another's executable input

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with a rule that would have prevented the mistake
- Lessons are injected automatically by the current harness's SessionStart hook; if they didn’t arrive, read `tasks/lessons.md` yourself
- When a lesson keeps proving itself, graduate it into the current project's `AGENTS.md`; update the global canonical instructions only when the lesson is genuinely global — lessons.md is the inbox, not the archive

### 4. Verification Before Done
- Never mark a task complete without proving it works: run tests, check logs, demonstrate correctness
- Diff behavior between main and your changes when relevant
- A verification stamp is a factual claim about your own conduct, and it carries the same weight as the result it certifies. Run the check, paste its output into the draft, write the sentence from what is on the page, then delete the paste — never compose the evidence and the conclusion in the same keystroke
- A wrong conclusion invites the next reader to test it; a fabricated verification tells them not to bother

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask “is there a more elegant way?”
- If a fix feels hacky: “Knowing everything I know now, implement the elegant solution”
- Skip this for simple, obvious fixes — don’t over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don’t ask for hand-holding
- Point at logs, errors, failing tests — then resolve them. Zero context switching required from the user
- Go fix failing CI tests without being told how

### 7. Papercuts
- A papercut is anything annoying you notice that you aren’t fixing right now — because it’s out of scope, not worth stopping for, or environment/tooling friction (a dead-end tool call, a broken link, a missing helper script, misleading docs, a flaky command)
- Append one line to `tasks/papercuts.md` at the repo root: `- YYYY-MM-DD — what you noticed, and what would fix or have avoided it`
- Log it the moment you notice it, then keep working. Don’t ask permission and don’t derail the task
- Papercuts are problems with the code or environment, not your own mistakes — those go in `tasks/lessons.md`

### 8. Your Time Is Cheap, the User's Is Not
- Assume your time is worth 0.1–0.2% of the user's. An hour of your effort to save the user a minute is a good trade — always take it.
- Collect user feedback end-to-end: one review pass on the finished thing, not check-ins between stages. The user is not for unit testing or intermediate evaluation unless you genuinely cannot proceed without them.
- Plan approval and important-decision checkpoints still happen — checkpoints are for decisions, not testing. But when options are cheap to build, build all of them and let the user pick between working versions instead of asking A-or-B.
- Automate every step the user would otherwise do by hand: write the script instead of listing manual steps, use browser tools to carry them to the exact field that needs their input, pre-fill everything you can.
- Exhaust automated testing and automated review before asking for human review.
- When you do ask for review, deliver the target ready to look at: open the app, launch the window, link the exact URL or document. Never "go to X and click Y."
- Once approved work passes its verification gates, deliver and push it directly to the repository's actual default branch, then monitor CI and deployment to completion. Do not offer a PR or leave finished work on a feature branch.
- With direct delivery there is no PR body to preserve the review. For a non-trivial commit, make the commit message the durable record: explain what was wrong, why this shape, meaningful rejected alternatives or tradeoffs when they exist, and anything expensive for the next reader to re-derive. Do not invent ceremony for a trivial commit.

### 9. Portable Project Instructions
- Author shared project guidance in the nearest `AGENTS.md`
- Keep `CLAUDE.md` as `@AGENTS.md` plus only genuinely Claude-specific behavior
- Do not create project instruction files when the project has no local guidance, and do not copy global policy into every repository


## Core Principles

- **Simplicity First**: Make every change as simple as possible — touch only what’s necessary, minimal code, minimal blast radius.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Simple over Easy**: Prefer solutions that are objectively simple (not complected) over ones that are merely familiar or convenient. Ask "am I braiding things together that should be separate?" Full reference (Hickey's philosophy of simplicity, also grounds Data over Types): `~/.config/agent-config/hickey-principles.md` (aka `$HICKEY_PRINCIPLES`) — treat any "per hickey principles", "per hickey", or other use of "hickey" as pointing here.
- **Data over Types**: Prefer plain data (maps, arrays) over custom classes when the structure is simple. Don’t create a wrapper when the underlying value carries the meaning.
- **UI for Humans**: Show system status, speak the user’s language, give control and clear exits, be consistent, prevent errors before they happen. Every element must earn its place on screen. Full reference (Nielsen + Atomic Design): `~/.config/agent-config/ui-design-principles.md` (aka `ui-principles.md`, and `$UI_PRINCIPLES`) — treat any "per ui principles" or "per design principles" as pointing here.


## Deploys & Servers

Every production site runs on one shared droplet. **`~/projects/server-config/SERVER.md` is the single reference** — read it before touching deploys, nginx, systemd, cron, DNS, or backups for any project. Point at it from a project's `AGENTS.md`; never restate it, or the two copies drift.

- **Pushing to GitHub `main` IS deploying.** The box self-pulls `main` every minute — a commit ships no matter where it came from. Know this before you push; check the SERVER.md inventory for sites not yet on self-pull.
- **Never edit `server-config`'s `etc/ usr/ var/ home/ opt/ secrets/`** — one-way live→repo mirror; your edit is reverted. Repo-root `*.md` and `snapshot.sh` *are* source of truth.
- **Never commit an agent transcript.** Claude Code's `/export` and equivalent harness exports can write into the cwd; transcripts contain connection strings and keys.

### Temporary server lifetime

- Launch every temporary development, preview, test, or static-file server through `~/.config/agent-config/bin/agent-session-server -- <command>`. Run the wrapper as a foreground long-running tool command; use the tool's background/session facility when work must continue. Do not use shell `&`, `nohup`, `disown`, or a daemon mode.
- The wrapper owns the server process group and stops it when the nearest Claude or Codex CLI process exits. Production services that intentionally outlive the agent session are outside this rule.
- Stop temporary servers explicitly when review ends, then verify that their listening port closed. Session-bound cleanup is the final safety net, not a substitute for ordinary cleanup.


## Writing

For any **longform I'll publish under my own name** — essays, articles, Substack posts, threads, arguments — invoke the **`writing`** skill first (process, preferences, and the Google Doc collaboration protocol live there; don't restate them). Core rule: **I write the words; you build the scaffold** — bullets and structure, never finished prose. `kauffj-voice` is for tweets and throwaway, not for ghostwriting signed work.
