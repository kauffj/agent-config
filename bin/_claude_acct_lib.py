#!/usr/bin/env python3
"""claude-acct — launch claude under whichever Max account has the most headroom.

Every launch first probes all configured accounts (~60s cache), prints a
one-line enumeration of them, and only then execs claude. Two working accounts
are required: with fewer, or with any configured account whose credentials are
broken, the wrapper does NOT launch a session — it collects the missing
credentials (`claude auth login` inside that account's config dir) and, if that
can't be done here, exits with the exact command to run.

Policy: balance against each window's next reset, not against raw usage. A
percentage only means something relative to when it refills — 60% spent with
twenty minutes to reset is nearly free, 60% spent with four hours to go is
scarce, and the account about to refill is the one worth burning. So every
limit (5-hour, weekly, model-scoped weekly) gets a slack: capacity left over
the capacity its window would hand back in the time remaining. 1.0 is exactly
on pace, below 1 is burning faster than it refills. Score sums session and
weekly pressure (1/slack), lowest wins — the scarcer window dominates on its
own, while the other still breaks ties between accounts whose pace matches.
Accounts at/above 99% are skipped, and one too spent to carry a session now is
passed over even when an imminent reset makes it look cheap. Near-ties go to
the account launched least recently.

Usage comes from the endpoint /usage itself renders
(api.anthropic.com/api/oauth/usage), cached under an flock so a burst of
launches (claude-resume reopening five tabs) does one fetch, with a small
per-launch score bump so bursts alternate accounts. Tokens are never refreshed
here — refresh-token rotation would desync what claude has stored. An account
whose access token has expired is still healthy (claude refreshes it at
launch); it's scored from its last-good snapshot with reset-aware decay. Only a
dead refresh token or a 401/403 counts as broken — a network failure never does.

Each account logs in through its OWN browser session (~/.claude-browsers/NAME,
handed to `claude auth login` as $BROWSER via bin/claude-acct-browser). The
default profile is already signed into one subscription, so a login there would
silently re-authorize that same account — two config dirs, one quota pool. The
per-account profile persists signed in as its own account, which is also what
Claude in Chrome needs to attach to that account (its NativeMessagingHosts/ is
symlinked in at setup). A login that lands on an email another account already
uses is rolled back, not kept.

Wrapper-owned arguments (never forwarded to claude):
    --acct NAME        force an account (env: CLAUDE_ACCT=NAME)
    --acct-status      show every account's credentials + usage, and the pick
    --acct-login [N]   collect credentials (all unhealthy accounts, or one)
    --acct-browser [N] open an account's browser session (claude.ai, extension)
    --acct-setup NAME  create ~/.claude-NAME (symlinks + seeded .claude.json)

Maintenance subcommands (auth, mcp, update, ...) run on the default account and
skip the health gate — they're how a broken account gets fixed. A preexisting
CLAUDE_CONFIG_DIR (daemon respawns, in-session children) bypasses everything.
`--acct NAME` on a working account also overrides the gate, so a wrapper bug
can never lock the user out of claude; `command claude` bypasses the wrapper.

Roster: ~/.claude/meta/accounts.json
    [{"name": "main", "configDir": null, "email": "..."},
     {"name": "alt",  "configDir": "~/.claude-alt"}]
configDir null = the default account (~/.claude + ~/.claude.json, no env var).
"""

# urllib.request (97ms), concurrent.futures (32ms) and subprocess (19ms) are
# imported where they are used, not here: they cost more than everything else
# this wrapper does, and a launch that hits the usage cache needs none of them.
# That import time is paid on every single `claude`, including each tab of a
# claude-resume burst.
import fcntl
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
REAL_CLAUDE = Path.home() / ".local" / "bin" / "claude"
ACCOUNTS_PATH = CLAUDE_DIR / "meta" / "accounts.json"
STATE_DIR = CLAUDE_DIR / "state"
CACHE_PATH = STATE_DIR / "acct-usage.json"
LEDGER_PATH = STATE_DIR / "acct-ledger.jsonl"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

MIN_ACCOUNTS = 2         # working accounts required before a session may start
CACHE_TTL = 60           # seconds a fetched snapshot counts as fresh
FAIL_TTL = 60            # seconds to stop retrying a network failure
LOGIN_WAIT = 300         # seconds to wait for a login running in another tab
FETCH_TIMEOUT = 3.0
BLOCKED_AT = 99.0        # an active limit at/above this = account unusable
STALL_GUARD = 90.0       # session % above which a launch would stall out
SESSION_WINDOW_H = 5.0   # nominal length of the session window
WEEKLY_WINDOW_H = 168.0  # nominal length of the weekly window
RESET_FLOOR_H = 0.05     # never divide by an almost-zero time-to-reset
SLACK_FLOOR = 0.01       # ... nor by almost-zero slack
WEEKLY_WEIGHT = 1.0      # weekly pressure relative to session pressure
HERD_BUMP = 0.2          # score bump per launch within the cache window
TIE_EPSILON = 0.05       # score gap treated as a tie -> least recently used

# Credential states. Healthy means claude can authenticate with it right now;
# "idle" (access token expired, refresh token alive) and "offline" (we simply
# couldn't reach the API) are healthy — neither says the credentials are bad.
HEALTHY = ("ok", "idle", "offline")
BROKEN = ("auth", "expired")      # 401/403, or the refresh token itself died
ABSENT = ("missing",)             # configured but never logged in

MAINTENANCE = {
    "agents", "auth", "auto-mode", "config", "doctor", "gateway", "import",
    "install", "mcp", "migrate-installer", "plugin", "plugins", "project",
    "setup-token", "update", "upgrade",
}


# ---------------------------------------------------------------- accounts

def load_accounts():
    try:
        raw = json.loads(ACCOUNTS_PATH.read_text())
    except (FileNotFoundError, ValueError):
        raw = [{"name": "main", "configDir": None}]
    return [{
        "name": a["name"],
        "dir": Path(os.path.expanduser(a["configDir"])) if a.get("configDir") else None,
        "email": a.get("email"),
        "browser": bool(a.get("browser")),
    } for a in raw]


def browser_capable(accounts):
    """Accounts that can actually drive a browser. Claude in Chrome is
    account-scoped — verified 2026-08-18: a session on the second account saw
    zero connected browsers while the first saw them all — so a browser session
    has to run on an account whose own browser carries the extension.
    Capability is detected from disk (the extension installed in that account's
    profile), with "browser" in accounts.json as a manual override. Returns
    every capable account, so browser work still balances once both qualify."""
    capable = [a for a in accounts
               if a["browser"] or (has_extension(a) and extension_activated(a))]
    live = [a for a in capable if browser_running(a)]
    return live or capable or [default_account(accounts)]


def wants_browser(args, flagged=False):
    return flagged or "--chrome" in args


def config_dir(acct):
    return acct["dir"] or CLAUDE_DIR


def default_account(accounts):
    for a in accounts:
        if a["dir"] is None:
            return a
    return accounts[0]


def email_of(acct):
    return acct.get("email") or account_email(acct)


def display_name(acct, accounts=None):
    """Accounts are identified by the subscription they log into. The local
    part of the email is enough unless two accounts share one."""
    email = email_of(acct)
    if not email:
        return acct["name"]
    local = email.split("@", 1)[0]
    if accounts:
        locals_ = [(email_of(a) or "").split("@", 1)[0] for a in accounts]
        if locals_.count(local) > 1:
            return email
    return local


def find_account(accounts, key):
    """Match on email, its local part, or the internal handle — any prefix
    that is unambiguous. `--acct you` and `--acct you@example.com` both
    work; so does the legacy `--acct alt`."""
    k = (key or "").strip().lower()
    for a in accounts:                                   # exact handle
        if a["name"].lower() == k:
            return a
    for a in accounts:                                   # exact email or local
        email = (email_of(a) or "").lower()
        if email and k in (email, email.split("@", 1)[0]):
            return a
    hits = [a for a in accounts
            if a["name"].lower().startswith(k)
            or (email_of(a) or "").lower().startswith(k)]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit("claude-acct: %s account %r — roster: %s"
                     % ("ambiguous" if hits else "unknown", key,
                        ", ".join(email_of(a) or a["name"] for a in accounts)))


def next_account_name(existing):
    """Name for a roster slot we're about to create."""
    if "alt" not in existing:
        return "alt"
    n = 3
    while ("acct%d" % n) in existing:
        n += 1
    return "acct%d" % n


def ensure_roster(accounts, minimum=MIN_ACCOUNTS):
    """Roster entries for accounts that don't exist yet, so they show up as
    'missing' and get collected rather than silently ignored."""
    if len(accounts) >= minimum:
        return accounts
    names = {a["name"] for a in accounts}
    while len(accounts) < minimum:
        name = next_account_name(names)
        names.add(name)
        path = Path.home() / (".claude-" + name)
        register_account(name, path)
        accounts.append({"name": name, "dir": path, "email": None})
    return accounts


def account_email(acct):
    try:
        return json.loads((config_dir(acct) / ".claude.json").read_text()) \
            .get("oauthAccount", {}).get("emailAddress")
    except Exception:
        return None


# ------------------------------------------------------------- credentials

def read_oauth(acct):
    """The claudeAiOauth block, or None if this account isn't logged in.
    Tolerates one mid-refresh partial write by claude itself."""
    path = config_dir(acct) / ".credentials.json"
    for attempt in (0, 1):
        try:
            return json.loads(path.read_text()).get("claudeAiOauth")
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            if attempt == 0:
                time.sleep(0.1)
    return None


def token_usable(oauth, now):
    """Access token good enough to call the usage API ourselves."""
    if not oauth or not oauth.get("accessToken"):
        return False
    exp = oauth.get("expiresAt")  # epoch milliseconds
    return not exp or exp / 1000.0 > now + 60


def refresh_alive(oauth, now):
    """Refresh token still valid — i.e. claude can re-auth without a login."""
    exp = (oauth or {}).get("refreshTokenExpiresAt")
    return not exp or exp / 1000.0 > now


# ------------------------------------------------------------- usage fetch

def cli_version():
    try:
        return os.path.basename(os.readlink(REAL_CLAUDE))
    except OSError:
        return "2.0.0"


def fetch_usage(token):
    """-> (usage, error) with error None | 'auth' (credentials rejected) |
    'network' (couldn't ask — says nothing about the credentials)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-cli/%s (external, cli)" % cli_version(),
    })
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, "auth" if e.code in (401, 403) else "network"
    except Exception:
        return None, "network"


def refresh_cache(accounts, cache, now, fetcher=fetch_usage, force=False):
    """Probe every account, recording credential state and (when reachable) a
    fresh usage snapshot. Mutates cache: {name: {fetched_at, usage, bump, probe}}."""
    stale = []
    for acct in accounts:
        entry = cache.setdefault(acct["name"], {})
        oauth = read_oauth(acct)
        if not oauth or not oauth.get("accessToken"):
            entry["probe"] = "missing"
            entry.pop("usage", None)
            continue
        if not refresh_alive(oauth, now):
            entry["probe"] = "expired"
            continue
        if not force and entry.get("usage") and \
                entry.get("fetched_at", 0) > now - CACHE_TTL:
            entry.setdefault("probe", "ok")
            continue
        if not force and entry.get("failed_at", 0) > now - FAIL_TTL:
            # The API just refused to answer. Retrying it on every launch costs
            # FETCH_TIMEOUT each time, and probe_all holds the cache lock while
            # it waits — so an offline laptop would serialize a claude-resume
            # burst behind one dead connection per tab. Score from the last
            # good snapshot instead and try again after the cooldown.
            entry.setdefault("probe", "offline")
            continue
        if not token_usable(oauth, now):
            entry["probe"] = "idle"   # claude refreshes it at launch
            continue
        stale.append((acct["name"], oauth["accessToken"]))
    if not stale:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(stale)) as pool:
        results = list(pool.map(lambda nt: (nt[0], fetcher(nt[1])), stale))
    for name, (usage, err) in results:
        entry = cache.setdefault(name, {})
        if usage is not None:
            entry.update({"fetched_at": now, "usage": usage, "bump": 0.0,
                          "probe": "ok"})
            entry.pop("failed_at", None)
        elif err == "auth":
            entry["probe"] = "auth"      # a refusal is fast; never cool it down
        else:
            entry["probe"] = "offline"
            entry["failed_at"] = now


# ------------------------------------------------------------------ policy

def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def scope_model(scope):
    """Display name of the model a limit is scoped to ('' = unscoped)."""
    m = (scope or {}).get("model") or {}
    return m.get("display_name") or m.get("id") or ""


def model_matches(name, launch_model):
    """Does a cap scoped to `name` constrain a launch of `launch_model`?
    Unknown either way means yes — a cap we can't rule out still counts."""
    if not name or not launch_model:
        return True
    name, lm = name.lower(), launch_model.lower()
    return name in lm or lm in name


def decayed_percent(lim, now):
    """A limit's utilization, as 0 once its window has rolled over since the
    snapshot. One rule, so scoring and reporting can never disagree."""
    resets = parse_iso(lim.get("resets_at"))
    pct = float(lim.get("percent") or 0)
    return (0.0 if resets is not None and now >= resets else pct), resets


def applicable_limits(usage, launch_model, now):
    """Limits with reset-aware decay, filtered to ones constraining THIS
    launch (a weekly_scoped cap for a model we aren't launching is ignored)."""
    out = []
    for lim in (usage or {}).get("limits") or []:
        if lim.get("kind") == "weekly_scoped" and \
                not model_matches(scope_model(lim.get("scope")), launch_model):
            continue
        pct, resets = decayed_percent(lim, now)
        out.append({"kind": lim.get("kind"), "percent": pct, "resets_at": resets})
    if not out:  # fresh account / older response shape: synthesize from totals
        for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
            block = (usage or {}).get(key)
            if block and block.get("utilization") is not None:
                pct, resets = decayed_percent(
                    {"percent": block["utilization"],
                     "resets_at": block.get("resets_at")}, now)
                out.append({"kind": kind, "percent": pct, "resets_at": resets})
    return out


def scoped_caps(usage, now):
    """Every model-scoped weekly cap in a snapshot, whichever model a launch
    would use. applicable_limits() hides the ones that don't constrain this
    launch — correct for scoring, wrong for telling a human what is left."""
    out = []
    for lim in (usage or {}).get("limits") or []:
        name = scope_model(lim.get("scope"))
        if lim.get("kind") != "weekly_scoped" or not name:
            continue
        pct, resets = decayed_percent(lim, now)
        out.append({"model": name, "percent": pct, "resets_at": resets})
    return sorted(out, key=lambda c: -c["percent"])


def cached_usage(name):
    """One account's last usage snapshot, read without taking the lock. For
    read-only callers (the SessionStart hook) that must never block a launch.
    A concurrent probe rewrites this file in place, so tolerate catching it
    mid-write the same way read_oauth() does — one retry, then give up."""
    for attempt in (0, 1):
        try:
            return (json.loads(CACHE_PATH.read_text()).get(name) or {}).get("usage")
        except OSError:
            return None
        except ValueError:
            if attempt == 0:
                time.sleep(0.05)
    return None


def session_weekly(limits):
    session = max((l["percent"] for l in limits if l["kind"] == "session"),
                  default=0.0)
    weekly = max((l["percent"] for l in limits if l["kind"] != "session"),
                 default=0.0)
    return session, weekly


def window_hours(kind):
    return SESSION_WINDOW_H if kind == "session" else WEEKLY_WINDOW_H


def limit_slack(lim, now):
    """How far ahead of its own refill pace this limit is.

    1.0 means capacity left is exactly proportional to time left; above 1 is
    ahead, below 1 is burning faster than the window refills. This is what
    makes a percentage mean anything: 60% spent with 20 minutes to reset is
    nearly free, 60% spent with four hours to go is scarce. A missing
    resets_at falls back to a full window, which degrades to plain usage."""
    w = window_hours(lim["kind"])
    remaining = max(0.0, 100.0 - lim["percent"])
    resets = lim.get("resets_at")
    hours = w if not resets else (resets - now) / 3600.0
    hours = min(max(hours, RESET_FLOOR_H), w)
    return (remaining * w) / (100.0 * hours)


def pressure(lim, now):
    """Cost of leaning on this limit — grows without bound as slack runs out."""
    return 1.0 / max(limit_slack(lim, now), SLACK_FLOOR)


def score(limits, now):
    """Lower is better.

    Both windows always contribute, rather than only the worst one: when two
    accounts have identical weekly pace, a nearly-spent 5-hour window still
    decides between them. The scarcer window dominates on its own, because
    pressure diverges as slack approaches zero."""
    session = max((pressure(l, now) for l in limits if l["kind"] == "session"),
                  default=1.0)
    weekly = max((pressure(l, now) for l in limits if l["kind"] != "session"),
                 default=1.0)
    return session + WEEKLY_WEIGHT * weekly


def blocked_until(limits, now):
    """Epoch when the account frees up, or None if usable now."""
    worst = None
    for lim in limits:
        if lim["percent"] >= BLOCKED_AT:
            resets = lim["resets_at"] or now + 5 * 3600  # unknown: assume 5h
            worst = max(worst or 0.0, resets)
    return worst


def assess(entry, launch_model, now):
    """Score one account from its cached snapshot. None = no data at all."""
    usage = (entry or {}).get("usage")
    if not usage:
        return None
    limits = applicable_limits(usage, launch_model, now)
    session, weekly = session_weekly(limits)
    return {
        "session": session,
        "weekly": weekly,
        "pace": min((limit_slack(l, now) for l in limits), default=1.0),
        "session_resets": max((l["resets_at"] for l in limits
                               if l["kind"] == "session" and l["resets_at"]),
                              default=None),
        "weekly_resets": max((l["resets_at"] for l in limits
                              if l["kind"] != "session" and l["resets_at"]),
                             default=None),
        "stall": session >= STALL_GUARD,
        "score": score(limits, now) + float((entry or {}).get("bump") or 0.0),
        "blocked_until": blocked_until(limits, now),
        "age": now - (entry or {}).get("fetched_at", now),
    }


def pick(candidates, last_launch, now):
    """candidates: [(name, assessment-or-None)]. Winning name, or None."""
    usable = [(n, a) for n, a in candidates
              if a and (a["blocked_until"] is None or a["blocked_until"] <= now)]
    # An account can score well purely because its window resets in minutes,
    # while still being too spent to get through the next few of them. Prefer
    # accounts that can actually carry a session; fall back only if none can.
    steady = [na for na in usable if not na[1].get("stall")]
    pool = steady or usable
    if pool:
        best = min(pool, key=lambda na: na[1]["score"])
        ties = [na for na in pool
                if na[1]["score"] - best[1]["score"] <= TIE_EPSILON]
        return min(ties, key=lambda na: last_launch.get(na[0], 0))[0]
    scored = [(n, a) for n, a in candidates if a]
    if scored:  # everything is blocked: soonest to free up
        return min(scored, key=lambda na: na[1]["blocked_until"] or 0)[0]
    names = [n for n, _ in candidates]
    return min(names, key=lambda n: last_launch.get(n, 0)) if names else None


def launch_model(args):
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    try:
        return json.loads((CLAUDE_DIR / "settings.json").read_text()).get("model")
    except Exception:
        return None


def extract_acct_flag(args):
    """Strip the wrapper's own flags -> (claude args, forced account, --browser)."""
    out, forced, browser, i = [], None, False, 0
    while i < len(args):
        a = args[i]
        if a == "--acct" and i + 1 < len(args):
            forced = args[i + 1]
            i += 2
            continue
        if a.startswith("--acct="):
            forced = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--browser":
            browser = True
            i += 1
            continue
        out.append(a)
        i += 1
    return out, forced, browser


def is_maintenance(args):
    return bool(args) and args[0] in MAINTENANCE


# ------------------------------------------------------------------- health

# Other agent CLIs on this machine. Claude is not the only way to get work
# done, and when both subscriptions are capped the useful answer is which tool
# is still available — not a wall. Never launched automatically: `claude` means
# Claude, and silently handing over a different model with different
# keybindings is worse than saying so.
VENDOR_CLIS = (("codex", "codex"), ("grok", "grok"),
               ("kimi", "kimi"), ("venice", "venice"))


def available_vendors():
    """Installed non-Claude agent CLIs, newest-used first."""
    import shutil as _shutil
    out = []
    for name, cmd in VENDOR_CLIS:
        if not _shutil.which(cmd):
            continue
        home = Path.home() / ("." + ("kimi-code" if name == "kimi" else name))
        try:
            used = max((p.stat().st_mtime for p in home.rglob("*")
                        if p.is_file()), default=0.0) if home.is_dir() else 0.0
        except OSError:
            used = 0.0
        out.append({"name": name, "cmd": cmd, "last_used": used})
    return sorted(out, key=lambda v: -v["last_used"])


def all_blocked(statuses, now):
    """Every usable account capped — nothing can start until one resets."""
    healthy = [s for s in statuses if s["state"] in HEALTHY and s["assessment"]]
    if not healthy:
        return None
    frees = [s["assessment"]["blocked_until"] for s in healthy]
    if any(f is None or f <= now for f in frees):
        return None
    return min(frees)


def exhausted_message(statuses, now, vendors, freed_at):
    lines = ["claude-acct: every account is capped."]
    for s in statuses:
        a = s["assessment"]
        if a and a["blocked_until"] and a["blocked_until"] > now:
            lines.append("  %s frees up in %s"
                         % (s.get("label") or s["name"],
                            humanize_mins(int((a["blocked_until"] - now) // 60))))
    if vendors:
        lines.append("Still available on this machine:")
        for v in vendors:
            when = ("last used %s" % humanize_ago(v["last_used"], now)
                    if v["last_used"] else "installed")
            lines.append("  %-8s (%s)" % (v["cmd"], when))
        lines.append("Start one of those, or wait %s."
                     % humanize_mins(int((freed_at - now) // 60)))
    else:
        lines.append("Wait %s." % humanize_mins(int((freed_at - now) // 60)))
    lines.append("To start Claude anyway: claude --acct <account>")
    return "\n".join(lines)


def remedy_plan(statuses, forced=None, interactive=True, minimum=MIN_ACCOUNTS):
    """Decide whether a session may start. Pure.

    -> (action, names_needing_login, message)
       action: "launch" (all good) | "collect" (log some in first) | "error"
    """
    healthy = [s for s in statuses if s["state"] in HEALTHY]
    broken = [s for s in statuses if s["state"] in BROKEN]
    absent = [s for s in statuses if s["state"] in ABSENT]
    need = [s["name"] for s in broken + absent]
    if len(healthy) >= minimum and not broken:
        return "launch", [], None

    problems = ["%s: %s" % (s.get("label") or s["name"],
                            STATE_TEXT.get(s["state"], s["state"]))
                for s in broken + absent]
    if not problems:
        problems.append("only %d working account%s, %d required"
                        % (len(healthy), "" if len(healthy) == 1 else "s",
                           minimum))
    msg = "; ".join(problems)
    if forced and any(s["name"] == forced and s["state"] in HEALTHY
                      for s in statuses):
        return "launch", need, msg + " (overridden by --acct %s)" % forced
    return ("collect" if interactive else "error"), need, msg


STATE_TEXT = {
    "ok": "working",
    "idle": "working (token refreshes at launch)",
    "offline": "working (usage API unreachable)",
    "auth": "credentials rejected (401/403)",
    "expired": "refresh token expired",
    "missing": "not logged in",
}


# ----------------------------------------------------------- cache, ledger

def last_launches():
    out = {}
    try:
        with open(LEDGER_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    out[rec["account"]] = rec["ts"]
                except (ValueError, KeyError):
                    pass
    except FileNotFoundError:
        pass
    return out


def log_launch(name, args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LEDGER_PATH.stat().st_size > 1_000_000:
            tail = LEDGER_PATH.read_text().splitlines()[-200:]
            LEDGER_PATH.write_text("\n".join(tail) + "\n")
    except FileNotFoundError:
        pass
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps({"ts": time.time(), "account": name,
                            "cwd": os.getcwd(), "args": args[:2]}) + "\n")


def _open_cache():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(CACHE_PATH, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    f.seek(0)
    try:
        cache = json.load(f)
    except ValueError:
        cache = {}
    return f, cache


def _save_cache(f, cache):
    f.seek(0)
    f.truncate()
    json.dump(cache, f)
    f.close()


def probe_all(accounts, model, now, force=False):
    """Credential state + usage assessment for every account (flocked)."""
    f, cache = _open_cache()
    try:
        refresh_cache(accounts, cache, now, force=force)
        statuses = []
        for acct in accounts:
            entry = cache.get(acct["name"]) or {}
            statuses.append({
                "name": acct["name"],
                "label": display_name(acct, accounts),
                "acct": acct,
                "state": entry.get("probe") or "missing",
                "assessment": assess(entry, model, now),
                "usage": entry.get("usage"),
            })
    finally:
        _save_cache(f, cache)
    return statuses


def apply_bump(name):
    """Nudge the winner's score so a burst of launches alternates accounts."""
    f, cache = _open_cache()
    entry = cache.setdefault(name, {})
    entry["bump"] = float(entry.get("bump") or 0.0) + HERD_BUMP
    _save_cache(f, cache)


# -------------------------------------------------------------- enumeration

def humanize_mins(mins):
    if mins < 60:
        return "%dm" % mins
    if mins < 60 * 24:
        return "%dh%02dm" % (mins // 60, mins % 60)
    return "%dd%dh" % (mins // (60 * 24), mins % (60 * 24) // 60)


def humanize_ago(ts, now):
    if not ts:
        return "never"
    return humanize_mins(int((now - ts) // 60)) + " ago"


def render_status(status, now):
    """Compact per-account chunk for the startup enumeration line."""
    a, state = status["assessment"], status["state"]
    who = status.get("label") or status["name"]
    if state in BROKEN or state in ABSENT:
        return "%s ✗%s" % (who, "auth" if state != "missing" else "no-login")
    if not a:
        return "%s ?" % who
    # Percentages alone make a pick look wrong (the busier account can be the
    # right one), so show what the decision actually turns on: time to reset.
    def window(pct, resets):
        if not resets or resets <= now:
            return "%.0f%%" % pct
        return "%.0f%%/%s" % (pct, humanize_mins(int((resets - now) // 60)))
    out = "%s s%s w%s" % (who, window(a["session"], a.get("session_resets")),
                          window(a["weekly"], a.get("weekly_resets")))
    if a["blocked_until"] and a["blocked_until"] > now:
        out += " blocked %s" % humanize_mins(int((a["blocked_until"] - now) // 60))
    if state == "offline":
        out += " offline"
    if a["age"] > CACHE_TTL * 2:
        out += " stale %s" % humanize_mins(int(a["age"] // 60))
    return out


def enumeration_line(statuses, now, winner=None, note=None):
    line = "claude-acct: " + " · ".join(render_status(s, now) for s in statuses)
    if winner:
        line += "  → %s" % winner
    if note:
        line += " (%s)" % note
    return line


def warn(msg):
    sys.stderr.write("claude-acct: %s\n" % msg)


def duplicate_identities(accounts):
    """Account names sharing one subscription — round-robin would be a no-op."""
    seen, dupes = {}, set()
    for acct in accounts:
        email = acct["email"] or account_email(acct)
        if not email:
            continue
        if email in seen:
            dupes.update((seen[email], acct["name"]))
        seen[email] = acct["name"]
    return sorted(dupes)


# -------------------------------------------------------------- launch/exec

def exec_claude(args, acct, note, statuses=None, now=None):
    env = dict(os.environ)
    env.pop("CLAUDE_ACCT", None)
    if acct is not None:
        if acct["dir"] is None:
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(acct["dir"])
        # so anything this session opens (claude-open, a doc link) lands in the
        # browser that belongs to the account the session is running on
        browser_env(acct, env)
    name = display_name(acct) if acct else "(env)"
    if statuses:
        sys.stderr.write(enumeration_line(statuses, now, name, note) + "\n")
    else:
        sys.stderr.write("claude-acct → %s%s\n"
                         % (name, " — " + note if note else ""))
    sys.stderr.flush()
    target = str(REAL_CLAUDE) if REAL_CLAUDE.exists() else "claude"
    os.execvpe(target, [target] + args, env)


def interactive():
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except ValueError:
        return False


def cmd_launch(argv):
    if "CLAUDE_CONFIG_DIR" in os.environ:
        exec_claude(argv, None, "CLAUDE_CONFIG_DIR preset, selection bypassed")
    accounts = load_accounts()
    args, forced, browser_flag = extract_acct_flag(argv)
    forced = forced or os.environ.get("CLAUDE_ACCT")
    if is_maintenance(args) and not forced:
        acct = default_account(accounts)
        log_launch(acct["name"], args)
        exec_claude(args, acct, args[0] + " → default account")

    now = time.time()
    model = launch_model(args)
    accounts = ensure_roster(accounts)
    statuses = probe_all(accounts, model, now)
    action, need, msg = remedy_plan(statuses, forced, interactive())

    if action != "launch":
        warn(msg)
        if action == "error":
            raise SystemExit(
                "claude-acct: refusing to start a session — %d of %d accounts "
                "working.\n  Fix from a terminal:  claude --acct-login\n"
                "  Bypass this wrapper:  command claude"
                % (len([s for s in statuses if s["state"] in HEALTHY]),
                   MIN_ACCOUNTS))
        collect_credentials(accounts, need)
        accounts = load_accounts()
        now = time.time()
        statuses = probe_all(accounts, model, now, force=True)
        action, _, msg = remedy_plan(statuses, forced, interactive=False)
        if action != "launch":
            raise SystemExit("claude-acct: still not ready — %s" % msg)
        dupes = duplicate_identities(accounts)
        if dupes:
            warn("%s share one subscription — they have the same quota pool"
                 % " and ".join(dupes))

    freed_at = None if forced else all_blocked(statuses, now)
    if freed_at:
        raise SystemExit(exhausted_message(statuses, now, available_vendors(),
                                           freed_at))

    if forced:
        acct = find_account(accounts, forced)
        note = "forced"
    else:
        # Browser work can only run where the extension is: restrict the pool
        # rather than pin, so it still balances once both accounts qualify.
        pool = ({a["name"] for a in browser_capable(accounts)}
                if wants_browser(args, browser_flag) else None)
        winner = pick([(s["name"], s["assessment"]) for s in statuses
                       if s["state"] in HEALTHY
                       and (pool is None or s["name"] in pool)],
                      last_launches(), now)
        acct = find_account(accounts, winner) if winner else default_account(accounts)
        note = ("browser-capable only" if pool and len(pool) < len(accounts)
                else None)
        apply_bump(acct["name"])
    if wants_browser(args, browser_flag) and not browser_running(acct) \
            and has_extension(acct):
        # asked for browser work and this account's browser is closed — start
        # it now rather than letting the session find an empty browser list
        profile = browser_profile(acct["name"], accounts)
        if profile is not None:
            warn("starting %s's browser" % display_name(acct, accounts))
            open_detached([str(BROWSER_SHIM), "https://claude.ai/"],
                          browser_env(acct, os.environ.copy()))
    log_launch(acct["name"], args)
    exec_claude(args, acct, note, statuses, now)


# ------------------------------------------------------------------ status

def cmd_status():
    accounts = ensure_roster(load_accounts())
    now = time.time()
    statuses = probe_all(accounts, launch_model([]), now, force=True)
    last = last_launches()
    winner = pick([(s["name"], s["assessment"]) for s in statuses
                   if s["state"] in HEALTHY], last, now)

    print("%-26s %-9s %-9s %-7s %-12s %s"
          % ("account", "session", "weekly", "pace", "last-launch", "state"))
    for s in statuses:
        a = s["assessment"]
        mark = "→" if s["name"] == winner else " "
        notes = [STATE_TEXT.get(s["state"], s["state"])]
        if a and a["blocked_until"] and a["blocked_until"] > now:
            notes.insert(0, "BLOCKED %s"
                         % humanize_mins(int((a["blocked_until"] - now) // 60)))
        if a and a["age"] > CACHE_TTL * 2:
            notes.append("snapshot %s old" % humanize_mins(int(a["age"] // 60)))
        print("%s %-25s %-9s %-9s %-7s %-12s %s"
              % (mark, email_of(s["acct"]) or s["name"],
                 "%.0f%%" % a["session"] if a else "—",
                 "%.0f%%" % a["weekly"] if a else "—",
                 "%.2f" % a["pace"] if a else "—",
                 humanize_ago(last.get(s["name"]), now),
                 ", ".join(notes)))

    action, need, msg = remedy_plan(statuses, None, interactive())
    if action != "launch":
        print("\nNot ready: %s" % msg)
        print("Collect credentials with:  claude --acct-login")
    for dup in [duplicate_identities(accounts)]:
        if dup:
            print("\nWARNING: %s share one subscription (same quota pool)."
                  % " and ".join(dup))
    model = launch_model([])
    rows = []
    for st in statuses:
        for cap in scoped_caps(st["usage"], now):
            binds = model_matches(cap["model"], model)
            left = ""
            if cap["resets_at"] and cap["resets_at"] > now:
                left = ", resets in %s" % humanize_mins(
                    int((cap["resets_at"] - now) // 60))
            rows.append("  %-26s %s %.0f%%%s%s"
                        % (email_of(st["acct"]) or st["name"], cap["model"],
                           cap["percent"], left,
                           "" if binds else "  (not counted above)"))
    if rows:
        # The weekly column only counts caps that constrain THIS launch, so a
        # maxed cap on another model is invisible there — exactly the number
        # someone asking "how much Fable is left?" came to find.
        print("\nModel-scoped weekly caps:")
        print("\n".join(rows))
    print("\nClaude in Chrome:")
    for st in statuses:
        acct, who = st["acct"], email_of(st["acct"]) or st["name"]
        state = browser_state(acct)
        fix = {
            "absent": "install it: claude --acct-browser %s",
            "unlinked": "open it and click the extension: claude --acct-browser %s",
            "closed": "start it: claude --acct-browser %s",
        }.get(state)
        print(("  %-26s %-28s %s"
               % (who, BROWSER_STATE_TEXT[state],
                  (fix % display_name(acct, accounts)) if fix else "")).rstrip())

    if model:
        print("\n(the weekly column counts %s-scoped caps; model from "
              "settings/--model)" % model)


# ------------------------------------------------------- credential collection

BROWSER_SHIM = CLAUDE_DIR / "bin" / "claude-acct-browser"
BROWSER_ROOT = Path.home() / ".claude-browsers"
BRAVE_DIR = Path.home() / ".config" / "BraveSoftware" / "Brave-Browser"
HOST_NAME = "com.anthropic.claude_code_browser_extension"
NATIVE_HOST_MANIFEST = BRAVE_DIR / "NativeMessagingHosts" / (HOST_NAME + ".json")
BINDING_FILE = "claude-config-dir"   # profile -> owning account
EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
EXTENSION_URL = "https://chromewebstore.google.com/detail/" + EXTENSION_ID


def browser_profile(acct_name, accounts=None):
    """Where this account browses. The default account uses the system browser
    — that profile is already its own; extra accounts get a dedicated one."""
    accounts = accounts if accounts is not None else load_accounts()
    for a in accounts:
        if a["name"] == acct_name and a["dir"] is None:
            return None
    return BROWSER_ROOT / acct_name


def profile_root(acct_name, accounts=None):
    """The browser user-data-dir backing this account, default profile included."""
    return browser_profile(acct_name, accounts) or BRAVE_DIR


def has_extension(acct):
    """Is Claude in Chrome installed in this account's browser? Chromium keeps
    extensions at <user-data-dir>/<Profile>/Extensions/<id>."""
    root = profile_root(acct["name"], [acct])
    try:
        return any((p / "Extensions" / EXTENSION_ID).is_dir()
                   for p in root.iterdir() if p.is_dir())
    except OSError:
        return False


def extension_activated(acct):
    """Has the extension in this account's browser actually been connected?

    Installed-on-disk is not enough: a freshly installed extension registers
    with no account until someone opens it and signs in, and a session then
    sees an empty browser list while everything looks correctly configured
    (observed 2026-08-19). An activated profile's extension storage carries the
    account binding; an untouched one is a near-empty store."""
    root = profile_root(acct["name"], [acct])
    for profile in ("Default", "."):
        store = root / profile / "Local Extension Settings" / EXTENSION_ID
        if not store.is_dir():
            continue
        try:
            files = sorted(store.iterdir(), key=lambda p: -p.stat().st_mtime)
        except OSError:
            continue
        budget = 8 << 20
        for f in files:
            if budget <= 0:
                break
            try:
                blob = f.read_bytes()[:budget]
            except OSError:
                continue
            budget -= len(blob)
            if b"accountUuid" in blob:
                return True
    return False


BROWSER_STATE_TEXT = {
    "ready": "ready",
    "closed": "browser not running",
    "unlinked": "extension never signed in",
    "absent": "extension not installed",
}


def browser_state(acct):
    """Why Claude in Chrome will or won't work for this account, in one word.
    The single classification: the SessionStart hook turns it into advice for a
    session, --acct-status into a column, and browser_ready into a boolean."""
    if not has_extension(acct):
        return "absent"
    if not extension_activated(acct):
        return "unlinked"
    if not browser_running(acct):
        return "closed"
    return "ready"


def browser_ready(acct):
    """Browser tools will actually work for this account right now."""
    return browser_state(acct) == "ready"


def browser_running(acct):
    """Is this account's browser actually up? An installed extension only
    counts while its browser is running — that is the difference between
    "browser tools work here" and "they return an empty list"."""
    profile = browser_profile(acct["name"], [acct])
    needle = "--user-data-dir=%s" % profile if profile else None
    try:
        pids = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for p in pids:
        try:
            cmd = (p / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if "brave" not in cmd and "chrom" not in cmd:
            continue
        if needle:
            if needle in cmd:
                return True
        elif "--user-data-dir=" not in cmd:   # the default profile
            return True
    return False


def ensure_browser_profile(name, acct_dir):
    """A self-contained browser session for one account: its own cookies (so
    its login can't be hijacked by whoever is signed into the default profile)
    and a one-line record of which account owns it.

    That record is the account binding. A native-messaging host inherits the
    environment of the browser that spawned it — verified 2026-08-19: a running
    host carried CLAUDE_ACCT_BROWSER_PROFILE and BROWSER straight from its
    browser — so the shim exports this profile's CLAUDE_CONFIG_DIR before
    exec'ing the browser, and every host that browser spawns lands on the right
    account. The env travels with the process; nothing has to win a race.

    Binding through the native-messaging manifest instead does NOT survive, and
    that is why this file exists: Brave reads manifests only from its product
    directory (never from a custom --user-data-dir), and Claude Code rewrites
    that one to its own shim on every --chrome session start — verified by
    reproduction, so any manifest we assert is undone by the next session."""
    profile = browser_profile(name)
    if profile is None:
        return None
    profile.mkdir(parents=True, exist_ok=True)
    (profile / BINDING_FILE).write_text(str(acct_dir) + "\n")
    # Earlier layouts bound the account through a per-profile manifest naming a
    # host shim. Brave never read either one; remove them so the next reader
    # doesn't take them for a live mechanism.
    shutil.rmtree(profile / "NativeMessagingHosts", ignore_errors=True)
    try:
        (profile / "native-host").unlink()
    except OSError:
        pass
    return profile


def browser_env(acct, env):
    """Point browser launches at this account's own profile (no-op for the
    default account, which already owns the system browser)."""
    profile = ensure_browser_profile(acct["name"], config_dir(acct))
    if profile is not None:
        env["BROWSER"] = str(BROWSER_SHIM)
        env["CLAUDE_ACCT_BROWSER_PROFILE"] = str(profile)
    return env


def duplicate_owner(email, others):
    """Which other account already signed in as this email (or None). Two
    config dirs on one subscription share a quota pool — round-robin no-op."""
    if not email:
        return None
    for name, other in (others or {}).items():
        if other and other.lower() == email.lower():
            return name
    return None


def other_emails(acct):
    return {a["name"]: (a["email"] or account_email(a))
            for a in load_accounts() if a["name"] != acct["name"]}


def prompt_email(name, taken):
    """Ask which subscription this slot is for; only prefills the login page."""
    if not interactive():
        return None
    if taken:
        print("Already in use: %s" % ", ".join(sorted(v for v in taken if v)))
    try:
        answer = input("Email for account %r (blank = type it in the browser): "
                       % name).strip()
    except EOFError:
        return None
    return answer or None


def reset_login(acct, why):
    """Undo a wrong login: drop the credentials and set that account's browser
    session aside so the retry starts signed out instead of re-authorizing the
    same subscription."""
    import subprocess
    warn(why)
    env = account_env(acct, os.environ.copy())
    subprocess.call([str(REAL_CLAUDE), "auth", "logout"], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    profile = browser_profile(acct["name"])
    if profile.exists():
        aside = profile.with_name("%s.wrong-account-%d"
                                  % (acct["name"], int(time.time())))
        profile.rename(aside)
        print("  browser session moved aside: %s" % aside)
    print("  retry with:  claude --acct-login %s" % acct["name"])


def account_env(acct, env):
    env.pop("CLAUDE_ACCT", None)
    if acct["dir"] is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir(acct))
    return env


def login_account(acct, email=None):
    """Run `claude auth login` in this account's config dir, authorizing
    through this account's own browser session. Interactive."""
    import subprocess
    dirpath = config_dir(acct)
    if acct["dir"] is not None and not (dirpath / ".claude.json").exists():
        cmd_setup(acct["name"], verbose=False)
    taken = other_emails(acct)
    print("\n=== Logging in account %r ===" % acct["name"])
    print("  config dir:      %s" % dirpath)
    print("  browser session: %s" % browser_profile(acct["name"]))
    print("A separate browser window opens, signed into nothing — sign in there")
    print("with the Max subscription this account should use.")
    email = email or acct["email"] or prompt_email(acct["name"], taken)

    env = browser_env(acct, account_env(acct, os.environ.copy()))
    cmd = [str(REAL_CLAUDE), "auth", "login", "--claudeai"]
    if email:
        cmd += ["--email", email]
    try:
        subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        print()

    if read_oauth(acct) is None:
        print("→ %s: still not logged in" % acct["name"])
        return False
    got = account_email(acct)
    clash = duplicate_owner(got, taken)
    if clash:
        reset_login(acct, "%s signed in as %s — the same subscription as %r. "
                          "Round-robin needs two different subscriptions."
                          % (acct["name"], got, clash))
        return False
    if got:
        update_account_email(acct["name"], got)
    print("→ %s: logged in as %s" % (acct["name"], got or "?"))
    return True


def open_detached(cmd, env):
    """Hand the browser off and return — never hold the terminal for as long
    as the window is open."""
    import subprocess
    subprocess.Popen(cmd, env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_browser(name=None):
    """Open the browser that belongs to one Claude login: the default account
    uses the system browser, every other account its own profile."""
    accounts = ensure_roster(load_accounts())
    acct = find_account(accounts, name) if name else default_account(accounts)
    who = display_name(acct, accounts)
    profile = browser_profile(acct["name"], accounts)

    if profile is None:
        env = os.environ.copy()
        for stray in ("BROWSER", "CLAUDE_ACCT_BROWSER_PROFILE"):
            env.pop(stray, None)   # never redirect it into another account's
        print("Opening the system browser — it is %s's own profile." % who)
        open_detached(["xdg-open", "https://claude.ai/"], env)
        return

    env = browser_env(acct, os.environ.copy())
    urls = ["https://claude.ai/"]
    if browser_running(acct):
        # Chromium hands the URL to the instance already on this profile rather
        # than starting one, and that instance keeps the environment it was
        # launched with — including the account binding a native host inherits.
        print("Note: %s's browser is already running, so this opens a tab in "
              "it. A binding change only takes effect on a fresh start — quit "
              "that window first if you need one." % who)
    print("Opening %s's browser session: %s" % (who, profile))
    if not has_extension(acct):
        urls.insert(0, EXTENSION_URL)
        print("Claude in Chrome isn't installed there yet — opening the "
              "extension page too. Sign in as %s." % (email_of(acct) or who))
    open_detached([str(BROWSER_SHIM)] + urls, env)


def collect_credentials(accounts, names):
    """Log in each named account, one flow at a time machine-wide.

    A broken token fails the health gate for EVERY launch, so a claude-resume
    burst arrives here all at once. The first tab runs the login; the rest wait
    on the lock and then re-check, because by the time they get it the account
    is usually already fixed — that turns one dead token from "the whole fleet
    exits, re-run claude-resume by hand" into one login and a short pause."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "acct-login.lock", "w")
    deadline = time.time() + LOGIN_WAIT
    waited = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                raise SystemExit(
                    "claude-acct: a login has been in progress in another "
                    "terminal for %d minutes.\n  Finish it there, then start "
                    "this session again." % (LOGIN_WAIT // 60))
            if not waited:
                warn("a login is in progress in another terminal — waiting for "
                     "it (up to %dm) rather than starting a second one"
                     % (LOGIN_WAIT // 60))
                waited = True
            time.sleep(1)
    try:
        now = time.time()
        for name in names:
            acct = find_account(accounts, name)
            oauth = read_oauth(acct)
            if oauth and oauth.get("accessToken") and refresh_alive(oauth, now):
                if waited:
                    warn("%s was logged in by the other terminal — continuing"
                         % display_name(acct, accounts))
                continue
            login_account(acct)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def cmd_login(name=None):
    accounts = ensure_roster(load_accounts())
    if name:
        login_account(find_account(accounts, name))
    else:
        now = time.time()
        statuses = probe_all(accounts, launch_model([]), now, force=True)
        need = [s["name"] for s in statuses
                if s["state"] in BROKEN or s["state"] in ABSENT]
        if not need:
            print("All %d accounts are working. Nothing to collect."
                  % len(statuses))
        else:
            collect_credentials(accounts, need)
    print()
    cmd_status()


# ------------------------------------------------------------------- setup

# Claude-Code-owned entries shared across accounts so every session — either
# account — sees one world: config, transcripts+memory (projects/), the fleet
# session registry (sessions/, read by _claude_sessions_lib via ~/.claude),
# rewind history, plans, pastes. Daemon/job state is deliberately NOT here:
# one supervisor per config dir. Tooling-owned files (state/, meta/, hooks
# output) need no links — the repo's scripts address ~/.claude absolutely.
SHARE = [
    "settings.json", "settings.local.json", "CLAUDE.md", "keybindings.json",
    ".mcp.json", "agents", "commands", "skills", "plugins",
    "projects", "sessions", "history.jsonl", "file-history", "plans",
    "paste-cache", "tasks", "todos",
]
# chrome/ is deliberately NOT shared: Claude Code regenerates that host shim
# per config dir, and sharing it made the two accounts rewrite one file.

# .claude.json keys copied to a new account so it doesn't re-onboard: MCP
# servers, onboarding/migration/nag flags, and per-project trust. Identity,
# telemetry, and caches are never copied. autoUpdates is forced off — the
# default account owns updates of the shared native install.
TOP_SEED = [
    "mcpServers", "hasCompletedOnboarding", "lastOnboardingVersion",
    "installMethod", "autoUpdatesProtectedForNative",
    "claudeInChromeDefaultEnabled", "hasCompletedClaudeInChromeOnboarding",
    "cachedChromeExtensionInstalled", "officialMarketplaceAutoInstalled",
    "officialMarketplaceAutoInstallAttempted", "opusProMigrationComplete",
    "sonnet1m45MigrationComplete", "fableOverageConsentV2",
    "hasSeenAutoDefaultNudge", "hasSeenAutoModeEntryWarning",
    "hasSeenUltrareviewTerms", "effortCalloutV2Dismissed",
    "resumeReturnDismissed", "remoteDialogSeen", "seenNotifications",
    "tipsHistory", "tipLifetimeShownCounts", "githubRepoPaths",
]
TOP_SEED_PREFIXES = ("unpin",)
PROJECT_SEED = [
    "hasTrustDialogAccepted", "hasCompletedProjectOnboarding", "allowedTools",
    "enabledMcpjsonServers", "disabledMcpjsonServers",
    "hasClaudeMdExternalIncludesApproved", "mcpServers", "mcpContextUris",
]


def seed_claude_json(src):
    out = {k: src[k] for k in TOP_SEED if k in src}
    out.update({k: v for k, v in src.items()
                if k.startswith(TOP_SEED_PREFIXES)})
    out["autoUpdates"] = False
    out["numStartups"] = 20  # past every "first N startups" nudge
    projects = {}
    for path, proj in (src.get("projects") or {}).items():
        kept = {k: proj[k] for k in PROJECT_SEED if k in proj}
        if kept:
            projects[path] = kept
    out["projects"] = projects
    return out


def _read_roster():
    try:
        return json.loads(ACCOUNTS_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return [{"name": "main", "configDir": None, "email": account_email(
            {"name": "main", "dir": None})}]


def register_account(name, dirpath):
    ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    roster = _read_roster()
    if not any(a["name"] == name for a in roster):
        roster.append({"name": name, "configDir": str(dirpath)})
    ACCOUNTS_PATH.write_text(json.dumps(roster, indent=2) + "\n")


def update_account_email(name, email):
    roster = _read_roster()
    for a in roster:
        if a["name"] == name:
            a["email"] = email
    ACCOUNTS_PATH.write_text(json.dumps(roster, indent=2) + "\n")


def cmd_setup(name, verbose=True):
    target = Path.home() / (".claude-" + name)
    say = print if verbose else (lambda *a, **k: None)
    say("Setting up account %r at %s" % (name, target))
    # Shared runtime dirs claude may not have created yet; a dangling symlink
    # in the new dir would otherwise shunt B's writes into a real local dir.
    for d in ("sessions", "todos", "paste-cache", "plans", "tasks",
              "projects", "file-history"):
        (CLAUDE_DIR / d).mkdir(exist_ok=True)
    (CLAUDE_DIR / "history.jsonl").touch(exist_ok=True)
    target.mkdir(mode=0o700, exist_ok=True)
    for entry in SHARE:
        src, dst = CLAUDE_DIR / entry, target / entry
        if not src.exists():
            continue  # settings.local.json etc. are optional
        if dst.is_symlink():
            if dst.resolve() == src.resolve():
                continue
            dst.unlink()
        elif dst.exists():
            warn("SKIP %s: a real file is already there" % entry)
            continue
        dst.symlink_to(src)
        say("  link %s" % entry)
    state = target / ".claude.json"
    if state.exists():
        say("  keep existing .claude.json")
    else:
        seed = seed_claude_json(
            json.loads((Path.home() / ".claude.json").read_text()))
        state.touch(mode=0o600)
        state.write_text(json.dumps(seed, indent=2) + "\n")
        say("  seed .claude.json (%d projects trusted, autoUpdates off)"
            % len(seed.get("projects", {})))
    stale_chrome = target / "chrome"
    if stale_chrome.is_symlink():
        stale_chrome.unlink()  # each account keeps its own generated host shim
    ensure_browser_profile(name, target)
    say("  browser session %s (own native-messaging host)"
        % browser_profile(name, [{"name": name, "dir": target}]))
    register_account(name, target)
    say("Done. Log it in with:  claude --acct-login %s" % name)


# -------------------------------------------------------------------- main

def main():
    argv = sys.argv[1:]
    try:
        if "--acct-status" in argv:
            return cmd_status()
        if argv[:1] == ["--acct-login"]:
            return cmd_login(argv[1] if len(argv) > 1 and
                             not argv[1].startswith("-") else None)
        if argv[:1] == ["--acct-browser"]:
            return cmd_browser(argv[1] if len(argv) > 1 and
                               not argv[1].startswith("-") else None)
        if argv[:1] == ["--acct-setup"]:
            if len(argv) < 2 or argv[1].startswith("-"):
                raise SystemExit("usage: claude-acct --acct-setup <name>")
            return cmd_setup(argv[1])
        cmd_launch(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:  # never leave the user unable to launch claude
        warn("failed (%s: %s) — plain launch" % (type(e).__name__, e))
        args, _, _ = extract_acct_flag(argv)
        os.execvpe(str(REAL_CLAUDE), [str(REAL_CLAUDE)] + args, os.environ.copy())


if __name__ == "__main__":
    main()
