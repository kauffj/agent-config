#!/usr/bin/env bash
# Unified Claude/Codex session-state hook — the single writer of live state.
#
# Fired on every lifecycle event, it does four things in order and ALWAYS exits 0,
# so a bug here can never wedge a session:
#   1. Derives status (working|waiting|delegating) from the hook event.
#   2. Writes ~/.claude/state/<session_id>.json
#      {status, since, agents, wezterm_pane, cwd, browser_profile}. browser_profile
#      is the account binding claude-acct put in the environment — it is what lets
#      claude-open route a clicked link (which knows only a pane) into the
#      browser of the account this session runs on. Empty = the default account.
#      `since` only moves on a real status *transition*, so "waiting 14m" stays
#      truthful across repeated same-status events.
#   3. For Claude, emits the OSC-0 tab title + idle marker via terminalSequence
#      (Codex reads the same state file directly and expects silent hook output).
#   4. Plays a one-shot sound on the transition *into* waiting (toggleable).
#
# Status is derived from the event, not passed as an arg, so every event wires to
# this one script. Renderers (picker, statusline, tab bar) compute idle/urgency
# *tiers* from the age of `since`; this hook records only the discrete state.
# Owns the tab title outright: the per-session label + subagent-guard logic.
#
# `delegating` = the turn ended but background subagents are still running; the
# task notification will wake the session, so it is NOT waiting on you. Tracked
# by `agents`: Claude counts Task/Agent PreToolUse; Codex has SubagentStart;
# both decrement on SubagentStop (clamped at 0). The counter can only stick HIGH
# (a killed subagent never fires SubagentStop), which would silence a tab
# forever — so renderers degrade a
# `delegating` whose `updated` has gone stale (>30m; every SubagentStop bumps it)
# back to `waiting`: the worst drift is a late ping, never a missed one.
# Known gaps that still read as waiting: background Bash and Workflow runs —
# neither has a completion event this hook can see.

# Base dir via a variable so the runtime paths below (state/, sound-off flags)
# aren't read as shipped-config references by lib/doctor.mjs, which flags any
# literal $HOME/.claude/<file> that's absent on a clean checkout.
CLAUDE_DIR="$HOME/.claude"
STATE_DIR="$CLAUDE_DIR/state"
HARNESS="${1:-claude}"
[[ "$HARNESS" == "codex" ]] || HARNESS=claude
INPUT=$(cat)

# One jq pass -> shell-escaped assignments -> eval. @sh quotes each field safely,
# so empty fields stay empty (no IFS-collapse) and hostile values can't inject.
event= agent_id= session_id= cwd= stop_active= ntype= tool_name=
eval "$(jq -r '@sh "event=\(.hook_event_name // "") agent_id=\(.agent_id // "") session_id=\(.session_id // "") cwd=\(.cwd // "") stop_active=\(.stop_hook_active // false | tostring) ntype=\(.notification_type // "") tool_name=\(.tool_name // "")"' <<<"$INPUT" 2>/dev/null)"

# Stop hooks can re-fire recursively.
[[ "$stop_active" == "true" ]] && exit 0
[[ -z "$session_id" ]] && exit 0
# Session ids become filenames. Both harnesses currently send compact UUID-like
# identifiers; keep future malformed payloads from escaping state/ or creating
# pathological names at this trusted hook boundary.
[[ ${#session_id} -le 128 && "$session_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || exit 0

# Bind the full native session id to the pane itself. Unlike pane numbers, this
# user variable dies with the pane and survives mux clients reconnecting; WezTerm
# publishes the current set for sandboxed recovery commands that cannot see host
# /proc. A direct terminal write is the only valid OSC 1337 route: Claude's
# terminalSequence hook field allowlists notification/title OSCs and rejects
# user variables. When its sandbox makes /dev/tty unwritable, the picker instead
# authenticates the host registry's session + process + pane tuple.
identity_seq=""
if [[ -n "${WEZTERM_PANE:-}" ]] && command -v base64 >/dev/null; then
  encoded_sid=$(printf %s "$session_id" | base64 -w 0)
  identity_seq=$(printf '\033]1337;SetUserVar=agent_session=%s\007' "$encoded_sid")
  if [[ -w /dev/tty ]]; then
    printf %s "$identity_seq" >/dev/tty 2>/dev/null || true
  fi
fi

state_file="$STATE_DIR/$session_id.json"
mkdir -p "$STATE_DIR"

# Parallel subagents finish concurrently, so SubagentStop events race the
# read-modify-write of `agents`. One shared lock serializes them; best-effort
# with a timeout — a lost counter update self-heals (clamp + stale-degrade),
# a wedged hook would not. The .lock file has no .json suffix, so the
# claude-snapshot reaper and the wezterm state glob never see it.
exec 9>>"$STATE_DIR/.lock" 2>/dev/null && flock -w 2 9 2>/dev/null

prev_status= prev_since= agents=0
[[ -f "$state_file" ]] && eval "$(jq -r '@sh "prev_status=\(.status // "") prev_since=\(.since // 0) agents=\(.agents // 0)"' "$state_file" 2>/dev/null)"
[[ "$agents" =~ ^[0-9]+$ ]] || agents=0
now=$(printf '%(%s)T' -1)

# Subagent lifecycle events are pure bookkeeping: Codex exposes the launch
# directly, and both harnesses expose completion. Touch no status — the parent
# remains working until its own Stop parks it. Each event bumps `updated` as
# proof of life; an orphan event with no tracked parent does nothing.
if [[ "$event" == "SubagentStop" \
      || ( "$HARNESS" == "codex" && "$event" == "SubagentStart" ) ]]; then
  [[ -f "$state_file" ]] || exit 0
  if [[ "$event" == "SubagentStart" ]]; then
    agents=$((agents + 1))
  else
    (( agents > 0 )) && agents=$((agents - 1))
  fi
  tmp="$state_file.$$"
  if jq -c --argjson agents "$agents" --argjson updated "$now" \
        '.agents = $agents | .updated = $updated' "$state_file" >"$tmp" 2>/dev/null; then
    mv -f "$tmp" "$state_file" 2>/dev/null
  fi
  rm -f "$tmp" 2>/dev/null
  exit 0
fi

# Event -> status (+ which sound a fresh wait should play). Uninteresting events
# (auth_success, elicitation_*, unknown) leave state untouched.
snd=""
if [[ -n "$agent_id" ]]; then
  # Subagent events: exactly two matter. A subagent's permission prompt IS real
  # attention (the parent UI shows the dialog); its next tool call after your
  # approval returns the tab to delegating. Everything else a subagent does must
  # not flip the marker.
  case "$event" in
    PermissionRequest) status=waiting; snd=message ;;
    PreToolUse)
      [[ "$prev_status" == "waiting" && "$agents" -gt 0 ]] || exit 0
      status=delegating ;;
    *) exit 0 ;;
  esac
else
  case "$event" in
    Stop)
      if (( agents > 0 )); then
        status=delegating           # parked on subagents; their notification wakes us
      else
        status=waiting; snd=complete
      fi ;;
    PermissionRequest)  status=waiting; snd=message ;;
    Notification)
      case "$ntype" in
        idle_prompt)
          # With agents in flight the "idle" prompt is the parked delegation,
          # not you being needed — don't cry wolf.
          if (( agents > 0 )); then status=delegating; else status=waiting; snd=message; fi ;;
        permission_prompt) status=waiting; snd=message ;;
        *) exit 0 ;;
      esac ;;
    UserPromptSubmit) status=working; agents=0 ;;   # user present = safe reconcile point
    PreToolUse)
      status=working
      # Claude has no SubagentStart, so its Task/Agent tool call is the launch
      # signal. Codex does have it; counting both would double every child.
      [[ "$HARNESS" == "claude" && "$tool_name" =~ ^(Task|Agent)$ ]] \
        && agents=$((agents + 1)) ;;
    SessionStart)     status=waiting; agents=0 ;;   # fresh/resumed session awaits you
    *) exit 0 ;;
  esac
fi

# Preserve `since` across same-status events; reset it on a real transition.
if [[ "$status" == "$prev_status" && -n "$prev_since" && "$prev_since" != "0" ]]; then
  since="$prev_since"
else
  since="$now"
fi

# A resume/startup isn't fresh attention. When SessionStart can't inherit a
# prior wait (state file long reaped, or the session died mid-work), seed
# `since` from the transcript's last real turn — user/assistant entries carry
# timestamps; the metadata a reopen appends doesn't — so an old session
# reopened days later shows its true idle age, not a calm fresh tab.
# SessionStart ONLY: a live session's on-disk transcript can lag minutes
# behind (writes are buffered), so this signal is truthful just at reopen,
# when the closed session's file is fully flushed.
if [[ "$HARNESS" == "claude" && "$event" == "SessionStart" \
      && "$since" == "$now" && -n "$cwd" ]]; then
  tf="$CLAUDE_DIR/projects/${cwd//\//-}/$session_id.jsonl"
  ts=$(tail -n 500 "$tf" 2>/dev/null \
       | jq -r 'select(.timestamp and (.type=="user" or .type=="assistant")) | .timestamp' 2>/dev/null \
       | tail -n 1)
  if [[ -n "$ts" ]]; then
    t=$(date -d "$ts" +%s 2>/dev/null)
    [[ -n "$t" && "$t" -lt "$now" ]] && since="$t"
  fi
fi

tmp="$state_file.$$"
if jq -nc --arg sid "$session_id" --arg st "$status" \
       --argjson since "$since" --argjson updated "$now" \
       --argjson agents "$agents" \
       --arg pane "${WEZTERM_PANE:-}" --arg cwd "$cwd" \
       --arg profile "${CLAUDE_ACCT_BROWSER_PROFILE:-}" --arg harness "$HARNESS" \
     '{session_id:$sid, status:$st, since:$since, updated:$updated, agents:$agents,
       cwd:$cwd, browser_profile:$profile}
      + (if $harness == "codex" then {vendor:"codex", status_source:"hook"} else {} end)
      + (if $pane != "" then {wezterm_pane:$pane} else {} end)' >"$tmp" 2>/dev/null; then
  mv -f "$tmp" "$state_file" 2>/dev/null
fi
rm -f "$tmp" 2>/dev/null
flock -u 9 2>/dev/null

# Sound: only on the transition INTO waiting, and only if not muted. Entering
# `delegating` is silent by design; the eventual real Stop plays `complete`.
if [[ "$status" == "waiting" && "$prev_status" != "waiting" && -n "$snd" ]]; then
  if [[ ! -f "$CLAUDE_DIR/sound-off" && ! -f "$CLAUDE_DIR/sound-off-$session_id" ]]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    setsid -f canberra-gtk-play -i "$snd" >/dev/null 2>&1 || true
  fi
fi

# Codex hook stdout is a control channel, not a terminal escape channel. The
# state file above is authoritative and WezTerm reads it on its next tick.
[[ "$HARNESS" == "codex" ]] && exit 0

# Tab title + marker (OSC 0 via terminalSequence). Marker is a SUFFIX so it
# survives title truncation; '●' = waiting on you, '◐' = waiting on subagents.
project="${cwd##*/}"
label_file="/tmp/.tab-label-$session_id"
if [[ -f "$label_file" ]]; then
  label=$(<"$label_file")
else
  branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null)
  label="${branch:-$project} ·${session_id:0:4}"
fi
marker=""
[[ "$status" == "waiting" ]] && marker="●"
[[ "$status" == "delegating" ]] && marker="◐"
title="$project $label"; [[ -n "$marker" ]] && title="$title $marker"
seq=$(printf '\033]0;%s\007' "$title")
jq -nc --arg seq "$seq" '{terminalSequence:$seq}'
exit 0
