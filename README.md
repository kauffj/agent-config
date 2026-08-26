# claude-config

A complete, opinionated Claude Code configuration: the whole `~/.claude`
directory as a git repo. It is installed by symlinking the repo *onto* the
config directory, so the thing you edit and the thing Claude Code reads are the
same files.

Two ideas run through all of it:

- **Sessions are a fleet, not a window.** A dozen concurrent Claude Code
  sessions is normal. They need a registry, an urgency order, a way to search
  across them, and a way to bring back the ones that died.
- **A transcript is the only artifact that can't be regenerated.** Code can be
  rewritten; a conversation can't. So durability gets real engineering, not
  hope.

It is **Linux + systemd + WezTerm**. Nothing here is portable to macOS or
Windows without work, and the interesting parts assume all three.

## What's in here

| Path | What it is |
|---|---|
| **[`FLEET.md`](FLEET.md)** | The fleet control plane — a terminal-agnostic data layer, and a WezTerm tab bar that renders it. Wait-color escalation, fuzzy session picker, transcript search, snooze-and-reopen. |
| **[`bin/README.md`](bin/README.md)** | Durability and resurrection — fsync'ing transcripts against unclean shutdowns, restoring ones that vanished, and reopening the sessions that were live before a reboot. |
| `skills/` | 14 skills — `workspace`, `feature`, `propose`, `review-pr`, `explore`, `fix-ci`, `humanizer`, and others. |
| `agents/` | 5 review subagents — security, simplicity, UI, visual, QA. |
| `hooks/` | 8 hooks — session state, a destructive-command guard, auto-format, lessons injection, transcript fsync, and a pre-commit leak guard. |
| `bin/` | 18 command-line tools behind the above. |
| `lib/` | Shared libraries behind the skills — among them `doctor.mjs` (config integrity), `workspace.mjs` (worktree state, port allocation) and `project.mjs` (project profile). |
| `systemd/user/` | 9 units — the timers that make durability and resurrection actually run. |
| `CLAUDE.md` | The standing instructions. Workflow orchestration, core principles, and a section on the economics of an agent's time vs. yours. |
| `hickey-principles.md`, `ui-design-principles.md` | Reference docs the instructions point at instead of restating. |

## The parts most worth stealing

**The fleet tab bar** ([`FLEET.md`](FLEET.md)). Every tab is one session, colored
by how long it has been waiting on you — a continuous truecolor gradient on a
logarithmic age scale, with a dynamic ceiling so the reddest tab is always the
most-neglected one *relative to the rest*. Wait age runs on an "awake clock"
that stops during suspend, so resuming your machine doesn't turn the whole bar
red. `Ctrl+Shift+Space` opens a fuzzy picker sorted by urgency;
`Ctrl+Shift+F` greps live transcripts and jumps to the match.

The visible bar is only a front-end. Underneath, `hooks/session-state.sh` writes
`state/<session_id>.json` on every session event and `bin/claude-sessions --json`
joins those into an urgency-sorted registry. Any terminal could render it.

The registry is not Claude-only. Codex and Grok sessions run in the same tabs and
compete for the same attention, so each vendor gets a small adapter returning the
same record shape plus a `vendor` tag — and the picker, the tab bar, the attend
key, cross-session search, the transcript reader and the post-reboot restore all
work on them without knowing which vendor they are. Vendors that publish no
session registry are found by process and matched to their own transcript store;
they get a status from how long that transcript has been silent, since only
Claude has hooks to report one.

**The durability layer** ([`bin/README.md`](bin/README.md)). Claude appends each
turn to a transcript, but the kernel can hold those writes in the page cache for
seconds, and ext4's default `data=ordered` journals metadata but not data — so
an unclean shutdown can roll back whole sessions. This happened, twice, and the
write-up is the most directly useful thing in the repo. A 20-second timer
fsyncs and mirrors recent transcripts; a 60-second timer records which sessions
are live, with a boot-grace guard and a "cliff guard" for suspend-freezes, so a
post-crash tick can't overwrite the pre-crash set before you restore from it.

**The account router** (`bin/claude-acct`). Two subscriptions, one `claude`
command: every launch probes both accounts and starts the session on whichever
has more headroom — scoring usage against each window's *next reset* rather than
its raw percentage, because 60% spent with twenty minutes to refill is cheaper
than 40% that has to last four hours. It enforces limits rather than working
around them: when every account is capped it refuses and names what else is
installed instead, and a session that hits its cap mid-turn hands the
conversation to the account with room rather than ending (both accounts share
one transcript store, so the conversation itself can move). One person, two paid
subscriptions, no shared credentials. It reads an undocumented
`api/oauth/usage` endpoint — the same one `/usage` renders — so treat that part
as liable to change without notice.

**`/workspace`** (`skills/workspace/SKILL.md`). One primitive for isolated
parallel work: a git worktree, a dev-server port, a copied env file, an isolated
database, and a state record — created, resumed, and torn down as a unit. The
other skills (`/feature`, `/propose`, `/review-pr`) call it instead of each
reimplementing worktree setup.

**The review agents** (`agents/`, `skills/review-*`). Five specialist reviewers —
security, simplicity, UI, visual, QA — that share one contract rather than one
prompt. Each declares its **authority** (what it may and may not do: the QA
tester never reads source, the simplicity reviewer never questions requirements
that were already agreed), carries a single **core question** it asks of every
file, grades findings **MUST FIX / SHOULD FIX / CONSIDER**, and ends with **"what
I couldn't evaluate"** — the blind-spot disclosure that makes a clean review
trustworthy instead of merely quiet. `/review-pr` runs them over a PR, a
worktree, or an explicit file list; each also runs standalone.

The simplicity and UI reviewers read their criteria from files in this repo
(`hickey-principles.md`, `ui-design-principles.md`, pointed at by `$HICKEY_PRINCIPLES`
and `$UI_PRINCIPLES` in `settings.json`), so the standard being applied is
editable text rather than something buried in a prompt.

**`lib/doctor.mjs`**. Resolves every cross-reference in the config — file paths
embedded in skills, agents, and hooks, `settings.json` env values, the `~/.claude`
symlink — against what is actually on disk. It runs at session start and on
pre-commit, turning silent breakage (a renamed agent file, a moved doc) into a
loud, early failure.

## Install

```bash
git clone <this repo> ~/projects/claude-config
ln -s ~/projects/claude-config ~/.claude
mkdir -p ~/.claude/state ~/.config/wezterm
ln -s ~/.claude/wezterm/wezterm.lua ~/.config/wezterm/wezterm.lua

# timers for durability + resurrection
mkdir -p ~/.config/systemd/user
ln -s ~/.claude/systemd/user/*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-snapshot.timer claude-transcript-sync.timer

# integrity check + the pre-commit leak guard
node ~/.claude/lib/doctor.mjs
ln -sf ../../hooks/pre-commit ~/.claude/.git/hooks/pre-commit

# keep auto mode's generated infra dossier out of git (see below)
git -C ~/.claude config filter.strip-automode.clean "node lib/strip-automode.mjs"
```

**That last line matters.** Auto mode writes a summary of this machine's
infrastructure — production hostnames, where secrets live, how deploys work —
into `settings.json` under `autoMode.environment`. It belongs in the working
file and never in a public repo. The clean filter (`lib/strip-automode.mjs`,
wired up by `.gitattributes`) strips it on the way into git, so staging
`settings.json` cannot carry it even if you forget; `hooks/pre-commit` refuses
the commit as a backstop when the filter isn't configured. Both are needed: the
filter is per-clone git config, which a fresh clone does not inherit.

`settings.json` already wires the hooks and the status line; it is read from
`~/.claude/settings.json`, which the symlink provides.

**Requirements:** WezTerm, `python3`, `jq`, `node`, systemd-user, and ideally
`ripgrep` (plain `grep` is the fallback).

**Optional, not included:** `claude-grep` (from a separate personal repo at
`~/projects/claude-beep`) marks a found session's terminal tab. `/find-session`
searches with the bundled `bin/claude-search` and only reaches for `claude-grep`
when it is present, so nothing here breaks without it.

**Caveats.** This is one person's live config, published because parts of it are
generally useful — not a framework. It assumes a `~/projects/<name>` layout, and
`settings.json` runs with a broad permission allowlist and `defaultMode: auto`,
which you should read before adopting.

**On permissions.** The allowlist grants `Bash(*)` — every shell command,
unprompted. That is a deliberate trade for an autonomous single-operator machine,
not an oversight, and it is the setting to change first if you adopt this. It
used to sit above forty specific `Bash(...)` entries that it already subsumed;
those are gone, because a list that grants nothing reads like the real boundary
and isn't. The actual boundary is `hooks/on-pre-tool.sh` (target-aware refusals,
tested by `hooks/test-on-pre-tool.sh`) plus `autoMode.soft_deny`. Defense in
depth, not the primary control — if you want a real one, narrow `Bash(*)`. `wezterm/families.example.json` is an
example; copy it to `~/.claude/fleet/families.json` and use absolute paths (the
`cwd` is handed to WezTerm verbatim — no `~` or `$HOME` expansion).

## Not in this repo

Some things are deliberately kept out of git and stay on the machine: Claude
Code's own runtime state (`.claude.json`, transcripts, caches), anything
credential-shaped, and a couple of personal assets. `.gitignore` covers what
exists today; `hooks/pre-commit` is the backstop for what shows up tomorrow — it
refuses to stage transcripts, credential-shaped paths, runtime-state
directories, and content matching known key formats.

If you clone this, note that `skills/kauffj-voice` and `skills/writing` encode
one person's voice and writing process. They are here as worked examples of
encoding a preference into a skill, not as something to use as-is.

## License

MIT — see [LICENSE](LICENSE). Take what is useful; attribution appreciated but
the license is the agreement.
