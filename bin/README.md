# Claude session resurrection & transcript durability

Two jobs:
1. **Durability** — make sure a hard reboot/freeze can't destroy session
   transcripts (it could, and did, on 2026-06-24).
2. **Resurrection** — bring back the sessions that were open when the machine
   shut down, as native **tilix** tabs, each resumed to its exact conversation.

## Durability (the data-loss guard)

Claude appends each turn to `~/.claude/projects/<encoded-cwd>/<id>.jsonl`, but the
kernel may hold those writes in the page cache for up to `vm.dirty_expire_centisecs`
before flushing. On an unclean shutdown, ext4's default `data=ordered` journals
metadata but not data — so unflushed writes, and even freshly-created transcript
files, roll back. Whole sessions can vanish. (This machine also sets
`vm.dirty_expire_centisecs=500` via `/etc/sysctl.d/99-faster-writeback.conf` to
shrink the window system-wide.)

- **`claude-transcript-sync`** — run every 20s by a systemd-user timer. `fsync()`s
  every transcript modified in the last 5 min so the *live* file is durable, and
  mirrors it to `~/.claude/projects-backup/` (also fsync'd). Caps freeze loss at
  ~20s per conversation. Older transcripts are already disk-durable, so they're skipped.
- **`claude-restore-transcripts`** — restores any transcript that went missing or
  got truncated from the live tree, out of the backup. Run this first after a freeze,
  then `claude-resume-all`. `--dry-run` to preview.

## Resurrection (which sessions were open)

- **`claude-snapshot`** — run every 60s by a systemd-user timer. Records the set of
  live Claude sessions to `~/.claude/sessions-snapshot.json`. Sources, by confidence:
  - `registry` — Claude's own `~/.claude/sessions/<pid>.json` (exact pid + id)
  - `proc` — session id read from a process's argv (`--resume` / `--session-id`)
  - `heuristic` — fresh `claude` launches matched to their transcript by start time
  - **Boot-grace guard:** for the first 15 min after boot the snapshot may not
    *shrink* — so a post-boot run can't overwrite the pre-crash set before you
    restore it. (This was the second half of the 2026-06-24 failure.)
- **`claude-resume-all`** — the on-demand restore command. Reads the latest snapshot,
  skips sessions already running, groups the rest by project, and opens one tilix
  window per project with each session a tab running `claude --resume <id>`. Launches
  sequentially and **verifies each session actually comes up** (retrying once and
  reporting failures) — an earlier version fired tabs at the tilix server faster than
  it could accept them and silently dropped some.

## Usage

```sh
# after a freeze:
claude-restore-transcripts        # put back any transcript that got rolled back
claude-resume-all                 # reopen everything that isn't already running

claude-resume-all --dry-run                 # show what it would open
claude-resume-all --single-window           # all tabs in one window
claude-resume-all --snapshot ~/.claude/sessions-snapshot.prev.json   # use the backup snapshot
claude-resume-all --from-transcripts [N]    # ignore the snapshot; reconstruct the N
                                            # most-recently-active sessions from transcripts
```

Large sessions show Claude's own "resume from summary vs full?" prompt per tab.

## Install (already done on this machine)

```sh
# ~/.claude is a symlink to this repo; bin/ and systemd/user/ live here.
ln -sf "$HOME/.claude/bin/claude-resume-all" "$HOME/.local/bin/claude-resume-all"
for u in claude-snapshot claude-transcript-sync; do
  ln -sf "$HOME/.claude/systemd/user/$u.service" ~/.config/systemd/user/
  ln -sf "$HOME/.claude/systemd/user/$u.timer"   ~/.config/systemd/user/
  systemctl --user enable --now "$u.timer"
done
```

Timers run within the user's graphical session (no `loginctl enable-linger` needed —
recovery happens after you log back in anyway).
