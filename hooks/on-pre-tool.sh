#!/usr/bin/env bash
# Hook: PreToolUse — block dangerous commands and protect sensitive files

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')

case "$TOOL" in
    Bash)
        CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

        # ── Destructive commands, judged by TARGET rather than by substring ──
        # One regex used to cover all of these, and it matched far too much:
        # 'rm -rf /' was a PREFIX, so every absolute path was blocked
        # ('rm -rf /tmp/build'); '.' caught './node_modules' and
        # '.workspaces/...'; and a bare 'DROP DATABASE' blocked both /workspace's
        # own teardown step and any grep for the phrase. A guard that fires on
        # routine work gets worked around, which is worse than no guard — so each
        # check below names the target that actually makes the command dangerous.

        # 1. Recursive delete of a root-ish target, matched as a WHOLE argument.
        #    A named path underneath one ('/tmp/build', './node_modules') is
        #    ordinary work and stays allowed.
        RM_TARGET='(/|/\*|\*|\.|\.\.|\./\*|~|~/|\$HOME|\$HOME/|\$\{HOME\}|\$\{HOME\}/)'
        RM_RECURSIVE='(^|[;&|[:space:]])rm([[:space:]]+-[[:alnum:]-]+)*[[:space:]]+(-[[:alnum:]]*[rR][[:alnum:]]*|--recursive)'
        if echo "$CMD" | grep -qE "(^|[;&|[:space:]])rm([[:space:]]+-[[:alnum:]-]+)*[[:space:]]+${RM_TARGET}([[:space:]]|;|&|\||$)" \
           && echo "$CMD" | grep -qE "$RM_RECURSIVE"; then
            echo "Blocked: recursive delete of a root-level target ('/', '~', '.', '*'). Name the directory you mean." >&2
            exit 2
        fi

        # 2. Destructive SQL — but only when a client is actually EXECUTING it.
        #    Grepping a migration for the phrase is reading, not dropping. A
        #    per-workspace database (…_ws_<name>) is exempt: /workspace creates
        #    it and is expected to drop it again at teardown.
        if echo "$CMD" | grep -qE '(^|[;&|[:space:]])(psql|mysql|mariadb|sqlite3|mongosh|clickhouse-client)([[:space:]]|$)' \
           && echo "$CMD" | grep -qiE '(DROP[[:space:]]+(TABLE|DATABASE|SCHEMA)|TRUNCATE[[:space:]])' \
           && ! echo "$CMD" | grep -q '_ws_'; then
            echo "Blocked: destructive SQL against a database that is not a /workspace clone (…_ws_<name>)." >&2
            exit 2
        fi

        # 3. History rewrites and fork bombs. '--force-with-lease' is deliberately
        #    NOT matched — it is the safe form of the same intent.
        if echo "$CMD" | grep -qE '(git[[:space:]]+push[^;&|]*[[:space:]](--force|-f)([[:space:]]|$)|git[[:space:]]+reset[[:space:]]+--hard|:\(\)[[:space:]]*\{)'; then
            echo "Blocked: force-push, hard reset, or fork bomb" >&2
            exit 2
        fi

        # ── Keep an opted-in repo's MAIN checkout on its protected branch ──
        # Opt a repo in from inside it (no path is hardcoded here):
        #   git config claude.protectMainCheckout true
        #   git config claude.protectedBranch main        # optional, default: main
        # Then branch-switching git commands are blocked in that repo's MAIN
        # working copy — start the work in a linked worktree instead. Inert in
        # every repo that has not opted in, in linked worktrees, and for file
        # restores ('git checkout -- <file>').
        if echo "$CMD" | grep -qE '(^|[;&|[:space:]])git[[:space:]]+(checkout|switch)([[:space:]]|$)'; then
            CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
            TARGET="$CWD"
            LEAD=$(printf '%s' "$CMD" | sed -nE 's/^[[:space:]]*cd[[:space:]]+([^[:space:];&|]+).*/\1/p')
            if [ -n "$LEAD" ]; then
                case "$LEAD" in /*) TARGET="$LEAD";; *) TARGET="$CWD/$LEAD";; esac
            fi
            COMMON=$(cd "$TARGET" 2>/dev/null && git rev-parse --git-common-dir 2>/dev/null)
            COMMON=$(cd "$TARGET" 2>/dev/null && readlink -f "$COMMON" 2>/dev/null)
            GITDIR=$(cd "$TARGET" 2>/dev/null && git rev-parse --absolute-git-dir 2>/dev/null)
            OPTIN=$(git -C "$TARGET" config --bool claude.protectMainCheckout 2>/dev/null)
            KEEP=$(git -C "$TARGET" config claude.protectedBranch 2>/dev/null); KEEP=${KEEP:-main}
            # opted in AND this is the MAIN checkout (not a linked worktree)
            if [ "$OPTIN" = "true" ] && [ -n "$COMMON" ] && [ "$GITDIR" = "$COMMON" ]; then
                BLOCK=0
                # Returning to the protected branch is always allowed; only moving OFF it is blocked.
                if echo "$CMD" | grep -qE 'git[[:space:]]+switch([[:space:]]|$)'; then
                    SW=$(printf '%s' "$CMD" | sed -E 's/.*git[[:space:]]+switch[[:space:]]+//')
                    if ! printf '%s' "$SW" | grep -qE '^(-h|--help)([[:space:]]|$)'; then
                        SWB=$(printf '%s' "$SW" | awk '{print $NF}')   # target branch = last token
                        [ "$SWB" != "$KEEP" ] && BLOCK=1
                    fi
                fi
                if echo "$CMD" | grep -qE 'git[[:space:]]+checkout([[:space:]]|$)'; then
                    if echo "$CMD" | grep -qE 'git[[:space:]]+checkout[[:space:]].*[[:space:]]--([[:space:]]|$)'; then
                        :   # `-- <pathspec>` → file restore, allow
                    elif echo "$CMD" | grep -qE 'git[[:space:]]+checkout[[:space:]]+(-b|-B)([[:space:]]|$)'; then
                        BLOCK=1                                       # creating a branch in the main checkout
                    else
                        ARG=$(printf '%s' "$CMD" | sed -E 's/.*git[[:space:]]+checkout[[:space:]]+//; s/[[:space:]].*//')
                        if [ "$ARG" = "$KEEP" ]; then
                            :                                        # returning to the protected branch is always fine
                        elif [ "$ARG" = "-" ]; then
                            BLOCK=1                                  # previous-branch switch
                        elif printf '%s' "$ARG" | grep -qE '^-'; then
                            :                                        # option-only, leave alone
                        elif git -C "$TARGET" show-ref --verify --quiet "refs/heads/$ARG" 2>/dev/null; then
                            BLOCK=1                                  # existing local branch
                        elif [ ! -e "$TARGET/$ARG" ] && git -C "$TARGET" rev-parse --verify --quiet "${ARG}^{commit}" >/dev/null 2>&1; then
                            BLOCK=1                                  # a ref/commit that isn't a file → detach/switch
                        else
                            :                                        # pathspec → file restore, allow
                        fi
                    fi
                fi
                if [ "$BLOCK" = "1" ]; then
                    TOP=$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null)
                    echo "Blocked: '$CMD' would move the MAIN checkout of $TOP off '$KEEP', which this repo pins (claude.protectMainCheckout). Start the work in an isolated worktree instead, e.g.:  git -C $TOP worktree add -b <branch> .workspaces/worktrees/<name> origin/$KEEP  (then cd into it). File restores like 'git checkout -- <file>' are unaffected." >&2
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
