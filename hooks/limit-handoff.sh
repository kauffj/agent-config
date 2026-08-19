#!/usr/bin/env bash
# Notification hook: when a session hits its 5-hour cap, arm the handoff to the
# account that still has room.
#
# Hitting the cap mid-turn used to mean noticing the banner, remembering the
# session id, exiting, and resuming by hand — while the other subscription sat
# idle. Both accounts share one transcript store, so the conversation itself can
# move. This spawns a tab that waits for this session's process to exit and then
# resumes it wherever the picker has headroom. Nothing happens until you /exit,
# and closing the tab cancels it.
#
# Gated in-script rather than by a hook matcher: Notification matchers do not
# filter on notification_type, so a matcher here would fire on every
# notification (the same bug this repo already fixed once in session-state.sh).
set -uo pipefail

INPUT=$(cat)
eval "$(jq -r '@sh "ntype=\(.notification_type // "") sid=\(.session_id // "")"' \
        <<<"$INPUT" 2>/dev/null)"

[[ "${ntype:-}" == "limit_reached" ]] || exit 0
[[ -n "${sid:-}" ]] || exit 0

# One handoff per session: the notification can repeat, and each spawn would be
# another tab waiting on the same pid.
marker="${TMPDIR:-/tmp}/claude-limit-handoff-${sid}"
[[ -e "$marker" ]] && exit 0
: > "$marker"

log="$HOME/.claude/state/limit-handoff.log"
{
    printf '%s %s ' "$(date -Is)" "${sid:0:8}"
    timeout 20 "$HOME/.claude/bin/claude-model" --continue --session "$sid" 2>&1 \
        | tr '\n' ' '
    printf '\n'
} >>"$log" 2>&1

exit 0
