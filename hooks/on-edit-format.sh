#!/usr/bin/env bash
# Hook: PostToolUse (Edit|Write) — auto-format edited files
# Runs the project's formatter on the file Claude just edited.

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[[ -z "$FILE" || ! -f "$FILE" ]] && exit 0

DIR=$(dirname "$FILE")

# Find project root (nearest package.json or git root)
PROJECT_ROOT=""
d="$DIR"
while [[ "$d" != "/" ]]; do
    if [[ -f "$d/package.json" ]]; then
        PROJECT_ROOT="$d"
        break
    fi
    d=$(dirname "$d")
done
[[ -z "$PROJECT_ROOT" ]] && exit 0

# Only format file types that benefit from it
case "$FILE" in
    *.ts|*.tsx|*.js|*.jsx|*.css|*.scss|*.json|*.md|*.html|*.vue|*.svelte) ;;
    *.py)
        command -v black &>/dev/null && black -q "$FILE" 2>/dev/null
        exit 0
        ;;
    *.go)
        command -v gofmt &>/dev/null && gofmt -w "$FILE" 2>/dev/null
        exit 0
        ;;
    *.rs)
        command -v rustfmt &>/dev/null && rustfmt "$FILE" 2>/dev/null
        exit 0
        ;;
    *) exit 0 ;;
esac

# Use project's prettier if available
if [[ -f "$PROJECT_ROOT/node_modules/.bin/prettier" ]]; then
    "$PROJECT_ROOT/node_modules/.bin/prettier" --write "$FILE" 2>/dev/null
elif command -v prettier &>/dev/null; then
    prettier --write "$FILE" 2>/dev/null
fi

exit 0
