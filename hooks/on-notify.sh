#!/usr/bin/env bash
# Hook: Notification (idle_prompt) — reinforce idle state on tab title

INPUT=$(cat)

CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
project="${CWD##*/}"

label_file="/tmp/.tab-label-$(echo "$CWD" | md5sum | cut -c1-8)"
if [[ -f "$label_file" ]]; then
    label=$(< "$label_file")
else
    label=$(git -C "$CWD" symbolic-ref --short HEAD 2>/dev/null)
fi

printf '\e]0;~ %s %s\a' "${label:-$project}" "$project" >&2

exit 0
