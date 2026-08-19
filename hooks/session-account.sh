#!/usr/bin/env bash
# SessionStart: tell the session which Claude account it is running on and
# whether Claude in Chrome actually works here.
#
# Claude in Chrome is account-scoped: the extension is bound to the account its
# browser is signed into, so a session on an account whose browser has no
# extension gets an empty browser list rather than an error. Sessions used to
# discover that by trying, failing, and retrying. This states it up front, with
# the command that fixes it.
set -uo pipefail

CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
ROSTER="$HOME/.claude/meta/accounts.json"
[[ -r "$ROSTER" ]] || exit 0

OUT=$(python3 - "$CFG" <<'PY' 2>/dev/null || true
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/.claude/bin"))
import _claude_acct_lib as L

cfg = os.path.realpath(sys.argv[1])
accounts = L.load_accounts()
mine = next((a for a in accounts
             if os.path.realpath(str(L.config_dir(a))) == cfg), None)
if mine is None:
    raise SystemExit(0)
capable = L.browser_state(mine)   # ready | closed | unlinked | absent
others = [L.display_name(a, accounts) for a in accounts
          if a["name"] != mine["name"] and L.browser_ready(a)]
print(L.display_name(mine, accounts), capable, ",".join(others) or "-")

# A model-scoped weekly cap the picker routed around is invisible from inside
# the session: this account was chosen for the launch model, so switching with
# /model can hit a wall the session has no way to see coming. Cached snapshot
# only — a hook must never block a launch on the network.
now = time.time()
launch = L.launch_model([])
maxed = [c for c in L.scoped_caps(L.cached_usage(mine["name"]), now)
         if c["percent"] >= 95
         and not L.model_matches(c["model"], launch)]
for cap in maxed[:1]:
    when = ""
    if cap["resets_at"] and cap["resets_at"] > now:
        when = ", resets in %s" % L.humanize_mins(int((cap["resets_at"] - now) // 60))
    roomy = []
    for a in accounts:
        if a["name"] == mine["name"]:
            continue
        for other in L.scoped_caps(L.cached_usage(a["name"]), now):
            if other["model"] == cap["model"] and other["percent"] < 90:
                roomy.append("%s is at %.0f%%"
                             % (L.display_name(a, accounts), other["percent"]))
    alt = (" %s — start a %s session with 'claude --model %s' and the picker "
           "routes there." % (" and ".join(roomy), cap["model"], cap["model"].lower())
           ) if roomy else (" No other account has %s headroom either."
                            % cap["model"])
    print("Model limit: this account's %s weekly cap is used up (%.0f%%%s), so "
          "switching this session to %s with /model will fail.%s"
          % (cap["model"], cap["percent"], when, cap["model"], alt))
PY
)

# line 1 is the account/browser state; any further lines are extra advisories
read -r LABEL CAPABLE OTHER <<<"$(printf '%s\n' "$OUT" | head -1)"
EXTRA=$(printf '%s\n' "$OUT" | tail -n +2)

[[ -n "${LABEL:-}" ]] || exit 0

if [[ "$CAPABLE" == "ready" ]]; then
    echo "Claude account: ${LABEL} — Claude in Chrome works in this session."
elif [[ "$CAPABLE" == "unlinked" ]]; then
    echo "Claude account: ${LABEL} — Claude in Chrome is installed in this account's browser but has never been connected to an account, so it registers with none and browser tools return an empty browser list. This needs a human click: open that browser with 'claude --acct-browser ${LABEL}', click the Claude extension in the toolbar, and sign in as ${LABEL}. Retrying the tools before that will not help."
elif [[ "$CAPABLE" == "closed" ]]; then
    echo "Claude account: ${LABEL} — Claude in Chrome is installed for this account but its browser is not running, so browser tools will return an empty browser list until it is. Ask the user to run 'claude --acct-browser ${LABEL}' (opens that browser); retrying the tools before then will not help."
elif [[ "$OTHER" != "-" ]]; then
    echo "Claude account: ${LABEL} — Claude in Chrome is NOT available in this session: the browser extension is bound to a different account (${OTHER}). Browser tools will return an empty browser list; that is expected, not a bug, and retrying will not help. To do browser work, tell the user to start a session with 'claude --chrome' (routes to an account whose browser has the extension), or 'claude --acct-browser ${LABEL}' to set this account's own browser up once."
else
    echo "Claude account: ${LABEL} — Claude in Chrome is NOT available in any account yet (no browser has the extension). Browser tools will return an empty browser list; retrying will not help. Fix once with 'claude --acct-browser ${LABEL}'."
fi

[[ -n "$EXTRA" ]] && echo "$EXTRA"
exit 0
