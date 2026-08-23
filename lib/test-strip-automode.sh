#!/usr/bin/env bash
# Regression tests for the settings.json clean filter.
#
#   bash lib/test-strip-automode.sh
#
# The filter's job is to make the generated autoMode.environment block invisible
# to git while leaving it in the working file. The property that matters is the
# integration test at the bottom: staging a file whose ONLY change is that block
# must stage nothing at all, so the block cannot reach a commit even when every
# human and hook forgets it.
#
# Note what is deliberately NOT asserted: that `git status` reads clean. Git
# short-circuits its modified check on size, and this filter always shrinks the
# file, so status says "modified" until an add refreshes the cached stat.

set -u
FILTER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/strip-automode.mjs"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
fails=0

ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi }

BARE='{"permissions":{"allow":["Bash(*)"]},"autoMode":{"soft_deny":["$defaults"]}}'
WITHBLOCK='{"permissions":{"allow":["Bash(*)"]},"autoMode":{"soft_deny":["$defaults"],"environment":["**Secrets**: /prod/secrets","**Host**: prod.example.com"]}}'

# 1. the block is removed
out=$(printf '%s' "$WITHBLOCK" | node "$FILTER" | jq -c '.autoMode | keys')
check "strips autoMode.environment" "$out" '["soft_deny"]'

# 2. everything else survives untouched
out=$(printf '%s' "$WITHBLOCK" | node "$FILTER" | jq -c '.permissions.allow')
check "preserves the rest of the file" "$out" '["Bash(*)"]'

# 3. idempotent — filtering twice equals filtering once
one=$(printf '%s' "$WITHBLOCK" | node "$FILTER")
two=$(printf '%s' "$one" | node "$FILTER")
check "idempotent" "$(printf '%s' "$two" | md5sum)" "$(printf '%s' "$one" | md5sum)"

# 4. malformed input passes through rather than breaking the commit
out=$(printf '%s' '{not json at all' | node "$FILTER")
check "invalid JSON passes through" "$out" '{not json at all'

# 5. a file with no block is returned byte-for-byte
out=$(printf '%s' "$BARE" | node "$FILTER")
check "no block: unchanged" "$out" "$BARE"

# ── the integration property: git's view ────────────────────────────────────
cd "$TMP" || exit 1
git init -q .
git config user.email t@t; git config user.name t
git config filter.strip-automode.clean "node $FILTER"
printf 'settings.json filter=strip-automode\n' > .gitattributes
printf '%s\n' "$BARE" | jq . > settings.json
git add -A && git commit -qm init

# add ONLY the generated block to the working file
jq '.autoMode.environment = ["**Host**: prod.example.com"]' settings.json > t && mv t settings.json
check "block-only change shows no diff" "$(git diff --stat settings.json)" ""

# THE property: staging it commits nothing, so the block cannot reach a commit
git add settings.json
git diff --cached --quiet \
  && ok "block-only change stages NOTHING" || bad "block-only change stages NOTHING"
check "and status settles clean after the add" "$(git status --porcelain settings.json)" ""

# now change something real, with the block still present
jq '.permissions.allow += ["Read"]' settings.json > t && mv t settings.json
[ -n "$(git status --porcelain settings.json)" ] \
  && ok "real change reads as MODIFIED" || bad "real change reads as MODIFIED"

# and what actually lands in the commit carries no block
git add settings.json
staged=$(git show :settings.json | jq -c '.autoMode | keys')
check "staged content has no environment key" "$staged" '["soft_deny"]'
staged_allow=$(git show :settings.json | jq -c '.permissions.allow')
check "staged content keeps the real change" "$staged_allow" '["Bash(*)","Read"]'

echo
if [ $fails -eq 0 ]; then echo "all cases pass"; else echo "$fails FAILING case(s)"; exit 1; fi
