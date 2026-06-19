# Claude session resurrection

Bring back the Claude Code sessions that were open when the machine shut down
(intentional, battery, or crash) as native **tilix** tabs, each resumed to its
exact conversation.

## How it works

- **`claude-snapshot`** — run every 60s by a systemd-user timer. Records the set of
  live Claude sessions to `~/.claude/sessions-snapshot.json`. Sources, by confidence:
  - `registry` — Claude's own `~/.claude/sessions/<pid>.json` (exact pid + id)
  - `proc` — session id read from a process's argv (`--resume` / `--session-id`)
  - `heuristic` — fresh `claude` launches (no id in argv, not yet registered) mapped
    to the most-recently-active transcript(s) in their cwd. Right count, best-effort
    identity; can mis-pick if a recently-closed transcript looks newer.
- **`claude-resume-all`** — the on-demand restore command. Reads the latest snapshot,
  skips sessions already running, groups the rest by project, and opens one tilix
  window per project with each session a tab running `claude --resume <id>`.

The conversation itself is never at risk — Claude always appends every turn to
`~/.claude/projects/<encoded-cwd>/<id>.jsonl`. This tooling only records *which*
sessions were open and reopens them.

## Usage

```sh
claude-resume-all                 # restore everything that isn't already running
claude-resume-all --dry-run       # show what it would open
claude-resume-all --single-window # all tabs in one window instead of per-project
claude-resume-all --snapshot ~/.claude/sessions-snapshot.prev.json   # use the backup
```

Large sessions show Claude's own "resume from summary vs full?" prompt per tab.

## Install (already done on this machine)

```sh
ln -sf "$HOME/.claude/bin/claude-resume-all" "$HOME/.local/bin/claude-resume-all"
ln -sf "$HOME/.claude/systemd/user/claude-snapshot.service" ~/.config/systemd/user/
ln -sf "$HOME/.claude/systemd/user/claude-snapshot.timer"   ~/.config/systemd/user/
systemctl --user enable --now claude-snapshot.timer
```
