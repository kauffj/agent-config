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

        # ── Keep the demo-app MAIN checkout on 'main' ────────────────────────
        # Branch-switching git commands (checkout/switch to a branch, -b/-B)
        # must not run in the main working copy (/home/you/projects/demo-app):
        # start work in a worktree instead. Self-scoped to the demo-app repo's
        # MAIN checkout — inert in every other repo, in linked worktrees, and
        # for file-restore checkouts ('git checkout -- <file>').
        if echo "$CMD" | grep -qE '(^|[;&|[:space:]])git[[:space:]]+(checkout|switch)([[:space:]]|$)'; then
            CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
            TARGET="$CWD"
            LEAD=$(printf '%s' "$CMD" | sed -nE 's/^[[:space:]]*cd[[:space:]]+([^[:space:];&|]+).*/\1/p')
            if [ -n "$LEAD" ]; then
                case "$LEAD" in /*) TARGET="$LEAD";; *) TARGET="$CWD/$LEAD";; esac
            fi
            MAIN_GIT=$(readlink -f /home/you/projects/demo-app/.git 2>/dev/null)
            COMMON=$(cd "$TARGET" 2>/dev/null && git rev-parse --git-common-dir 2>/dev/null)
            COMMON=$(cd "$TARGET" 2>/dev/null && readlink -f "$COMMON" 2>/dev/null)
            GITDIR=$(cd "$TARGET" 2>/dev/null && git rev-parse --absolute-git-dir 2>/dev/null)
            # demo-app repo (COMMON==MAIN_GIT) AND its main checkout (GITDIR==COMMON)
            if [ -n "$COMMON" ] && [ "$COMMON" = "$MAIN_GIT" ] && [ "$GITDIR" = "$COMMON" ]; then
                BLOCK=0
                # Returning to 'main' is always allowed; only moving OFF main is blocked.
                if echo "$CMD" | grep -qE 'git[[:space:]]+switch([[:space:]]|$)'; then
                    SW=$(printf '%s' "$CMD" | sed -E 's/.*git[[:space:]]+switch[[:space:]]+//')
                    if ! printf '%s' "$SW" | grep -qE '^(-h|--help)([[:space:]]|$)'; then
                        SWB=$(printf '%s' "$SW" | awk '{print $NF}')   # target branch = last token
                        [ "$SWB" != "main" ] && BLOCK=1
                    fi
                fi
                if echo "$CMD" | grep -qE 'git[[:space:]]+checkout([[:space:]]|$)'; then
                    if echo "$CMD" | grep -qE 'git[[:space:]]+checkout[[:space:]].*[[:space:]]--([[:space:]]|$)'; then
                        :   # `-- <pathspec>` → file restore, allow
                    elif echo "$CMD" | grep -qE 'git[[:space:]]+checkout[[:space:]]+(-b|-B)([[:space:]]|$)'; then
                        BLOCK=1                                       # creating a branch in the main checkout
                    else
                        ARG=$(printf '%s' "$CMD" | sed -E 's/.*git[[:space:]]+checkout[[:space:]]+//; s/[[:space:]].*//')
                        if [ "$ARG" = "main" ]; then
                            :                                        # returning to main is always fine
                        elif [ "$ARG" = "-" ]; then
                            BLOCK=1                                  # previous-branch switch
                        elif printf '%s' "$ARG" | grep -qE '^-'; then
                            :                                        # option-only, leave alone
                        elif git -C "$TARGET" show-ref --verify --quiet "refs/heads/$ARG" 2>/dev/null; then
                            BLOCK=1                                  # existing local branch (incl. feat/foo)
                        elif [ ! -e "$TARGET/$ARG" ] && git -C "$TARGET" rev-parse --verify --quiet "${ARG}^{commit}" >/dev/null 2>&1; then
                            BLOCK=1                                  # a ref/commit that isn't a file → detach/switch
                        else
                            :                                        # pathspec → file restore, allow
                        fi
                    fi
                fi
                if [ "$BLOCK" = "1" ]; then
                    echo "Blocked: '$CMD' would change the branch of the MAIN demo-app checkout (/home/you/projects/demo-app), which must stay on 'main'. Start the work in an isolated worktree instead, e.g.:  git -C /home/you/projects/demo-app worktree add -b <branch> ../demo-app-<name> origin/main  (then cd into it). File restores like 'git checkout -- <file>' are unaffected." >&2
                    exit 2
                fi
            fi
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
