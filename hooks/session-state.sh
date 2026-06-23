#!/usr/bin/env bash
# Unified per-session state hook — the single writer of a session's live state.
#
# Fired on every lifecycle event, it does four things in order and ALWAYS exits 0,
# so a bug here can never wedge a session:
#   1. Derives status (working|waiting) from the hook event.
#   2. Writes ~/.claude/state/<session_id>.json {status, since, wezterm_pane, cwd}.
#      `since` only moves on a real status *transition*, so "waiting 14m" stays
#      truthful across repeated same-status events.
#   3. Emits the OSC-0 tab title + idle marker via the terminalSequence field
#      (hooks have no tty; Claude Code writes the escape to the PTY for us).
#   4. Plays a one-shot sound on the transition *into* waiting (toggleable).
#
# Status is derived from the event, not passed as an arg, so every event wires to
# this one script. Renderers (picker, statusline, tab bar) compute idle/urgency
# *tiers* from the age of `since`; this hook records only the discrete state.
# Supersedes hooks/tab-title.sh (folds in its label + subagent-guard logic).

# Base dir via a variable so the runtime paths below (state/, sound-off flags)
# aren't read as shipped-config references by lib/doctor.mjs, which flags any
# literal $HOME/.claude/<file> that's absent on a clean checkout.
CLAUDE_DIR="$HOME/.claude"
STATE_DIR="$CLAUDE_DIR/state"
INPUT=$(cat)

# One jq pass -> shell-escaped assignments -> eval. @sh quotes each field safely,
# so empty fields stay empty (no IFS-collapse) and hostile values can't inject.
event= agent_id= session_id= cwd= stop_active= ntype=
eval "$(jq -r '@sh "event=\(.hook_event_name // "") agent_id=\(.agent_id // "") session_id=\(.session_id // "") cwd=\(.cwd // "") stop_active=\(.stop_hook_active // false | tostring) ntype=\(.notification_type // "")"' <<<"$INPUT" 2>/dev/null)"

# Only the MAIN agent drives session state. Subagent events carry agent_id (or are
# SubagentStop) — ignore them so a background subagent can't flip the marker.
[[ -n "$agent_id" || "$event" == "SubagentStop" ]] && exit 0
# Stop hooks can re-fire recursively.
[[ "$stop_active" == "true" ]] && exit 0
[[ -z "$session_id" ]] && exit 0

# Event -> status (+ which sound a fresh wait should play). Uninteresting events
# (auth_success, elicitation_*, unknown) leave state untouched.
snd=""
case "$event" in
  Stop)               status=waiting; snd=complete ;;
  PermissionRequest)  status=waiting; snd=message ;;
  Notification)
    case "$ntype" in
      idle_prompt)       status=waiting; snd=message ;;
      permission_prompt) status=waiting; snd=message ;;
      *) exit 0 ;;
    esac ;;
  UserPromptSubmit|PreToolUse) status=working ;;
  SessionStart)                status=waiting ;;   # fresh/resumed session awaits you
  *) exit 0 ;;
esac

# Preserve `since` across same-status events; reset it on a real transition.
state_file="$STATE_DIR/$session_id.json"
prev_status= prev_since=
[[ -f "$state_file" ]] && eval "$(jq -r '@sh "prev_status=\(.status // "") prev_since=\(.since // 0)"' "$state_file" 2>/dev/null)"
now=$(printf '%(%s)T' -1)
if [[ "$status" == "$prev_status" && -n "$prev_since" && "$prev_since" != "0" ]]; then
  since="$prev_since"
else
  since="$now"
fi

mkdir -p "$STATE_DIR"
tmp="$state_file.$$"
if jq -nc --arg sid "$session_id" --arg st "$status" \
       --argjson since "$since" --argjson updated "$now" \
       --arg pane "${WEZTERM_PANE:-}" --arg cwd "$cwd" \
     '{session_id:$sid, status:$st, since:$since, updated:$updated, cwd:$cwd}
      + (if $pane != "" then {wezterm_pane:$pane} else {} end)' >"$tmp" 2>/dev/null; then
  mv -f "$tmp" "$state_file" 2>/dev/null
fi
rm -f "$tmp" 2>/dev/null

# Sound: only on the transition INTO waiting, and only if not muted.
if [[ "$status" == "waiting" && "$prev_status" != "waiting" && -n "$snd" ]]; then
  if [[ ! -f "$CLAUDE_DIR/sound-off" && ! -f "$CLAUDE_DIR/sound-off-$session_id" ]]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    setsid -f canberra-gtk-play -i "$snd" >/dev/null 2>&1 || true
  fi
fi

# Tab title + marker (OSC 0 via terminalSequence). Marker is a SUFFIX because
# Tilix left-trims long titles; '●' = waiting on you.
project="${cwd##*/}"
label_file="/tmp/.tab-label-$session_id"
if [[ -f "$label_file" ]]; then
  label=$(<"$label_file")
else
  branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null)
  label="${branch:-$project} ·${session_id:0:4}"
fi
marker=""; [[ "$status" == "waiting" ]] && marker="●"
title="$project $label"; [[ -n "$marker" ]] && title="$title $marker"
seq=$(printf '\033]0;%s\007' "$title")
jq -nc --arg seq "$seq" '{terminalSequence:$seq}'
exit 0
