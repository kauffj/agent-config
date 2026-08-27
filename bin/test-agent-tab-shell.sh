#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TAB_SHELL="$ROOT/bin/agent-tab-shell"
fails=0

probe='for name in BROWSER CLAUDE_CONFIG_DIR CLAUDE_ACCT_BROWSER_PROFILE CLAUDECODE CLAUDE_CODE_MESSAGING_SOCKET CODEX_SESSION_ID CODEX_HOME CODEX_SQLITE_HOME CODEX_API_KEY CODEX_ACCESS_TOKEN OPENAI_FEDERATION_RULE_ID OPENAI_IDENTITY_TOKEN_FILE OPENAI_WORKLOAD_IDENTITY_CONTEXT; do if [[ -v $name ]]; then printf "leaked=%s\n" "$name"; fi; done; printf "safe=%s\n" "${SAFE_MARKER-unset}"'
out=$(env \
    BROWSER=/home/test/bin/claude-acct-browser \
    CLAUDE_CONFIG_DIR=/home/test/.claude-alt \
    CLAUDE_ACCT_BROWSER_PROFILE=/home/test/.browser-alt \
    CLAUDECODE=1 \
    CLAUDE_CODE_MESSAGING_SOCKET=/tmp/stale.sock \
    CODEX_SESSION_ID=stale-codex \
    CODEX_HOME=/home/test/.codex-alt \
    CODEX_SQLITE_HOME=/home/test/.codex-state \
    CODEX_API_KEY=stale-api-key \
    CODEX_ACCESS_TOKEN=stale-access-token \
    OPENAI_FEDERATION_RULE_ID=stale-federation \
    OPENAI_IDENTITY_TOKEN_FILE=/tmp/stale-oidc-token \
    OPENAI_WORKLOAD_IDENTITY_CONTEXT='{"workload":"stale"}' \
    SAFE_MARKER=kept \
    "$TAB_SHELL" "$probe" 2>/dev/null)
if [[ "$out" == "safe=kept" ]]; then
    echo "  ok    contaminated parent identity is scrubbed"
else
    echo "  FAIL  leaked identity: $out"
    fails=$((fails + 1))
fi

set +e
"$TAB_SHELL" 'exit 23' >/dev/null 2>&1
status=$?
set -e
if [[ $status -eq 23 ]]; then
    echo "  ok    non-interactive command status is preserved"
else
    echo "  FAIL  expected status 23, got $status"
    fails=$((fails + 1))
fi

for source in bin/claude-resume bin/claude-model bin/claude-schedule wezterm/wezterm.lua; do
    if grep -q 'agent-tab-shell' "$ROOT/$source"; then
        printf '  ok    %-24s uses clean tab boundary\n' "$source"
    else
        printf '  FAIL  %-24s bypasses clean tab boundary\n' "$source"
        fails=$((fails + 1))
    fi
done

if grep -q '"resume_command": resume_command' "$ROOT/bin/claude-sessions" \
   && grep -q 'resume_command(sid, e.get("vendor")' "$ROOT/bin/claude-schedule"; then
    echo "  ok    picker and snooze retain native vendor resume commands"
else
    echo "  FAIL  native vendor resume command is not carried end to end"
    fails=$((fails + 1))
fi

if grep -q "resume_cmd or ('claude --resume '" "$ROOT/wezterm/wezterm.lua"; then
    echo "  FAIL  picker has an unvalidated resume-command fallback"
    fails=$((fails + 1))
else
    echo "  ok    picker refuses sessions without a validated resume command"
fi

if grep -q 'if activated and rec.scheduled then' "$ROOT/wezterm/wezterm.lua" \
   && ! grep -q 'pane_by_id\|cwd_by_id\|cmd_by_id\|scheduled_ids' \
        "$ROOT/wezterm/wezterm.lua"; then
    echo "  ok    snooze cancellation follows successful record activation"
else
    echo "  FAIL  picker can cancel snooze state independently of activation"
    fails=$((fails + 1))
fi

fixture=$(mktemp -d)
mkdir "$fixture/.claude"
HOME="$fixture" "$ROOT/bin/claude-schedule" add \
    --sid 01codex --vendor codex --cwd /tmp --when 1h --label test >/dev/null
scheduled=$(HOME="$fixture" "$ROOT/bin/claude-schedule" list --json)
if jq -e '.[0].vendor == "codex"
          and .[0].resume_command == "codex resume 01codex"' \
        >/dev/null <<<"$scheduled"; then
    echo "  ok    snooze state round-trips the Codex resume command"
else
    echo "  FAIL  snooze state lost the Codex resume command"
    fails=$((fails + 1))
fi
find "$fixture" -depth -delete

if [[ $fails -gt 0 ]]; then
    echo "$fails FAILING case(s)"
    exit 1
fi
echo "all cases pass"
