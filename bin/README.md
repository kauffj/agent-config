# Claude session resurrection & transcript durability

Two jobs:
1. **Durability** — make sure a hard reboot/freeze can't destroy session
   transcripts (it could, and did, on 2026-06-24).
2. **Resurrection** — bring back the sessions that were open when the machine
   shut down, as WezTerm tabs (`claude-resume`), each resumed to its exact
   conversation.

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
  then `claude-resume`. `--dry-run` to preview.

## Resurrection (which sessions were open)

- **`claude-snapshot`** — run every 60s by a systemd-user timer. Records the set of
  live Claude sessions to `~/.claude/sessions-snapshot.json`. Sources, by confidence:
  - `registry` — Claude's own `~/.claude/sessions/<pid>.json` (exact pid + id)
  - `proc` — session id read from a process's argv (`--resume` / `--session-id`)
  - `heuristic` — fresh `claude` launches matched to their transcript by start time
  - **Boot-grace guard:** for the first 15 min after boot the snapshot may not
    *shrink* — so a post-boot run can't overwrite the pre-crash set before you
    restore it. (This was the second half of the 2026-06-24 failure.)
  - **Cliff guard:** boot grace only fires on a real *reboot*. A suspend-resume
    freeze (or X/terminal crash) leaves boot time untouched, so the next tick
    would overwrite the pre-freeze snapshot — it did, on 2026-06-30, costing a
    session. When the live set drops by ≥3 between two ticks (a mass death,
    unlike closing tabs one at a time), the pre-cliff set is copied to
    `~/.claude/sessions-recovery.json`, which the timer **never** clobbers.
- **`sessions-recovery.json`** — the durable high-water record from the cliff
  guard. The resume commands union it into their set, so a session that vanished
  from the live snapshot is still reopened. It's archived to `backups/` once a
  restore brings everything back, and ignored after 12 h so it can't go stale.
- **Tab-order memory:** the fleet tab bar (`wezterm/wezterm.lua`) publishes each
  pane's on-screen position to `~/.cache/wezterm-fleet-tab-order.json` every tick —
  the only place visual order is observable, since `wezterm cli list` returns panes
  unordered. `claude-snapshot` joins it to sessions by pid (`WEZTERM_PANE` from
  `/proc/<pid>/environ`, not the state file, which can name a dead pane) and stores
  it as `tab: [window, index]`. `resume_set()` sorts on it, so a restore rebuilds
  the bar as you arranged it — dragged tabs included. Sessions without a position
  sort by cwd after the rest.
- **`claude-resume`** — the on-demand restore command. Takes the resume set
  (snapshot ∪ recovery, minus sessions already live or whose transcript is gone)
  and spawns a `claude --resume <id>` tab per session via `wezterm cli spawn`, in
  remembered tab order.
  Run it from inside WezTerm. `--snapshot PATH` restores from a specific snapshot
  (e.g. the `.prev` backup); `--from-transcripts [N]` ignores the snapshot and
  reconstructs the N most-recently-active sessions from transcript files — the
  fallback for when no snapshot survived the reboot.

It skips any session whose transcript no longer exists, so a rolled-back id never
spawns a dead "No conversation found" tab — run `claude-restore-transcripts` first
to pull survivors out of backup.

## Reading (the scrollback-mangle escape hatch)

- **`claude-transcript`** — renders a session's transcript as clean text with
  logical lines intact, so the *terminal* does the wrapping instead of Claude
  Code's hard newlines (anthropics/claude-code#43113): output reflows on resize,
  the scrollbar tracks it, selections copy as unbroken paragraphs. No session,
  no tokens — it's a file formatter. `CTRL+SHIFT+H` in WezTerm opens the focused
  pane's session in a new tab (Ctrl+D closes); or `claude-transcript [SIDPREFIX]`,
  `--pane N`, `-p` to page in `less`. On-disk transcripts trail live output by a
  few seconds — this is for re-reading, not tailing.

## Usage

```sh
# after a freeze (from inside WezTerm):
claude-restore-transcripts        # put back any transcript that got rolled back
claude-resume                     # reopen everything that isn't already running, as tabs
claude-resume --dry-run           # show what it would open
claude-resume --snapshot ~/.claude/sessions-snapshot.prev.json   # use the backup snapshot
claude-resume --from-transcripts [N]    # ignore the snapshot; reconstruct the N
                                        # most-recently-active sessions from transcripts
```

Large sessions show Claude's own "resume from summary vs full?" prompt per tab.

## Install (already done on this machine)

```sh
# ~/.claude is a symlink to this repo; bin/ and systemd/user/ live here.
ln -sf "$HOME/.claude/bin/claude-resume" "$HOME/.local/bin/claude-resume"
for u in claude-snapshot claude-transcript-sync; do
  ln -sf "$HOME/.claude/systemd/user/$u.service" ~/.config/systemd/user/
  ln -sf "$HOME/.claude/systemd/user/$u.timer"   ~/.config/systemd/user/
  systemctl --user enable --now "$u.timer"
done
```

Timers run within the user's graphical session (no `loginctl enable-linger` needed —
recovery happens after you log back in anyway).

---

# Dual Max account picker

- **`claude-acct`** (policy in `_claude_acct_lib.py`, tests in `test_claude_acct.py`) —
  every interactive `claude` launch routes through it via the `claude()` function in
  `~/.bash_aliases` and lands on whichever Max account has the most headroom.
  Usage comes from the same endpoint `/usage` renders (`api/oauth/usage`), cached 60s
  under an flock.

  **Balancing is against each window's next reset, not raw usage.** A percentage
  means nothing on its own: 60% spent with twenty minutes to reset is nearly free,
  60% spent with four hours to go is scarce, and the account about to refill is the
  one worth burning. So every limit (5-hour, weekly, model-scoped weekly) gets a
  *slack* — capacity left over the capacity its window hands back in the time
  remaining. 1.0 is exactly on pace, below 1 is burning faster than it refills.
  Score sums session and weekly pressure (1/slack) and the lowest wins, so the
  scarcer window dominates on its own while the other still breaks ties between
  accounts whose pace matches. Expect the busier-looking account to be chosen when
  its windows reset sooner — that is the point, and the launch banner prints
  time-to-reset next to each percentage so the pick is legible.

  Accounts at/above 99% are skipped, and one too spent to carry a session now
  (session ≥90%) is passed over even when an imminent reset makes it look cheap —
  the caps and stall guard read raw percentages, only the score is time-normalized.
  Near-ties go to the least-recently-launched; a per-launch bump makes burst spawns
  (claude-resume) alternate. Tokens are never refreshed here — an idle account is
  judged from its last-good snapshot, with any window whose reset has passed counted
  as empty. Any wrapper failure fails open to a plain launch.

**Every launch first probes all accounts and prints one enumeration line** before
exec'ing claude, e.g. `claude-acct: claudepersonal s84%/2h11m w54%/3d16h · claude s12%/4h w41%/1d8h → claude`
(accounts are named by the subscription they log into — the email's local part).

**It stays out of the way on a bad day.** The wrapper runs on every launch, so
its failure modes matter more than its features:

- *Cost when nothing is wrong*: ~30ms of imports plus a cached probe.
  `urllib.request` alone costs 97ms to import, so it — and `concurrent.futures`
  and `subprocess` — are imported at their call sites instead of module level.
  A launch that hits the 60s usage cache needs none of them.
- *Offline*: the first launch pays one `FETCH_TIMEOUT` (3s) and then the failure
  is cached for `FAIL_TTL` (60s). Without that, every launch pays it again —
  and since the probe holds the cache lock while it waits, a claude-resume burst
  would serialize one dead connection per tab. Accounts still launch from their
  last-good snapshot; an unreachable API never reads as broken credentials.
- *A dead token*: it fails the health gate for every launch at once, so the
  whole fleet arrives at the login path together. The first tab runs
  `claude auth login`; the rest wait on a machine-wide lock (up to
  `LOGIN_WAIT`) and re-check when they get it — by then the account is usually
  already fixed, so they continue instead of each starting their own flow.
- *Wrapper missing*: the `claude()` function in `~/.bash_aliases` checks for
  `bin/claude-acct` on every call, not once at shell startup, and falls through
  to `command claude` when it isn't there. A shell that outlives the file (a
  branch checkout, a moved repo) keeps working.
- *Anything else*: any unexpected exception is caught and turned into a plain
  `claude` launch. The wrapper is never the reason a session can't start.

**Two working accounts are required.** With fewer — or with any configured account
whose credentials are broken — no session starts: the wrapper runs
`claude auth login` in the offending account's config dir (one flow at a time,
machine-wide flock, so a claude-resume burst can't start ten), then continues.
Where a login can't run (no tty), it exits with the command to run. Credential
states are distinguished carefully: an expired *access* token is healthy (claude
refreshes it at launch) and an unreachable API is healthy (offline laptops still
work); only a 401/403 or a dead *refresh* token counts as broken.

**Each account logs in through its own browser session.** `claude auth login`
hands its OAuth URL to `$BROWSER`; the wrapper points that at
`bin/claude-acct-browser`, which opens Brave on `~/.claude-browsers/<account>`.
Without this, a second login inside the default profile just re-authorizes
whoever is already signed into claude.ai there — two config dirs, one
subscription, no round-robin. A login landing on an email another account
already uses is rolled back (credentials dropped, that browser session moved
aside) rather than kept.

**Claude in Chrome is account-scoped** — verified 2026-08-18: with the extension
connected, a session on `main` listed 2 browsers while a session on `alt` listed
`[]`. So a browser session has to land where the extension actually is.
`--chrome` (or `--browser`) restricts selection to browser-capable accounts
rather than pinning one, so browser work balances again as soon as a second
account qualifies.

Capability is **detected from disk** — the extension present under
`<user-data-dir>/*/Extensions/<id>` in that account's browser — with
`"browser": true` in `accounts.json` as a manual override. No bookkeeping: the
moment you install the extension in an account's profile, it joins the pool.

The account binding is **the browser's environment**, not the manifest. A
native-messaging host inherits the environment of the browser that spawned it
(verified 2026-08-19: a live host carried `CLAUDE_ACCT_BROWSER_PROFILE` and
`BROWSER` straight from its browser), so `claude-acct-browser` exports that
profile's `CLAUDE_CONFIG_DIR` — recorded in `<user-data-dir>/claude-config-dir`
— before exec'ing the browser, and every host it spawns lands on the right
account. The shim also scrubs the `CLAUDE_CODE_*` variables of whatever session
opened the browser, so a host never inherits a session identity that isn't its.

Binding through the manifest instead does **not** survive, which is why it
isn't used: Brave reads native-messaging manifests only from its product
directory and ignores any inside a custom `--user-data-dir`, and Claude Code
rewrites that product-wide manifest to its own shim on every `--chrome` session
start (verified by reproduction) — so any manifest the wrapper asserts is undone
by the next session that starts. `chrome/` is therefore not in the shared
symlink set.

Installing the extension is **not** enough. It registers with no account until a
human opens it in that browser and signs in — the diagnostic is its storage
under `<user-data-dir>/Default/Local Extension Settings/<id>`, which holds
`accountUuid`/`connected` markers once activated. Capability therefore means
installed **and** activated **and** that browser running; the SessionStart hook
names whichever of the three is missing, and the exact click that fixes it.

That same hook also warns when the account it landed on has a **model-scoped
cap it routed around** — the picker chose this account for the launch model, so
a mid-session `/model` switch can hit a wall the session would otherwise not see
coming. It reads the cached snapshot only; a hook must never block a launch on
the network.

- **`claude-model`** — the way out of that wall. An account's model caps are fixed
  for the life of a session, but both accounts share one transcript store, so the
  *conversation* can move: `claude-model fable` picks the account with Fable
  headroom and spawns a tab that resumes this same session id there. The new tab
  **waits on the old session's pid** before starting claude — two live processes
  appending to one transcript is the failure this repo exists to prevent — so the
  handoff is: run it, `/exit` the old tab, the new one takes over by itself. It
  forces no account: `--model` is what makes `launch_model()` scope the caps, and
  the picker routes on its own. Bare `claude-model` prints this session's account
  and every model-scoped cap; `--dry-run` prints the spawn command.

**Links open in the account's own browser.** A URL from an `alt` session opening
in the default profile means claude.ai signed in as the *other* account, so both
ways of opening one route by session:

- **`claude-open <url>`** — resolves the profile from `CLAUDE_ACCT_BROWSER_PROFILE`
  and hands off to `claude-acct-browser`; the default account gets plain
  `brave-browser`. It used to launch a throwaway `--user-data-dir` per session,
  which correlated windows perfectly and was signed into nothing — a signed-in
  browser is worth more, so correlation (`--class=claude-<sid>` +
  `spawned-windows.log`) is now best-effort: once the account browser is running,
  new windows re-parent onto it.
- **a clicked link** — WezTerm's `open-uri` handler calls `claude-open --pane <id>`,
  which reads the profile out of `state/<sid>.json` (the session-state hook records
  `browser_profile` next to `wezterm_pane`). A pane that isn't a Claude session, or
  a non-`http(s)` URI, falls through to `xdg-open` exactly as before.

To give a second account a browser: `claude --acct-browser alt` opens its
profile (with the extension page and claude.ai when the extension is missing);
install the extension there and sign into claude.ai as that account.

```sh
claude                       # probes, enumerates, picks (stderr says which)
claude --acct claude@...     # force an account by email, local part or handle
                             # (also CLAUDE_ACCT=…) — escape hatch if the gate misfires
claude --chrome              # browser work: pool restricted to capable accounts
claude --acct-status         # credentials, session/weekly/score, every
                             # model-scoped cap, and Claude-in-Chrome readiness
claude --acct-login [name]   # collect credentials (all unhealthy, or just one)
claude --acct-browser [name] # open that account's own browser session
claude-model fable           # move THIS conversation to the account that has fable
claude-open <url>            # open a URL in this session's account browser
claude-acct --acct-setup bob # scaffold another account dir (~/.claude-bob)
command claude               # bypass the wrapper entirely
```

- Roster: `~/.claude/meta/accounts.json` (`configDir: null` = the default `~/.claude`).
- Account dirs (`~/.claude-alt`) hold per-account `.credentials.json` + seeded
  `.claude.json` (project trust + MCP approvals copied; identity/telemetry never;
  `autoUpdates` off — main owns the shared native install) and symlink everything
  shared back into this repo: config, `projects/` (transcripts+memory), `sessions/`
  (the fleet registry — resume/pickers see both accounts), `history.jsonl`,
  `file-history/`, `plans/`, `paste-cache/`, `todos/`, `tasks/`, `chrome/`.
- Maintenance subcommands (`auth mcp plugin update doctor install project agents
  setup-token …`) always run on the default account and skip the health gate — they
  are how a broken account gets fixed. A preexisting `CLAUDE_CONFIG_DIR` (daemon
  respawns, in-session children) bypasses everything, as do non-interactive shells
  (they never see the function).
- Runtime state: `state/acct-usage.json` (snapshot + probe cache),
  `state/acct-ledger.jsonl` (launch log, self-truncating), `state/acct-login.lock`.
- A duplicate-subscription check warns if two accounts sign in as the same email —
  round-robin across one quota pool would be a no-op.
