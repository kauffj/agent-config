#!/usr/bin/env bash
# Hook: PreToolUse — block dangerous commands and protect sensitive files

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')

case "$TOOL" in
    Bash)
        CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

        # Block destructive patterns
        if echo "$CMD" | grep -qE '(rm\s+-rf\s+/|rm\s+-rf\s+\.|--force(\s|$)\s*push|push\s+--force(\s|$)|reset\s+--hard|DROP\s+(TABLE|DATABASE)|TRUNCATE\s|:\(\)\s*\{|fork\s*\()'; then
            echo "Blocked: destructive command pattern detected" >&2
            exit 2
        fi
        ;;

    Edit|Write)
        FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

        # Protect secrets and critical config
        case "$FILE" in
            *.credentials*|*secrets*|*.env.production|*id_rsa*|*id_ed25519*)
                echo "Protected file: $FILE" >&2
                exit 2
                ;;
        esac
        ;;
esac

exit 0
