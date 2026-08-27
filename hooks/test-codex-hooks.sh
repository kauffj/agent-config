#!/usr/bin/env bash
# Regression tests for the essential Codex hook adapter.
#
#   bash hooks/test-codex-hooks.sh

set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CONFIG="$ROOT/codex/hooks.json"
LESSONS="$ROOT/hooks/inject-lessons.sh"
GUARD="$ROOT/hooks/on-pre-tool.sh"
FIXTURE_HOME=$(mktemp -d)
trap 'rm -rf "$FIXTURE_HOME"' EXIT
fails=0

ok() {
  printf '  ok    %s\n' "$1"
}

fail() {
  printf '  FAIL  %s\n' "$1"
  fails=$((fails + 1))
}

expect_jq() { # expect_jq <label> <jq expression>
  local label=$1 expression=$2
  if jq -e "$expression" "$CONFIG" >/dev/null; then
    ok "$label"
  else
    fail "$label"
  fi
}

echo "Codex config keeps lifecycle hooks and separates essential responsibilities:"
for event in SessionStart UserPromptSubmit PreToolUse PermissionRequest SubagentStart SubagentStop Stop; do
  command=$(jq -r --arg event "$event" '.hooks[$event][0].hooks[0].command // empty' "$CONFIG")
  if [ "$command" = '~/.config/agent-config/hooks/session-state.sh codex' ]; then
    ok "$event fleet hook"
  else
    fail "$event fleet hook: command=$command"
  fi
done

expect_jq "independent SessionStart hooks" \
  '.hooks.SessionStart | length == 3 and all(.[]; (.hooks | length) == 1)'
expect_jq "doctor reports at SessionStart" \
  '[.hooks.SessionStart[].hooks[].command] | index("node ~/.config/agent-config/lib/doctor.mjs || true") != null'
expect_jq "lessons inject at SessionStart" \
  '[.hooks.SessionStart[].hooks[].command] | index("~/.config/agent-config/hooks/inject-lessons.sh") != null'
expect_jq "one shared safety hook" \
  '[.hooks.PreToolUse[] | select(.matcher == "Bash|Edit|Write") | .hooks[] | select(.command == "~/.config/agent-config/hooks/on-pre-tool.sh")] | length == 1'
expect_jq "all commands use neutral install paths" \
  '[.hooks[][]?.hooks[]?.command] | all(test("~/.config/agent-config")) and all(test("\\.claude") | not)'
expect_jq "Claude-only lifecycle events stay absent" \
  '. as $config | ["PostToolUse", "Notification", "SessionEnd"] | all(. as $event | ($config.hooks[$event] == null))'
expect_jq "Claude-only hook commands stay absent" \
  '[.hooks[][]?.hooks[]?.command] | all(test("fsync-transcript|session-account|limit-handoff|on-edit-format") | not)'

echo "SessionStart lessons use Codex cwd and retain Claude compatibility:"
CODEX_PROJECT="$FIXTURE_HOME/codex-project"
CLAUDE_PROJECT="$FIXTURE_HOME/claude-project"
mkdir -p "$CODEX_PROJECT/tasks" "$CLAUDE_PROJECT/tasks"
printf '%s\n' '- codex cwd lesson' >"$CODEX_PROJECT/tasks/lessons.md"
printf '%s\n' '- claude env lesson' >"$CLAUDE_PROJECT/tasks/lessons.md"

codex_output=$(
  cd "$FIXTURE_HOME" && env -u CLAUDE_PROJECT_DIR \
    "$LESSONS" <<EOF
{"session_id":"codex-session","cwd":"$CODEX_PROJECT","hook_event_name":"SessionStart","source":"startup"}
EOF
)
if jq -e \
  '.hookSpecificOutput.hookEventName == "SessionStart"
   and (.hookSpecificOutput.additionalContext | contains("codex cwd lesson"))' \
  <<<"$codex_output" >/dev/null; then
  ok "Codex cwd selects project lessons"
else
  fail "Codex cwd selects project lessons: output=$codex_output"
fi

claude_output=$(
  CLAUDE_PROJECT_DIR="$CLAUDE_PROJECT" "$LESSONS" <<EOF
{"session_id":"claude-session","cwd":"$CODEX_PROJECT","hook_event_name":"SessionStart"}
EOF
)
if jq -e \
  '(.hookSpecificOutput.additionalContext | contains("claude env lesson"))
   and (.hookSpecificOutput.additionalContext | contains("codex cwd lesson") | not)' \
  <<<"$claude_output" >/dev/null; then
  ok "CLAUDE_PROJECT_DIR remains authoritative"
else
  fail "CLAUDE_PROJECT_DIR remains authoritative: output=$claude_output"
fi

empty_output=$(env -u CLAUDE_PROJECT_DIR "$LESSONS" <<EOF
{"session_id":"codex-session","cwd":"$FIXTURE_HOME/missing","hook_event_name":"SessionStart","source":"resume"}
EOF
)
if [ -z "$empty_output" ]; then
  ok "missing lessons are silent"
else
  fail "missing lessons are silent: output=$empty_output"
fi

OUTSIDE_LESSONS="$FIXTURE_HOME/outside-lessons"
LINK_PROJECT="$FIXTURE_HOME/link-project"
printf '%s\n' 'must not be injected' >"$OUTSIDE_LESSONS"
mkdir -p "$LINK_PROJECT/tasks"
ln -s "$OUTSIDE_LESSONS" "$LINK_PROJECT/tasks/lessons.md"
link_output=$(env -u CLAUDE_PROJECT_DIR "$LESSONS" <<EOF
{"session_id":"codex-session","cwd":"$LINK_PROJECT","hook_event_name":"SessionStart"}
EOF
)
if [ -z "$link_output" ]; then
  ok "symlinked lessons are not injected"
else
  fail "symlinked lessons are not injected: output=$link_output"
fi

PARENT_LINK_PROJECT="$FIXTURE_HOME/parent-link-project"
mkdir -p "$PARENT_LINK_PROJECT"
ln -s "$CODEX_PROJECT/tasks" "$PARENT_LINK_PROJECT/tasks"
parent_link_output=$(env -u CLAUDE_PROJECT_DIR "$LESSONS" <<EOF
{"session_id":"codex-session","cwd":"$PARENT_LINK_PROJECT","hook_event_name":"SessionStart"}
EOF
)
if [ -z "$parent_link_output" ]; then
  ok "lessons outside canonical project root are not injected"
else
  fail "lessons outside canonical project root are not injected: output=$parent_link_output"
fi

echo "Doctor command reports through its neutral install path:"
mkdir -p "$FIXTURE_HOME/.config/agent-config/lib"
printf '%s\n' 'console.log("fixture doctor report")' \
  >"$FIXTURE_HOME/.config/agent-config/lib/doctor.mjs"
doctor_command=$(jq -r \
  '.hooks.SessionStart[].hooks[].command | select(contains("doctor.mjs"))' "$CONFIG")
doctor_output=$(HOME="$FIXTURE_HOME" bash -c "$doctor_command")
if [ "$doctor_output" = "fixture doctor report" ]; then
  ok "doctor output reaches SessionStart stdout"
else
  fail "doctor output reaches SessionStart stdout: output=$doctor_output"
fi

run_guard() { # run_guard <tool> <command> [cwd]
  local tool=$1 command=$2 cwd=${3:-$ROOT}
  jq -n \
    --arg tool "$tool" \
    --arg command "$command" \
    --arg cwd "$cwd" \
    '{session_id:"codex-session",turn_id:"turn-1",tool_use_id:"tool-1",
      hook_event_name:"PreToolUse",tool_name:$tool,
      tool_input:{command:$command},cwd:$cwd}' \
    | "$GUARD" >/dev/null 2>&1
}

expect_guard() { # expect_guard <ALLOW|BLOCK> <label> <tool> <command>
  local want=$1 label=$2 tool=$3 command=$4 status
  run_guard "$tool" "$command"
  status=$?
  if { [ "$want" = ALLOW ] && [ "$status" -eq 0 ]; } \
     || { [ "$want" = BLOCK ] && [ "$status" -eq 2 ]; }; then
    ok "$label"
  else
    fail "$label: want=$want status=$status"
  fi
}

echo "Codex-shaped tool payloads use the shared safety guard:"
R=r
expect_guard ALLOW "unified exec routine delete" Bash \
  "$(printf 'rm -%sf ./node_modules' "$R")"
expect_guard BLOCK "unified exec destructive delete" Bash \
  "$(printf 'rm -%sf /' "$R")"
expect_guard BLOCK "worktree branch-sharing override" Bash \
  "git worktree add --force ../shared main"
expect_guard ALLOW "ordinary apply_patch" apply_patch \
  $'*** Begin Patch\n*** Update File: src/index.js\n@@\n-old\n+new\n*** End Patch'
expect_guard BLOCK "apply_patch protected destination" apply_patch \
  $'*** Begin Patch\n*** Add File: config/secrets.json\n+{}\n*** End Patch'
expect_guard BLOCK "apply_patch protected move destination" apply_patch \
  $'*** Begin Patch\n*** Update File: config/example.json\n*** Move to: config/.env.production\n@@\n-{}\n+{}\n*** End Patch'

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
else
  echo "$fails FAILURE(S)"
  exit 1
fi
