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
CODEX_HOOKS="$(dirname "$HOOK")/../codex/hooks.json"
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

fire_codex() { # same payload, but exercise Codex's silent-output contract
  local ev=$1; shift
  local extra=""
  for kv in "$@"; do extra+=",$kv"; done
  printf '{"hook_event_name":"%s","session_id":"%s","cwd":"%s"%s}' \
    "$ev" "$SID" "$CWD" "$extra" | "$HOOK" codex 2>/dev/null
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

allowlisted_sequence() { # allowlisted_sequence <hook-stdout> <label>
  local seq ok=1
  seq=$(jq -r '.terminalSequence // ""' <<<"$1" 2>/dev/null)
  case "$seq" in
    *SetUserVar*) ok=0 ;;
    *) : ;;
  esac
  if [[ "$ok" == 1 ]]; then
    printf '  ok    %-28s terminal sequence allowlisted\n' "$2"
  else
    printf '  FAIL  %-28s terminal sequence contains rejected OSC\n' "$2"
    fails=$((fails + 1))
  fi
}

echo "Codex config wires every status transition:"
for ev in SessionStart UserPromptSubmit PreToolUse PermissionRequest SubagentStart SubagentStop Stop; do
  command=$(jq -r --arg ev "$ev" '.hooks[$ev][0].hooks[0].command // ""' "$CODEX_HOOKS")
  [ "$command" = '~/.config/agent-config/hooks/session-state.sh codex' ] \
    && printf '  ok    %-28s wired\n' "$ev" \
    || { printf '  FAIL  %-28s command=%q\n' "$ev" "$command"; fails=$((fails+1)); }
done

echo "malformed session ids cannot become state paths:"
SID='../escape'; fire SessionStart >/dev/null
escape_name='escape.json'
[ ! -e "$HOME/.claude/$escape_name" ] \
  && printf '  ok    %-28s rejected\n' "path traversal" \
  || { printf '  FAIL  %-28s file appeared\n' "path traversal"; fails=$((fails+1)); }
SID='test-session-1'; SF="$HOME/.claude/state/$SID.json"

echo "lifecycle without subagents stays as before:"
out=$(WEZTERM_PANE=42 fire SessionStart)
expect status waiting  "SessionStart"
expect agents 0        "SessionStart"
marker "$out" '●'      "SessionStart"
allowlisted_sequence "$out" "SessionStart"
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

echo "Codex lifecycle events are authoritative and silent:"
SID="test-codex-session"; SF="$HOME/.claude/state/$SID.json"
export WEZTERM_PANE=91
out=$(fire_codex SessionStart)
expect status waiting       "Codex SessionStart"
expect vendor codex         "Codex vendor"
expect status_source hook   "Codex status source"
expect wezterm_pane 91      "Codex pane"
[ -z "$out" ] \
  && printf '  ok    %-28s stdout empty\n' "Codex SessionStart" \
  || { printf '  FAIL  %-28s stdout=%q\n' "Codex SessionStart" "$out"; fails=$((fails+1)); }

fire_codex UserPromptSubmit >/dev/null
expect status working       "Codex prompt submitted"
fire_codex PreToolUse '"tool_name":"Agent"' >/dev/null
expect agents 0             "Codex no PreTool double-count"
fire_codex SubagentStart '"agent_id":"sub-codex"' >/dev/null
expect agents 1             "Codex SubagentStart"
out=$(fire_codex Stop)
expect status delegating    "Codex Stop with subagent"
[ -z "$out" ] \
  && printf '  ok    %-28s stdout empty\n' "Codex Stop" \
  || { printf '  FAIL  %-28s stdout=%q\n' "Codex Stop" "$out"; fails=$((fails+1)); }
fire_codex SubagentStop '"agent_id":"sub-codex"' >/dev/null
expect agents 0             "Codex SubagentStop"
fire_codex Stop >/dev/null
expect status waiting       "Codex final Stop"

rm -rf "$HOME"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILURE(S)"; exit 1; fi
