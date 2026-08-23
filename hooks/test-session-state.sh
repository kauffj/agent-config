#!/usr/bin/env bash
# Regression tests for the session-state hook's status machine.
#
#   bash hooks/test-session-state.sh
#
# Focus: the `delegating` third state (turn parked on background subagents).
# The failure mode being guarded against is BOTH-sided: a tab that dings/●'s
# while it will wake itself (the old false-waiting bug), and a tab that stays
# silent forever because the agents counter stuck high (the inversion the
# clamp + renderer degrade exist for). Runs against a throwaway $HOME so real
# session state is never touched.

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/session-state.sh"
export HOME=$(mktemp -d)
mkdir -p "$HOME/.claude"
touch "$HOME/.claude/sound-off"          # never ding from a test run
CWD="$HOME/proj"; mkdir -p "$CWD"
SID="test-session-1"
SF="$HOME/.claude/state/$SID.json"
fails=0

fire() { # fire <event> [extra-json-pairs ...] -> hook stdout
  local ev=$1; shift
  local extra=""
  for kv in "$@"; do extra+=",$kv"; done
  printf '{"hook_event_name":"%s","session_id":"%s","cwd":"%s"%s}' \
    "$ev" "$SID" "$CWD" "$extra" | "$HOOK" 2>/dev/null
}

expect() { # expect <jq-field> <want> <label>
  local got
  got=$(jq -r ".$1 // \"\"" "$SF" 2>/dev/null)
  if [ "$got" = "$2" ]; then
    printf '  ok    %-28s %s=%s\n' "$3" "$1" "$got"
  else
    printf '  FAIL  %-28s %s: want=%s got=%s\n' "$3" "$1" "$2" "$got"
    fails=$((fails + 1))
  fi
}

marker() { # marker <hook-stdout> <glyph|none> <label>
  local seq
  seq=$(jq -r '.terminalSequence // ""' <<<"$1" 2>/dev/null)
  local want=$2 ok=1
  case "$want" in
    none) case "$seq" in *●*|*◐*) ok=0 ;; esac ;;
    *)    case "$seq" in *"$want"*) : ;; *) ok=0 ;; esac ;;
  esac
  if [ "$ok" = 1 ]; then
    printf '  ok    %-28s marker=%s\n' "$3" "$want"
  else
    printf '  FAIL  %-28s marker: want=%s seq=%q\n' "$3" "$want" "$seq"
    fails=$((fails + 1))
  fi
}

echo "lifecycle without subagents stays as before:"
out=$(fire SessionStart)
expect status waiting  "SessionStart"
expect agents 0        "SessionStart"
marker "$out" '●'      "SessionStart"
out=$(fire UserPromptSubmit)
expect status working  "UserPromptSubmit"
marker "$out" none     "UserPromptSubmit"
fire PreToolUse '"tool_name":"Read"' >/dev/null
expect agents 0        "plain tool call"
out=$(fire Stop)
expect status waiting  "Stop, no agents"
marker "$out" '●'      "Stop, no agents"

echo "launching subagents makes Stop delegating, not waiting:"
fire UserPromptSubmit >/dev/null
fire PreToolUse '"tool_name":"Task"'  >/dev/null
fire PreToolUse '"tool_name":"Agent"' >/dev/null
expect agents 2        "two launches counted"
out=$(fire Stop)
expect status delegating "Stop with agents in flight"
marker "$out" '◐'        "Stop with agents in flight"
since1=$(jq -r .since "$SF")
out=$(fire Stop)
[ "$(jq -r .since "$SF")" = "$since1" ] \
  && printf '  ok    %-28s since preserved\n' "repeat Stop" \
  || { printf '  FAIL  %-28s since moved on same-status event\n' "repeat Stop"; fails=$((fails+1)); }

echo "SubagentStop is pure bookkeeping:"
upd1=$(jq -r .updated "$SF")
sleep 1
fire SubagentStop '"agent_id":"sub-a"' >/dev/null
expect agents 1          "first SubagentStop"
expect status delegating "first SubagentStop"
upd2=$(jq -r .updated "$SF")
[ "$upd2" -gt "$upd1" ] \
  && printf '  ok    %-28s updated bumped (proof of life)\n' "SubagentStop" \
  || { printf '  FAIL  %-28s updated not bumped (%s -> %s)\n' "SubagentStop" "$upd1" "$upd2"; fails=$((fails+1)); }

echo "a subagent's permission prompt is real attention:"
out=$(fire PermissionRequest '"agent_id":"sub-b"')
expect status waiting  "subagent PermissionRequest"
marker "$out" '●'      "subagent PermissionRequest"
fire PreToolUse '"agent_id":"sub-b","tool_name":"Bash"' >/dev/null
expect status delegating "subagent resumes after approval"

echo "last SubagentStop + wake-up Stop = the real waiting:"
fire SubagentStop '"agent_id":"sub-b"' >/dev/null
expect agents 0        "count drained"
expect status delegating "still delegating until wake-up Stop"
out=$(fire Stop)
expect status waiting  "wake-up Stop"
marker "$out" '●'      "wake-up Stop"
fire SubagentStop '"agent_id":"stray"' >/dev/null
expect agents 0        "clamp at zero (Workflow strays)"

echo "idle_prompt respects in-flight agents:"
fire UserPromptSubmit >/dev/null
fire PreToolUse '"tool_name":"Task"' >/dev/null
fire Stop >/dev/null
fire Notification '"notification_type":"idle_prompt"' >/dev/null
expect status delegating "idle_prompt while delegating"
fire SubagentStop >/dev/null
fire Stop >/dev/null
fire Notification '"notification_type":"idle_prompt"' >/dev/null
expect status waiting  "idle_prompt with no agents"

echo "user presence reconciles a drifted counter:"
fire UserPromptSubmit >/dev/null
fire PreToolUse '"tool_name":"Task"' >/dev/null
expect agents 1        "launch before reconcile"
fire UserPromptSubmit >/dev/null
expect agents 0        "UserPromptSubmit resets"

echo "subagent noise cannot flip a working tab:"
fire PreToolUse '"agent_id":"sub-c","tool_name":"Bash"' >/dev/null
expect status working  "subagent PreToolUse while working"

echo "SubagentStop with no state file writes nothing:"
SID="test-session-2"
fire SubagentStop >/dev/null
[ ! -e "$HOME/.claude/state/$SID.json" ] \
  && printf '  ok    %-28s no file created\n' "orphan SubagentStop" \
  || { printf '  FAIL  %-28s state file appeared\n' "orphan SubagentStop"; fails=$((fails+1)); }

rm -rf "$HOME"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILURE(S)"; exit 1; fi
