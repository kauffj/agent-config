#!/usr/bin/env bash
# SessionStart: inject the project's tasks/lessons.md into context so lessons
# are reviewed deterministically instead of relying on the model to remember.
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd? // empty' 2>/dev/null)
PROJECT_DIR=${CLAUDE_PROJECT_DIR:-${CWD:-.}}
PROJECT_ROOT=$(realpath -e -- "$PROJECT_DIR" 2>/dev/null) || exit 0
LESSONS="$PROJECT_ROOT/tasks/lessons.md"
[ -f "$LESSONS" ] && [ ! -L "$LESSONS" ] && [ -s "$LESSONS" ] || exit 0
LESSONS_REAL=$(realpath -e -- "$LESSONS" 2>/dev/null) || exit 0
case "$LESSONS_REAL" in
  "$PROJECT_ROOT"/*) ;;
  *) exit 0 ;;
esac
jq -n --rawfile lessons "$LESSONS_REAL" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: ("Lessons from tasks/lessons.md (review before working):\n" + $lessons)
  }
}'
