#!/usr/bin/env bash
# Regression tests for the PreToolUse destructive-command guard.
#
#   bash hooks/test-on-pre-tool.sh
#
# The guard's failure mode is not "misses something dangerous" — it is "blocks
# ordinary work", which teaches everyone to route around it. Half of these cases
# are therefore commands that MUST stay allowed, including /workspace's own
# database teardown, which the previous substring guard blocked.
#
# Command strings are assembled at runtime from pieces: this file is itself read
# by Claude through the Bash tool, and a literal 'rm -rf /' in the harness would
# trip the very pattern under test.

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/on-pre-tool.sh"
R=r; D=D; fails=0

check() { # check <ALLOW|BLOCK> <command>
  local want=$1 cmd=$2 got
  printf '{"tool_name":"Bash","tool_input":{"command":%s},"cwd":"%s"}' \
    "$(jq -Rn --arg c "$cmd" '$c')" "$PWD" | "$HOOK" >/dev/null 2>&1
  [ $? -eq 2 ] && got=BLOCK || got=ALLOW
  if [ "$got" = "$want" ]; then
    printf '  ok    %-6s %s\n' "$got" "$cmd"
  else
    printf '  FAIL  want=%s got=%s  %s\n' "$want" "$got" "$cmd"
    fails=$((fails + 1))
  fi
}

echo "--- routine work: must be ALLOWED ---"
check ALLOW "$(printf 'rm -%sf /tmp/claude-scratch/foo' $R)"
check ALLOW "$(printf 'rm -%sf ./node_modules' $R)"
check ALLOW "$(printf 'rm -%sf .workspaces/worktrees/oldfeature' $R)"
check ALLOW "$(printf 'rm -%sf $HOME/projects/scratch' $R)"
check ALLOW "$(printf 'psql "$U" -c "%sROP DATABASE IF EXISTS \\"app_ws_x\\" WITH (FORCE);"' $D)"
check ALLOW "$(printf 'grep -rn "%sROP TABLE" migrations/' $D)"
check ALLOW "$(printf 'sed -i "s/%sROP TABLE/-- &/" old.sql' $D)"
check ALLOW "git push --force-with-lease origin feature/x"
check ALLOW "git worktree remove .workspaces/worktrees/x --force"
check ALLOW "printf '%s\\n' '# docs: --ignore-other-worktrees'"
check ALLOW "printf '%s\\n' 'git reset --hard is forbidden'"
check ALLOW "printf '%s\\n' 'git push --force is forbidden'"
check ALLOW "printf '%s\\n' '# git checkout --ignore-other-worktrees main'"
check ALLOW "ls -la"

echo "--- genuinely destructive: must be BLOCKED ---"
check BLOCK "$(printf 'rm -%sf /' $R)"
check BLOCK "$(printf 'rm -%sf / --no-preserve-root' $R)"
check BLOCK "$(printf 'rm -%sf /*' $R)"
check BLOCK "$(printf 'rm -f -%s ~' $R)"
check BLOCK "$(printf 'rm -%sf .' $R)"
check BLOCK "$(printf 'rm -%sf *' $R)"
check BLOCK "$(printf 'cd /tmp && rm -%sf $HOME/' $R)"
check BLOCK "$(printf 'psql "$U" -c "%sROP DATABASE app_production;"' $D)"
check BLOCK 'psql "$U" -c "TRUNCATE members;"'
check BLOCK "git push --force origin main"
check BLOCK "git push origin main --force"
check BLOCK "git push -f origin main"
check BLOCK "git push origin +HEAD:main"
check BLOCK "git reset --hard origin/main"
check BLOCK "cd /tmp && git reset --hard origin/main"
check BLOCK "if git push --force origin main; then echo bad; fi"

echo "--- worktree branch-sharing overrides: must be BLOCKED ---"
check BLOCK "git checkout --ignore-other-worktrees main"
check BLOCK "git switch --ignore-other-worktrees main"
check BLOCK "git worktree add --force ../wt main"
check BLOCK "git worktree add ../wt main --force"
check BLOCK "git worktree add -f ../wt main"
check ALLOW "git worktree add .workspaces/worktrees/x -b feature/y origin/main"
check ALLOW "git worktree add .workspaces/worktrees/x feature/existing"
check ALLOW "git worktree remove .workspaces/worktrees/x --force"

echo "--- protected files (Edit/Write) ---"
w() { # w <ALLOW|BLOCK> <path>
  local want=$1 path=$2 got
  printf '{"tool_name":"Write","tool_input":{"file_path":"%s"},"cwd":"%s"}' "$path" "$PWD" \
    | "$HOOK" >/dev/null 2>&1
  [ $? -eq 2 ] && got=BLOCK || got=ALLOW
  if [ "$got" = "$want" ]; then
    printf '  ok    %-6s %s\n' "$got" "$path"
  else
    printf '  FAIL  want=%s got=%s  %s\n' "$want" "$got" "$path"
    fails=$((fails + 1))
  fi
}
w BLOCK "/home/u/.credentials.json"
w BLOCK "/home/u/app/.env.production"
w BLOCK "/home/u/.ssh/id_ed25519"
w ALLOW "/home/u/app/.env.local"
w ALLOW "/home/u/app/src/index.ts"

echo
if [ $fails -eq 0 ]; then
  echo "all cases pass"
else
  echo "$fails FAILING case(s)"
  exit 1
fi
