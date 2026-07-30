#!/usr/bin/env bash
# SessionStart: inject the project's tasks/lessons.md into context so lessons
# are reviewed deterministically instead of relying on the model to remember.
f="${CLAUDE_PROJECT_DIR:-.}/tasks/lessons.md"
[ -s "$f" ] || exit 0
jq -n --rawfile lessons "$f" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: ("Lessons from tasks/lessons.md (review before working):\n" + $lessons)
  }
}'
