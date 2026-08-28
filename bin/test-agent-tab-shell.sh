#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TAB_SHELL="$ROOT/bin/agent-tab-shell"
fails=0
if ! fixture=$(mktemp -d) || [[ -z "$fixture" || ! -d "$fixture" ]]; then
    echo "FAIL  could not create test fixture" >&2
    exit 1
fi
trap 'find "$fixture" -depth -delete' EXIT
mkdir "$fixture/.claude"

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

# Two restore commands can race after a freeze. Exactly one may enter the
# native command, and an unlocked leftover lease file must not block a later
# resume after that command exits.
lease_id=lease-test
LEASE_TEST_ROOT="$fixture" HOME="$fixture" \
    "$TAB_SHELL" --session "$lease_id" \
    'touch "$LEASE_TEST_ROOT/entered"; while [[ ! -e "$LEASE_TEST_ROOT/release" ]]; do sleep 0.05; done' \
    >/dev/null 2>&1 &
holder=$!
for _ in $(seq 1 100); do
    [[ -e "$fixture/entered" ]] && break
    sleep 0.02
done
set +e
LEASE_TEST_ROOT="$fixture" HOME="$fixture" \
    "$TAB_SHELL" --session "$lease_id" 'touch "$LEASE_TEST_ROOT/duplicate"' \
    >/dev/null 2>&1
duplicate_status=$?
set -e
touch "$fixture/release"
wait "$holder"
rerun=$(HOME="$fixture" "$TAB_SHELL" --session "$lease_id" 'printf resumed' 2>/dev/null)
if [[ -e "$fixture/entered" && ! -e "$fixture/duplicate" \
      && $duplicate_status -eq 75 && "$rerun" == "resumed" ]]; then
    echo "  ok    live lease rejects duplicates and releases on exit"
else
    echo "  FAIL  live lease lifecycle: duplicate_status=$duplicate_status rerun=$rerun"
    fails=$((fails + 1))
fi

# A native command may legitimately use exit 75. Only an acquisition collision
# gets the helper's "already active" diagnostic.
set +e
child_75_err=$(HOME="$fixture" "$TAB_SHELL" --session child-status-75 \
    'exit 75' 2>&1 >/dev/null)
child_75_status=$?
set -e
if [[ $child_75_status -eq 75 && "$child_75_err" != *"already active"* ]]; then
    echo "  ok    child exit 75 is not mislabeled as a lease collision"
else
    echo "  FAIL  child exit 75 was confused with lease collision"
    fails=$((fails + 1))
fi

# Before foreground handoff, the interactive child waits behind a pipe gate.
# Losing its supervisor closes that pipe: the command must never start, and the
# inherited lease must become acquirable instead of remaining held by a stopped
# background Bash.
if python3 - "$ROOT/bin/_agent_session_lease.py" "$fixture" <<'PY'
import fcntl
import os
import shlex
import subprocess
import sys

helper, root = sys.argv[1:]
marker = os.path.join(root, "startup-gate-command-ran")
lease_path = os.path.join(root, "startup-gate.lock")
lease = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(lease, fcntl.LOCK_EX)
start_fd, release_fd = os.pipe()
child = subprocess.Popen(
    [sys.executable, helper, "--await-foreground", str(start_fd),
     "touch " + shlex.quote(marker)],
    pass_fds=(start_fd, lease), process_group=0)
os.close(start_fd)
os.close(release_fd)
os.close(lease)
try:
    status = child.wait(timeout=2)
except subprocess.TimeoutExpired:
    child.kill()
    child.wait()
    raise
probe = os.open(lease_path, os.O_RDWR)
try:
    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
finally:
    os.close(probe)
if status != 125 or os.path.exists(marker):
    raise SystemExit(1)
PY
then
    echo "  ok    startup gate drops an orphaned child and inherited lease"
else
    echo "  FAIL  startup handoff can strand a child or inherited lease"
    fails=$((fails + 1))
fi

# A real terminal gives the agent child its own foreground process group, then
# returns foreground ownership to the clean login shell. First exit leaves the
# tab at that shell; only the second exit closes it.
pty_out=$(printf 'exit\ntouch "$LEASE_TEST_ROOT/pty-shell-stayed"\nexit\n' \
    | LEASE_TEST_ROOT="$fixture" HOME="$fixture" timeout 10 script -qefc \
      "$TAB_SHELL --session pty-shell-test 'read -r line; [[ \$line == exit ]]'" \
      /dev/null 2>&1)
if [[ -e "$fixture/pty-shell-stayed" \
      && "$pty_out" != *"no job control"* \
      && "$pty_out" != *"cannot set terminal process group"* ]]; then
    echo "  ok    first agent exit returns to a job-control shell"
else
    echo "  FAIL  agent exit closed or stranded the terminal tab"
    fails=$((fails + 1))
fi

# SIGKILL bypasses the supervisor's finally. The native command keeps the
# inherited lease; the outer wrapper waits for that tree, reclaims the TTY, and
# still reaches the post-agent shell. Monitor mode must also stay off so the
# Bash command and its external sleep child share one supervised process group.
mkfifo "$fixture/pty-crash-input"
LEASE_TEST_ROOT="$fixture" HOME="$fixture" timeout 15 script -qefc \
    "$TAB_SHELL --session pty-supervisor-crash-test 'touch \"\$LEASE_TEST_ROOT/pty-crash-ready\"; sleep 1000; true'" \
    /dev/null <"$fixture/pty-crash-input" >"$fixture/pty-crash-output" 2>&1 &
pty_crash_job=$!
exec 8>"$fixture/pty-crash-input"
pty_crash_helper=
pty_crash_bash=
pty_crash_leaf=
for _ in $(seq 1 100); do
    [[ -e "$fixture/pty-crash-ready" ]] || { sleep 0.02; continue; }
    pty_crash_helper=$(ps -eo pid=,args= | awk \
        '$0 ~ /_agent_session_lease.py/ && $0 ~ /pty-supervisor-crash-test/ { print $1; exit }')
    [[ -n "$pty_crash_helper" ]] || { sleep 0.02; continue; }
    pty_crash_bash=$(ps -o pid= --ppid "$pty_crash_helper" | awk 'NF { print $1; exit }')
    [[ -n "$pty_crash_bash" ]] || { sleep 0.02; continue; }
    pty_crash_leaf=$(ps -o pid= --ppid "$pty_crash_bash" | awk 'NF { print $1; exit }')
    [[ -n "$pty_crash_leaf" ]] && break
    sleep 0.02
done
pty_same_group=false
if [[ -n "$pty_crash_bash" && -n "$pty_crash_leaf" ]]; then
    bash_group=$(ps -o pgid= -p "$pty_crash_bash" | tr -d ' ')
    leaf_group=$(ps -o pgid= -p "$pty_crash_leaf" | tr -d ' ')
    [[ -n "$bash_group" && "$bash_group" == "$leaf_group" ]] && pty_same_group=true
fi
[[ -n "$pty_crash_helper" ]] && kill -KILL "$pty_crash_helper"
printf '%s\n' 'touch "$LEASE_TEST_ROOT/pty-crash-recovered"' 'exit' >&8
[[ -n "$pty_crash_leaf" ]] && kill -TERM "$pty_crash_leaf"
exec 8>&-
wait "$pty_crash_job" || true
pty_crash_out=$(<"$fixture/pty-crash-output")
if [[ "$pty_same_group" == true && -e "$fixture/pty-crash-recovered" \
      && "$pty_crash_out" != *"no job control"* \
      && "$pty_crash_out" != *"cannot set terminal process group"* ]]; then
    echo "  ok    supervisor crash preserves one pgrp, lease, and returned shell"
else
    echo "  FAIL  supervisor crash stranded the writer, lease, or returned shell"
    fails=$((fails + 1))
fi

# If the lease supervisor itself is killed, the live foreground process tree
# inherits the locked descriptor and keeps duplicate exclusion intact.
crash_id=supervisor-crash-test
LEASE_TEST_ROOT="$fixture" HOME="$fixture" \
    "$TAB_SHELL" --session "$crash_id" \
    'touch "$LEASE_TEST_ROOT/crash-ready"; while [[ ! -e "$LEASE_TEST_ROOT/crash-release" ]]; do sleep 0.05; done' \
    >/dev/null 2>&1 &
crash_wrapper=$!
crash_helper=
crash_child=
for _ in $(seq 1 100); do
    [[ -e "$fixture/crash-ready" ]] || { sleep 0.02; continue; }
    crash_helper=$(ps -o pid= --ppid "$crash_wrapper" | awk 'NF { print $1; exit }')
    [[ -n "$crash_helper" ]] || { sleep 0.02; continue; }
    crash_child=$(ps -o pid= --ppid "$crash_helper" | awk 'NF { print $1; exit }')
    [[ -n "$crash_child" ]] && break
    sleep 0.02
done
if [[ -n "$crash_helper" && -n "$crash_child" ]]; then
    kill -KILL "$crash_helper"
    wait "$crash_wrapper" 2>/dev/null || true
fi
set +e
flock -n "$fixture/.local/state/agent-fleet/live/$crash_id.lock" -c true
crash_lock_held=$?
set -e
touch "$fixture/crash-release"
for _ in $(seq 1 100); do
    kill -0 "$crash_child" 2>/dev/null || break
    sleep 0.02
done
set +e
flock -n "$fixture/.local/state/agent-fleet/live/$crash_id.lock" -c true
crash_lock_released=$?
set -e
if [[ -n "$crash_child" && $crash_lock_held -ne 0 && $crash_lock_released -eq 0 ]]; then
    echo "  ok    agent tree retains lease across supervisor death"
else
    echo "  FAIL  supervisor death lost or stranded the live lease"
    fails=$((fails + 1))
fi

# A handoff must wait for the old writer, then own the lease before entering
# the replacement command.
handoff_id=11111111-1111-1111-1111-111111111111
bash -c 'exec -a claude python3 -c "import time; time.sleep(0.5)" --resume "$1"' \
    _ "$handoff_id" &
old_writer=$!
sleep 0.05
LEASE_TEST_ROOT="$fixture" HOME="$fixture" \
    "$TAB_SHELL" --session "$handoff_id" --wait-session \
    'touch "$LEASE_TEST_ROOT/handoff-entered"; while [[ ! -e "$LEASE_TEST_ROOT/handoff-release" ]]; do sleep 0.05; done' \
    >/dev/null 2>&1 &
handoff=$!
sleep 0.1
entered_early=false
[[ -e "$fixture/handoff-entered" ]] && entered_early=true
wait "$old_writer"
for _ in $(seq 1 100); do
    [[ -e "$fixture/handoff-entered" ]] && break
    sleep 0.02
done
set +e
flock -n "$fixture/.local/state/agent-fleet/live/$handoff_id.lock" -c true
handoff_lock_status=$?
set -e
touch "$fixture/handoff-release"
wait "$handoff"
if [[ "$entered_early" == false && -e "$fixture/handoff-entered" \
      && $handoff_lock_status -ne 0 ]]; then
    echo "  ok    handoff waits for the old writer then holds the lease"
else
    echo "  FAIL  handoff sequencing: early=$entered_early lock=$handoff_lock_status"
    fails=$((fails + 1))
fi

# A shared liveness probe is momentary. The writer waits through it instead of
# treating the reader as another active session.
probe_race_id=probe-race-test
lease_path="$fixture/.local/state/agent-fleet/live/$probe_race_id.lock"
LEASE_TEST_ROOT="$fixture" flock -s "$lease_path" bash -c \
    'touch "$LEASE_TEST_ROOT/probe-ready"; sleep 0.2' &
reader=$!
for _ in $(seq 1 100); do
    [[ -e "$fixture/probe-ready" ]] && break
    sleep 0.01
done
probe_race=$(HOME="$fixture" "$TAB_SHELL" --session "$probe_race_id" 'printf entered' 2>/dev/null)
wait "$reader"
if [[ "$probe_race" == entered ]]; then
    echo "  ok    writer waits through a concurrent liveness probe"
else
    echo "  FAIL  liveness probe blocked a legitimate writer"
    fails=$((fails + 1))
fi

set +e
HOME="$fixture" "$TAB_SHELL" --session 'bad;id' true >/dev/null 2>&1
unsafe_status=$?
HOME="$fixture" "$TAB_SHELL" --wait-session true >/dev/null 2>&1
missing_session_status=$?
set -e
if [[ $unsafe_status -eq 2 && $missing_session_status -eq 2 ]]; then
    echo "  ok    unsafe ids and wait-without-session are rejected"
else
    echo "  FAIL  invalid arguments: session=$unsafe_status missing-session=$missing_session_status"
    fails=$((fails + 1))
fi

symlink_home="$fixture/symlink-home"
outside="$fixture/outside-live"
mkdir -p "$symlink_home/.local/state/agent-fleet" "$outside"
ln -s "$outside" "$symlink_home/.local/state/agent-fleet/live"
set +e
HOME="$symlink_home" "$TAB_SHELL" --session symlink-test true >/dev/null 2>&1
symlink_status=$?
set -e
if [[ $symlink_status -ne 0 && ! -e "$outside/symlink-test.lock" ]]; then
    echo "  ok    lease directory symlinks are rejected without touching targets"
else
    echo "  FAIL  lease directory symlink crossed the runtime boundary"
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

if grep -q 'tab_shell, "--session", sid, cmd' "$ROOT/bin/claude-resume" \
   && grep -q 'tab_shell, "--session", sid, command' "$ROOT/bin/claude-schedule" \
   && grep -q "claude-schedule', 'reopen'" "$ROOT/wezterm/wezterm.lua" \
   && grep -q '"--session", sid, "--wait-session", resume' "$ROOT/bin/claude-model"; then
    echo "  ok    every known-session launch carries its live lease id"
else
    echo "  FAIL  a known-session launch bypasses the live lease"
    fails=$((fails + 1))
fi

if grep -q "resume_cmd or ('claude --resume '" "$ROOT/wezterm/wezterm.lua"; then
    echo "  FAIL  picker has an unvalidated resume-command fallback"
    fails=$((fails + 1))
else
    echo "  ok    picker refuses sessions without a validated resume command"
fi

if grep -q 'if not rec.scheduled then' "$ROOT/wezterm/wezterm.lua" \
   && grep -q 'shlex.quote(model)' "$ROOT/bin/claude-model"; then
    echo "  ok    live external sessions refuse resume and model args are quoted"
else
    echo "  FAIL  duplicate-writer picker guard or model quoting is missing"
    fails=$((fails + 1))
fi

if grep -q "claude-schedule', 'reopen'" "$ROOT/wezterm/wezterm.lua" \
   && ! grep -q "claude-schedule', 'cancel'" "$ROOT/wezterm/wezterm.lua" \
   && ! grep -q 'pane_by_id\|cwd_by_id\|cmd_by_id\|scheduled_ids' \
        "$ROOT/wezterm/wezterm.lua"; then
    echo "  ok    snooze removal follows confirmed native startup"
else
    echo "  FAIL  picker can remove snooze state before native startup"
    fails=$((fails + 1))
fi

if grep -q 'SetUserVar=agent_session' "$ROOT/bin/_agent_session_lease.py" \
   && grep -q 'SetUserVar=agent_session' "$ROOT/hooks/session-state.sh" \
   && grep -q 'vars.agent_session' "$ROOT/wezterm/wezterm.lua" \
   && grep -q 'LIVE_SESSION_CACHE' "$ROOT/bin/claude-resume"; then
    echo "  ok    pane-scoped identity reaches sandboxed recovery end to end"
else
    echo "  FAIL  pane-scoped identity wiring is incomplete"
    fails=$((fails + 1))
fi

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
if [[ $fails -gt 0 ]]; then
    echo "$fails FAILING case(s)"
    exit 1
fi
echo "all cases pass"
