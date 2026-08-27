# The fleet control plane

Turns a single WezTerm window into a **fleet dashboard**: every tab is one agent
session, colored by how long it has been waiting on you, labeled by what
distinguishes it, and reachable through a fuzzy picker and a content search.

Part of [claude-config](README.md) — see the README for install and for the rest
of the repo. Linux + systemd + WezTerm.

## The idea: two layers

The visible tab bar is only a front-end. It renders a small, terminal-agnostic
**data layer** that any tool could consume.

```
Claude/Codex hooks ─► ~/.claude/state/<session_id>.json   {status, since, agents, wezterm_pane, cwd}
        │                          ▲
        │                 bin/claude-snapshot  (60s systemd timer + unhooked-vendor fallback)
        │                   • snapshot live sessions for crash recovery
        │                   • REAP state files whose session is dead
        ▼                          │
  bin/claude-sessions --json ──────┤        bin/claude-search <text>
   (urgency-sorted registry)       │         (grep live transcripts)
        │                          │                 │
        └───────────────► wezterm/wezterm.lua ◄──────┘
                          colors · labels · pickers
```

**Layer 1 — the data layer (works in any terminal).**

- **`hooks/session-state.sh`** is wired into Claude Code through `settings.json`
  and Codex through `codex/hooks.json`. On each lifecycle event it writes
  `~/.claude/state/<session_id>.json`
  with the session's `status` (`working` / `waiting` / `delegating`), `since`
  (when that status last *changed* — it only moves on a real transition, so
  "waiting 14m" stays truthful), `agents`, `wezterm_pane`, and `cwd`. It also
  Claude also gets a terminal title with a trailing `●` when the session is
  waiting on you, or `◐` when it is `delegating`; Codex hook output stays silent
  because WezTerm consumes the shared state file directly.

  `delegating` means the turn ended but background subagents are still running —
  the task notification will wake the session, so it is *not* waiting on you: no
  ding, no `●`, Attend skips it. The `agents` counter drives it (PreToolUse of
  the Task/Agent tool increments for Claude, while Codex's native
  `SubagentStart` increments directly; `SubagentStop` decrements both). Because a killed
  subagent never fires `SubagentStop`, the counter can only stick *high*, which
  would silence a tab forever — so every renderer (tab bar, statusline, picker)
  degrades a `delegating` whose `updated` has been silent >30m back to
  `waiting`: the worst drift is a late ping, never a missed one. Known gaps that
  still read as `waiting`: background Bash tasks and Workflow runs, which have
  no completion event the hook can see.
- **`bin/claude-sessions --json`** joins those state files into an urgency-sorted
  registry — the list the picker shows.
- **`bin/claude-search <text>`** greps the transcripts of *live* sessions (ripgrep
  if present, else grep) and returns the matches ranked by hit count. It is the
  live-scoped sibling of the `/find-session` skill.
- **`bin/claude-snapshot`** runs every 60 s from a systemd-user timer. It records
  the live session set for crash recovery *and* reaps dead `state/` files —
  a file whose session is no longer live is garbage that would otherwise skew the
  tab colors and collide with reused WezTerm pane ids. (Reaping is safe: a live
  session regenerates its file on its next hook, and it never mass-deletes on an
  empty liveness read.) For vendors without lifecycle hooks it retains the old
  transcript-silence status estimate; a record tagged `status_source: "hook"`
  is authoritative and is never overwritten by that estimate.

**Layer 2 — the WezTerm front-end (`wezterm/wezterm.lua`).**
It reads the data layer every ~second and turns it into the tab bar described below.

## What the tab bar does

**Wait-color escalation.** A waiting tab warms from calm teal → green → yellow →
orange → deep red as it waits. Details that make it readable:

- **Continuous truecolor gradient**, interpolated across seven stops — not three
  hard steps.
- **Logarithmic age scale** so minutes, hours, and days each get their own band;
  a few days-old sessions can't flatten everything recent into one color.
- **Ease-in slow start** (`WAIT_GAMMA`) — a fresh wait stays calm for a while
  before it climbs.
- **Dynamic, relative ceiling** = `max(2h, the longest currently-open wait)`. The
  reddest tab is always the most-neglected relative to the rest, and nothing maxes
  out before two hours when every wait is recent. Only *open* tabs count toward the
  ceiling, so lingering orphans don't blow out the scale.
- **Downtime-frozen ages.** Wait age is measured on an "awake clock" that stops
  during suspend and does not count powered-off time, and it is persisted to
  `~/.cache/wezterm-fleet-wait.json`. So resuming the machine — or rebooting —
  preserves each tab's color instead of inflating every wait by the time you were
  away.
- **Live-title gated.** The color only applies when the pane's live title still
  shows the `●` marker, so a tab that has resumed working never keeps a stale
  color from a dead session that once held its (reused) pane id.

**Adaptive labels.** Each tab spends its columns on what *distinguishes* it:
working tabs show their task text; otherwise the label drops default branch names
(`main`/`master`), de-duplicates a project name that repeats the label, and — when
the tab is tight — leads with the distinguishing bit (feature branch, else the
session tag) so right-edge truncation can't eat it.

**Stable, filled layout.** Tabs are sized by proportional shares (the active tab
gets more) so the row fills the bar (WezTerm's retro tab bar won't stretch on its
own). The measured bar width is stabilized with hysteresis (a one-column wobble —
the scrollbar column blinking in and out as scrollback grows — is ignored) and a
focused-window guard, so nothing jumps while you type. The active tab is
highlighted so the focused session is unmistakable.

## Keybindings

| Keys | Action |
|------|--------|
| `Ctrl+Shift+Space` | **Session picker** — fuzzy list of live (and snoozed) sessions, sorted by urgency; Enter jumps to that tab (or resumes it if closed/snoozed) |
| `Ctrl+Shift+F` | **Content search** — type text, grep live transcripts, jump to the matching session |
| `Ctrl+Shift+S` | **Snooze** — pick a time, close the tab now, and auto-reopen it then (resume early from the picker) |
| `Ctrl+Shift+G` | **Launch family** — spawn a saved cluster of sessions (`~/.claude/fleet/families.json`) into its own workspace |
| `Ctrl+Shift+←` / `→` | Move the active tab one slot left / right |
| `Ctrl+Shift+Home` / `End` | Send the active tab to the first / last position |

## Snooze: close now, reopen on schedule

For long-running tasks that are blocked for days, `Ctrl+Shift+S` **closes the tab
now and schedules it to reopen at a chosen time** (`1h`, `tomorrow 9am`, `3 days`,
or a precise datetime). This is just a *scheduled resurrection*: closing ends the
process but the transcript survives, and `claude --resume` brings the conversation
back exactly.

- **`bin/claude-schedule`** owns `~/.claude/scheduled.json` — `add` / `list` /
  `cancel` / `reopen-due`. It parses the time, and `reopen-due` spawns any overdue
  session back as a tab (the same `wezterm cli spawn … claude --resume` that
  `claude-resume` uses), dropping entries that are already live and keeping ones
  whose spawn fails so they retry.
- **`bin/claude-snapshot`** calls `reopen-due` on its existing 60s tick, so a
  reopen scheduled while the machine is asleep simply fires when it is next on —
  no new timer, robust to suspend/reboot.
- Snoozed sessions still appear in the **`Ctrl+Shift+Space` picker** marked
  `⏰ reopens in Xh`; selecting one reopens it immediately and cancels the schedule.

## Renderer note

The config runs Claude Code on the **classic renderer** (`"tui": "default"`, and
`CLAUDE_CODE_NO_FLICKER` is *not* set). That keeps output in the terminal's primary
scrollback, so WezTerm's scrollbar, mouse wheel, and native text selection all
work. The tradeoff is that resizing the window re-wraps already-printed lines
(inherent to primary-scrollback terminals); `Ctrl+L` forces a clean redraw. The
fullscreen/alt-screen renderer resizes cleanly but has no scrollback — this config
chooses the scrollbar.

## Install

See the [README](README.md#install). The pieces this subsystem needs are the
`~/.claude`, `~/.codex/hooks.json`, and `wezterm.lua` symlinks, a
`~/.claude/state` directory, the hook configs already present in `settings.json`
and `codex/hooks.json`, and the
`claude-snapshot.timer` systemd-user unit (60 s tick — it drives both crash
recovery and scheduled reopens).

## Related

`bin/README.md` documents the **durability & resurrection** layer — fsync'ing
transcripts against unclean shutdowns (`claude-transcript-sync`,
`claude-restore-transcripts`) and reopening the sessions that were live before a
reboot (`claude-snapshot`, `claude-resume`).
