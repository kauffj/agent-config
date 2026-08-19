#!/usr/bin/env python3
"""Policy tests for _claude_acct_lib (the dual Max account picker).

    python3 ~/.claude/bin/test_claude_acct.py

Stdlib only. Everything here is pure — payloads mirror the real
api/oauth/usage response shape (limits[] with kind session / weekly_all /
weekly_scoped) as observed live on 2026-08-18.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _claude_acct_lib as L  # noqa: E402

NOW = 1_800_000_000.0


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def usage(session=0.0, weekly=0.0, scoped=None, session_resets=NOW + 3600,
          weekly_resets=NOW + 6 * 86400):
    limits = [
        {"kind": "session", "group": "session", "percent": session,
         "severity": "normal", "resets_at": iso(session_resets), "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": weekly,
         "severity": "normal", "resets_at": iso(weekly_resets), "scope": None},
    ]
    if scoped:
        model, pct = scoped
        limits.append({"kind": "weekly_scoped", "group": "weekly",
                       "percent": pct, "severity": "warning",
                       "resets_at": iso(weekly_resets),
                       "scope": {"model": {"id": None, "display_name": model}}})
    return {"limits": limits}


def entry(u, fetched=NOW, bump=0.0):
    return {"fetched_at": fetched, "usage": u, "bump": bump}


def assess(u, model="fable", now=NOW, bump=0.0, fetched=NOW):
    return L.assess(entry(u, fetched=fetched, bump=bump), model, now)


class TestScoring(unittest.TestCase):
    def test_hourly_decides_when_weeklies_are_cool(self):
        # Audit example: session 10/weekly 60 must BEAT session 40/weekly 30 —
        # a mid-range weekly must not override the session signal.
        a = assess(usage(session=10, weekly=60))
        b = assess(usage(session=40, weekly=30))
        self.assertLess(a["score"], b["score"])

    def test_hot_weekly_overrules_any_session_gap(self):
        # The user ask: shift off an account whose weekly approaches cap,
        # even when its 5-hour window is nearly idle.
        hot = assess(usage(session=5, weekly=85))
        busy = assess(usage(session=70, weekly=30))
        self.assertGreater(hot["score"], busy["score"])

    def test_penalty_starts_at_soft_cap(self):
        self.assertEqual(L.score(20, L.WEEKLY_SOFT_CAP), 20)
        self.assertGreater(L.score(20, L.WEEKLY_SOFT_CAP + 1), 20)

    def test_scoped_weekly_counts_for_matching_model(self):
        # Live shape: weekly_all 45 but Fable-scoped 83 — the scoped cap is
        # the real constraint when launching fable.
        a = assess(usage(session=35, weekly=45, scoped=("Fable", 83)))
        self.assertEqual(a["weekly"], 83)

    def test_scoped_weekly_ignored_for_other_model(self):
        a = assess(usage(session=35, weekly=45, scoped=("Opus", 97)),
                   model="fable")
        self.assertEqual(a["weekly"], 45)

    def test_model_matching_is_loose_both_ways(self):
        scope = {"model": {"id": None, "display_name": "Opus"}}
        self.assertTrue(L.model_matches(L.scope_model(scope), "claude-opus-5"))
        self.assertFalse(L.model_matches(L.scope_model(scope), "fable"))
        self.assertTrue(L.model_matches(L.scope_model(None), "fable"))  # unscoped
        self.assertTrue(L.model_matches(L.scope_model(scope), None))    # unknown
        self.assertEqual(L.scope_model({"model": {"id": "claude-fable-5"}}),
                         "claude-fable-5")                        # id fallback

    def test_bump_is_added(self):
        self.assertEqual(assess(usage(session=10), bump=3.0)["score"],
                         assess(usage(session=10))["score"] + 3.0)


class TestResetDecay(unittest.TestCase):
    """An expired-token account is judged from its last-good snapshot; any
    window whose resets_at has passed must count as 0%."""

    def test_session_window_rolled_over(self):
        a = assess(usage(session=90, weekly=40, session_resets=NOW - 60))
        self.assertEqual(a["session"], 0)
        self.assertEqual(a["weekly"], 40)

    def test_weekly_rolled_over(self):
        a = assess(usage(session=20, weekly=95, weekly_resets=NOW - 60))
        self.assertEqual(a["weekly"], 0)

    def test_synthesized_from_totals_when_limits_missing(self):
        u = {"five_hour": {"utilization": 30.0, "resets_at": iso(NOW + 100)},
             "seven_day": {"utilization": 88.0, "resets_at": iso(NOW + 86400)}}
        a = L.assess(entry(u), "fable", NOW)
        self.assertEqual((a["session"], a["weekly"]), (30.0, 88.0))

    def test_empty_usage_scores_zero(self):
        a = L.assess(entry({"limits": []}), "fable", NOW)
        self.assertEqual(a["score"], 0)

    def test_no_usage_is_none(self):
        self.assertIsNone(L.assess({}, "fable", NOW))
        self.assertIsNone(L.assess(None, "fable", NOW))


class TestBlocked(unittest.TestCase):
    def test_active_limit_at_cap_blocks_until_reset(self):
        a = assess(usage(session=100, weekly=50, session_resets=NOW + 1800))
        self.assertAlmostEqual(a["blocked_until"], NOW + 1800)

    def test_below_cap_is_not_blocked(self):
        self.assertIsNone(assess(usage(session=98, weekly=98))["blocked_until"])

    def test_blocked_account_is_skipped(self):
        cands = [("a", assess(usage(session=100, session_resets=NOW + 1800))),
                 ("b", assess(usage(session=90, weekly=90)))]
        self.assertEqual(L.pick(cands, {}, NOW), "b")

    def test_all_blocked_picks_soonest_reset(self):
        cands = [("a", assess(usage(session=100, session_resets=NOW + 3000))),
                 ("b", assess(usage(session=100, session_resets=NOW + 600)))]
        self.assertEqual(L.pick(cands, {}, NOW), "b")


class TestPick(unittest.TestCase):
    def test_lowest_score_wins(self):
        cands = [("a", assess(usage(session=50))),
                 ("b", assess(usage(session=20)))]
        self.assertEqual(L.pick(cands, {}, NOW), "b")

    def test_tie_goes_to_least_recently_launched(self):
        cands = [("a", assess(usage(session=30))),
                 ("b", assess(usage(session=30)))]
        self.assertEqual(L.pick(cands, {"a": NOW - 10, "b": NOW - 900}, NOW), "b")
        self.assertEqual(L.pick(cands, {"a": NOW - 900, "b": NOW - 10}, NOW), "a")

    def test_burst_alternates_via_bump(self):
        # First launch picked "a" and bumped it; the very next launch inside
        # the cache window must flip to "b".
        cands = [("a", assess(usage(session=30), bump=L.HERD_BUMP)),
                 ("b", assess(usage(session=31)))]
        self.assertEqual(L.pick(cands, {}, NOW), "b")

    def test_known_beats_unknown(self):
        cands = [("a", None), ("b", assess(usage(session=60)))]
        self.assertEqual(L.pick(cands, {}, NOW), "b")

    def test_all_unknown_falls_back_to_lru(self):
        cands = [("a", None), ("b", None)]
        self.assertEqual(L.pick(cands, {"a": NOW - 5, "b": NOW - 50}, NOW), "b")

    def test_nothing_at_all(self):
        self.assertIsNone(L.pick([], {}, NOW))


class TestArgHandling(unittest.TestCase):
    def setUp(self):
        # capability is normally sniffed off the real disk (this machine's
        # default Brave really does have the extension) — pin it for the
        # flag-logic tests below
        self._real = L.has_extension
        L.has_extension = lambda acct: False

    def tearDown(self):
        L.has_extension = self._real

    def test_extract_acct_flag_forms(self):
        self.assertEqual(L.extract_acct_flag(["--acct", "alt", "--resume", "x"]),
                         (["--resume", "x"], "alt", False))
        self.assertEqual(L.extract_acct_flag(["--acct=alt", "-p", "hi"]),
                         (["-p", "hi"], "alt", False))
        self.assertEqual(L.extract_acct_flag(["--resume", "x"]),
                         (["--resume", "x"], None, False))
        # --browser is the wrapper's own; claude never sees it
        self.assertEqual(L.extract_acct_flag(["--browser", "-c"]),
                         (["-c"], None, True))

    def test_browser_work_restricted_to_capable_accounts(self):
        # Claude in Chrome is account-scoped (verified: the second account saw
        # zero connected browsers), so a browser session must land where the
        # extension is — but as a pool, not a pin.
        accounts = [{"name": "main", "dir": None, "email": None, "browser": False},
                    {"name": "alt", "dir": Path("/x"), "email": None,
                     "browser": True}]
        self.assertEqual([a["name"] for a in L.browser_capable(accounts)], ["alt"])
        self.assertTrue(L.wants_browser(["--chrome"]))       # explicit claude flag
        self.assertTrue(L.wants_browser([], flagged=True))   # wrapper --browser
        self.assertFalse(L.wants_browser(["-c"]))

    def test_both_capable_accounts_still_balance(self):
        accounts = [{"name": "main", "dir": None, "email": None, "browser": True},
                    {"name": "alt", "dir": Path("/x"), "email": None,
                     "browser": True}]
        self.assertEqual(len(L.browser_capable(accounts)), 2)

    def test_capability_falls_back_to_the_default_account(self):
        accounts = [{"name": "main", "dir": None, "email": None, "browser": False},
                    {"name": "alt", "dir": Path("/nonexistent"), "email": None,
                     "browser": False}]
        self.assertEqual([a["name"] for a in L.browser_capable(accounts)], ["main"])

    def test_maintenance_is_first_positional_only(self):
        self.assertTrue(L.is_maintenance(["mcp", "list"]))
        self.assertTrue(L.is_maintenance(["update"]))
        self.assertFalse(L.is_maintenance(["-p", "update"]))  # a prompt
        self.assertFalse(L.is_maintenance(["--resume", "abc"]))
        self.assertFalse(L.is_maintenance([]))

    def test_launch_model_flag_forms(self):
        self.assertEqual(L.launch_model(["--model", "opus", "-p", "x"]), "opus")
        self.assertEqual(L.launch_model(["--model=sonnet"]), "sonnet")


def status(name, state, u=None, **kw):
    return {"name": name, "acct": {"name": name, "dir": None, "email": None},
            "state": state,
            "assessment": assess(u, **kw) if u is not None else None}


class TestHealthGate(unittest.TestCase):
    """Two working accounts are required before any session may start, and a
    configured account whose credentials are broken is an error — but a
    network blip never is."""

    def test_two_working_accounts_launch(self):
        st = [status("main", "ok", usage(session=50)),
              status("alt", "ok", usage(session=10))]
        self.assertEqual(L.remedy_plan(st)[0], "launch")

    def test_expired_access_token_still_counts_as_working(self):
        # claude refreshes it at launch; nothing is wrong with the account.
        st = [status("main", "ok", usage(session=50)), status("alt", "idle")]
        self.assertEqual(L.remedy_plan(st)[0], "launch")

    def test_network_failure_never_reads_as_broken(self):
        # Offline laptop must still be able to start a session.
        st = [status("main", "offline"), status("alt", "offline")]
        self.assertEqual(L.remedy_plan(st)[0], "launch")

    def test_single_account_does_not_launch(self):
        st = [status("main", "ok", usage(session=50))]
        action, need, msg = L.remedy_plan(st)
        self.assertEqual(action, "collect")
        self.assertIn("1 working account", msg)

    def test_missing_account_is_collected(self):
        st = [status("main", "ok", usage(session=50)), status("alt", "missing")]
        action, need, msg = L.remedy_plan(st)
        self.assertEqual((action, need), ("collect", ["alt"]))
        self.assertIn("not logged in", msg)

    def test_broken_credentials_error_even_with_enough_others(self):
        st = [status("main", "ok", usage()), status("alt", "ok", usage()),
              status("third", "auth")]
        action, need, msg = L.remedy_plan(st)
        self.assertEqual((action, need), ("collect", ["third"]))
        self.assertIn("rejected", msg)

    def test_dead_refresh_token_is_broken(self):
        st = [status("main", "ok", usage()), status("alt", "expired")]
        self.assertEqual(L.remedy_plan(st)[0], "collect")

    def test_non_interactive_errors_instead_of_collecting(self):
        st = [status("main", "ok", usage()), status("alt", "missing")]
        self.assertEqual(L.remedy_plan(st, interactive=False)[0], "error")

    def test_forced_working_account_overrides_the_gate(self):
        # Escape hatch: a wrapper bug must never lock the user out of claude.
        st = [status("main", "ok", usage()), status("alt", "missing")]
        action, _, msg = L.remedy_plan(st, forced="main")
        self.assertEqual(action, "launch")
        self.assertIn("overridden", msg)

    def test_forced_broken_account_does_not_override(self):
        st = [status("main", "ok", usage()), status("alt", "missing")]
        self.assertEqual(L.remedy_plan(st, forced="alt")[0], "collect")


class TestCredentialState(unittest.TestCase):
    def test_refresh_alive(self):
        self.assertTrue(L.refresh_alive({"refreshTokenExpiresAt":
                                         (NOW + 86400) * 1000}, NOW))
        self.assertFalse(L.refresh_alive({"refreshTokenExpiresAt":
                                          (NOW - 1) * 1000}, NOW))
        self.assertTrue(L.refresh_alive({}, NOW))  # absent = no expiry known

    def test_token_usable_needs_headroom(self):
        self.assertTrue(L.token_usable({"accessToken": "x",
                                        "expiresAt": (NOW + 600) * 1000}, NOW))
        self.assertFalse(L.token_usable({"accessToken": "x",
                                         "expiresAt": (NOW - 1) * 1000}, NOW))
        self.assertFalse(L.token_usable({"expiresAt": (NOW + 600) * 1000}, NOW))

    def test_probe_states_from_fetch(self):
        # refresh_cache must map a 401 to "auth" and a timeout to "offline",
        # keeping the last-good usage snapshot in both cases.
        accts = [{"name": "a", "dir": None, "email": None}]
        cache = {"a": {"usage": usage(session=5), "fetched_at": NOW - 999}}
        creds = {"accessToken": "t", "expiresAt": (NOW + 600) * 1000}
        real_read = L.read_oauth
        L.read_oauth = lambda acct: creds
        try:
            L.refresh_cache(accts, cache, NOW, fetcher=lambda t: (None, "auth"))
            self.assertEqual(cache["a"]["probe"], "auth")
            L.refresh_cache(accts, cache, NOW,
                            fetcher=lambda t: (None, "network"))
            self.assertEqual(cache["a"]["probe"], "offline")
            self.assertIn("usage", cache["a"])  # snapshot survives both
            L.refresh_cache(accts, cache, NOW,
                            fetcher=lambda t: (usage(session=7), None))
            self.assertEqual(cache["a"]["probe"], "ok")
        finally:
            L.read_oauth = real_read


class TestEnumeration(unittest.TestCase):
    def test_line_shows_every_account_and_the_winner(self):
        st = [status("main", "ok", usage(session=79, weekly=100)),
              status("alt", "ok", usage(session=12, weekly=41))]
        line = L.enumeration_line(st, NOW, winner="alt")
        self.assertIn("main s79% w100%", line)
        self.assertIn("alt s12% w41%", line)
        self.assertIn("→ alt", line)
        self.assertIn("blocked", line)   # weekly at 100%
        self.assertEqual(len(line.splitlines()), 1)  # brief: one line

    def test_broken_account_is_visible(self):
        st = [status("main", "ok", usage()), status("alt", "missing")]
        self.assertIn("alt ✗no-login", L.enumeration_line(st, NOW))


class TestBrowserState(unittest.TestCase):
    """One classification feeds the hook's advice, the status column and the
    boolean — so they cannot drift apart."""

    ACCT = {"name": "alt", "dir": Path("/tmp/cfg-alt"), "email": None,
            "browser": False}

    def _with(self, installed, activated, running):
        L.has_extension = lambda a: installed
        L.extension_activated = lambda a: activated
        L.browser_running = lambda a: running

    def test_missing_link_in_the_chain_names_itself(self):
        real = (L.has_extension, L.extension_activated, L.browser_running)
        try:
            self._with(False, False, False)
            self.assertEqual(L.browser_state(self.ACCT), "absent")
            self._with(True, False, True)
            self.assertEqual(L.browser_state(self.ACCT), "unlinked")
            self._with(True, True, False)
            self.assertEqual(L.browser_state(self.ACCT), "closed")
            self._with(True, True, True)
            self.assertEqual(L.browser_state(self.ACCT), "ready")
            self.assertTrue(L.browser_ready(self.ACCT))
            # every state has text to show for it
            for state in ("absent", "unlinked", "closed", "ready"):
                self.assertIn(state, L.BROWSER_STATE_TEXT)
        finally:
            (L.has_extension, L.extension_activated, L.browser_running) = real

    def test_only_ready_counts_as_ready(self):
        real = (L.has_extension, L.extension_activated, L.browser_running)
        try:
            for combo in ((False, False, False), (True, False, True),
                          (True, True, False)):
                self._with(*combo)
                self.assertFalse(L.browser_ready(self.ACCT), combo)
        finally:
            (L.has_extension, L.extension_activated, L.browser_running) = real


class TestScopedCapVisibility(unittest.TestCase):
    """A cap the scorer filters out is still a number a human came to find."""

    def test_lists_caps_the_launch_model_does_not_bind(self):
        caps = L.scoped_caps(usage(session=1, weekly=58, scoped=("Fable", 100)), NOW)
        self.assertEqual([(c["model"], c["percent"]) for c in caps],
                         [("Fable", 100.0)])
        # ...while scoring an opus launch still ignores it
        self.assertEqual(L.assess(entry(usage(session=1, weekly=58,
                                              scoped=("Fable", 100))),
                                  "opus[1m]", NOW)["weekly"], 58.0)

    def test_unscoped_limits_are_not_model_caps(self):
        self.assertEqual(L.scoped_caps(usage(session=9, weekly=44), NOW), [])

    def test_rolled_over_window_reads_empty(self):
        caps = L.scoped_caps(
            usage(scoped=("Fable", 100), weekly_resets=NOW - 60), NOW)
        self.assertEqual(caps[0]["percent"], 0.0)

    def test_sorted_worst_first(self):
        payload = {"limits": [
            {"kind": "weekly_scoped", "percent": 12, "resets_at": iso(NOW + 60),
             "scope": {"model": {"display_name": "Opus"}}},
            {"kind": "weekly_scoped", "percent": 97, "resets_at": iso(NOW + 60),
             "scope": {"model": {"display_name": "Fable"}}}]}
        self.assertEqual([c["model"] for c in L.scoped_caps(payload, NOW)],
                         ["Fable", "Opus"])

    def test_cached_usage_survives_a_missing_or_corrupt_cache(self):
        real = L.CACHE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                L.CACHE_PATH = Path(tmp) / "gone.json"
                self.assertIsNone(L.cached_usage("main"))
                L.CACHE_PATH.write_text("{not json")
                self.assertIsNone(L.cached_usage("main"))
                L.CACHE_PATH.write_text(json.dumps(
                    {"main": {"usage": usage(scoped=("Fable", 100))}}))
                self.assertEqual(
                    L.scoped_caps(L.cached_usage("main"), NOW)[0]["percent"], 100.0)
                self.assertIsNone(L.cached_usage("nobody"))
            finally:
                L.CACHE_PATH = real


class TestIsolatedBrowserLogin(unittest.TestCase):
    """Each account authorizes in its own browser session. The default profile
    is signed into one subscription, so logging in there just re-authorizes
    that account — two config dirs sharing one quota pool."""

    ROSTER = [{"name": "main", "dir": None, "email": None, "browser": False},
              {"name": "alt", "dir": Path("/tmp/cfg-alt"), "email": None,
               "browser": False}]

    def test_default_account_keeps_the_system_browser(self):
        # It already owns the default profile; only extra accounts need one.
        self.assertIsNone(L.browser_profile("main", self.ROSTER))
        self.assertIsNotNone(L.browser_profile("alt", self.ROSTER))
        # and a dedicated profile never lives inside ~/.claude (the git repo)
        self.assertNotIn(L.CLAUDE_DIR, L.browser_profile("alt", self.ROSTER).parents)

    def test_login_env_points_claude_at_the_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_root = L.BROWSER_ROOT
            L.BROWSER_ROOT = Path(tmp)
            try:
                env = L.browser_env(self.ROSTER[1], {})
                self.assertEqual(env["BROWSER"], str(L.BROWSER_SHIM))
                self.assertEqual(env["CLAUDE_ACCT_BROWSER_PROFILE"],
                                 str(Path(tmp) / "alt"))
            finally:
                L.BROWSER_ROOT = real_root

    def test_profile_records_the_account_that_owns_it(self):
        # The binding is a one-line file in the profile, read by the shim.
        with tempfile.TemporaryDirectory() as tmp:
            real_root = L.BROWSER_ROOT
            L.BROWSER_ROOT = Path(tmp)
            try:
                profile = L.ensure_browser_profile("alt", Path("/tmp/cfg-alt"))
                binding = profile / L.BINDING_FILE
                self.assertEqual(binding.read_text().strip(), "/tmp/cfg-alt")
            finally:
                L.BROWSER_ROOT = real_root

    def test_inert_manifest_layout_is_cleaned_up(self):
        # Brave never read these (product dir only); leaving them behind would
        # read as a live mechanism to the next person.
        with tempfile.TemporaryDirectory() as tmp:
            real_root = L.BROWSER_ROOT
            L.BROWSER_ROOT = Path(tmp)
            try:
                stale = Path(tmp) / "alt"
                (stale / "NativeMessagingHosts").mkdir(parents=True)
                (stale / "NativeMessagingHosts" / "x.json").write_text("{}")
                (stale / "native-host").write_text("#!/bin/sh\n")
                profile = L.ensure_browser_profile("alt", Path("/tmp/cfg-alt"))
                self.assertFalse((profile / "NativeMessagingHosts").exists())
                self.assertFalse((profile / "native-host").exists())
            finally:
                L.BROWSER_ROOT = real_root

    def test_shim_exports_the_binding_to_the_browser(self):
        # End-to-end through the real shim with a stub browser: whatever the
        # browser gets, every native-messaging host it spawns inherits.
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "prof"
            profile.mkdir()
            (profile / L.BINDING_FILE).write_text("/tmp/cfg-alt\n")
            stub_dir = Path(tmp) / "bin"
            stub_dir.mkdir()
            stub = stub_dir / "brave-browser"
            stub.write_text('#!/bin/sh\nprintf "%s" "$CLAUDE_CONFIG_DIR"\n')
            stub.chmod(0o755)
            out = subprocess.run(
                [str(L.BROWSER_SHIM), "https://claude.ai/"],
                env={"PATH": str(stub_dir) + ":/usr/bin:/bin",
                     "CLAUDE_ACCT_BROWSER_PROFILE": str(profile)},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.strip(), "/tmp/cfg-alt")

    def test_shim_scrubs_the_opening_session_env(self):
        # A browser opened from inside a Claude session must not hand that
        # session's identity to every native-messaging host it spawns.
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "prof"
            profile.mkdir()
            (profile / L.BINDING_FILE).write_text("/tmp/cfg-alt\n")
            stub_dir = Path(tmp) / "bin"
            stub_dir.mkdir()
            stub = stub_dir / "brave-browser"
            stub.write_text('#!/bin/sh\n'
                            'printf "%s|%s|%s" "${CLAUDECODE-unset}" '
                            '"${CLAUDE_CODE_SESSION_ID-unset}" '
                            '"$CLAUDE_CONFIG_DIR"\n')
            stub.chmod(0o755)
            out = subprocess.run(
                [str(L.BROWSER_SHIM), "https://claude.ai/"],
                env={"PATH": str(stub_dir) + ":/usr/bin:/bin",
                     "CLAUDE_ACCT_BROWSER_PROFILE": str(profile),
                     "CLAUDECODE": "1",
                     "CLAUDE_CODE_SESSION_ID": "abc-123",
                     "CLAUDE_CODE_MESSAGING_TOKEN": "secret"},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.strip(), "unset|unset|/tmp/cfg-alt")

    def test_shim_without_a_binding_leaves_the_default_account(self):
        # An unbound profile must not inherit whatever the caller happened to
        # have exported — the default account owns the system browser.
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "prof"
            profile.mkdir()
            stub_dir = Path(tmp) / "bin"
            stub_dir.mkdir()
            stub = stub_dir / "brave-browser"
            stub.write_text('#!/bin/sh\nprintf "%s" "${CLAUDE_CONFIG_DIR-unset}"\n')
            stub.chmod(0o755)
            out = subprocess.run(
                [str(L.BROWSER_SHIM), "https://claude.ai/"],
                env={"PATH": str(stub_dir) + ":/usr/bin:/bin",
                     "CLAUDE_ACCT_BROWSER_PROFILE": str(profile)},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.strip(), "unset")

    def test_extension_presence_is_detected_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_root = L.BROWSER_ROOT
            L.BROWSER_ROOT = Path(tmp)
            acct = {"name": "alt", "dir": Path("/tmp/cfg-alt"), "email": None,
                    "browser": False}
            try:
                (Path(tmp) / "alt").mkdir()
                self.assertFalse(L.has_extension(acct))
                (Path(tmp) / "alt" / "Default" / "Extensions" /
                 L.EXTENSION_ID).mkdir(parents=True)
                self.assertTrue(L.has_extension(acct))
            finally:
                L.BROWSER_ROOT = real_root

    def test_shim_is_executable_and_needs_a_profile(self):
        self.assertTrue(os.access(L.BROWSER_SHIM, os.X_OK))
        p = subprocess.run([str(L.BROWSER_SHIM), "https://example.com"],
                           env={"PATH": os.environ["PATH"]},
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("CLAUDE_ACCT_BROWSER_PROFILE", p.stderr)

    def test_shim_isolates_the_browser_it_launches(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin, profile = os.path.join(tmp, "bin"), os.path.join(tmp, "prof")
            os.makedirs(fake_bin)
            brave = os.path.join(fake_bin, "brave-browser")
            with open(brave, "w") as f:
                f.write('#!/bin/sh\necho "$@"\n')
            os.chmod(brave, 0o755)
            p = subprocess.run(
                [str(L.BROWSER_SHIM), "https://claude.com/oauth"],
                env={"PATH": fake_bin + ":" + os.environ["PATH"],
                     "CLAUDE_ACCT_BROWSER_PROFILE": profile},
                capture_output=True, text=True)
            self.assertIn("--user-data-dir=" + profile, p.stdout)
            self.assertIn("https://claude.com/oauth", p.stdout)
            self.assertTrue(os.path.isdir(profile))

    def test_duplicate_login_is_detected(self):
        others = {"main": "a@b.c"}
        self.assertEqual(L.duplicate_owner("a@b.c", others), "main")
        self.assertEqual(L.duplicate_owner("A@B.C", others), "main")  # case
        self.assertIsNone(L.duplicate_owner("other@b.c", others))
        self.assertIsNone(L.duplicate_owner(None, others))
        self.assertIsNone(L.duplicate_owner("a@b.c", {"main": None}))


class TestRoster(unittest.TestCase):
    def test_next_name_prefers_alt_then_numbers(self):
        self.assertEqual(L.next_account_name({"main"}), "alt")
        self.assertEqual(L.next_account_name({"main", "alt"}), "acct3")
        self.assertEqual(L.next_account_name({"main", "alt", "acct3"}), "acct4")


class TestSeed(unittest.TestCase):
    SRC = {
        "oauthAccount": {"emailAddress": "x@y.z"},
        "userID": "deadbeef",
        "anonymousId": "anon",
        "firstStartTime": "2026-01-01",
        "cachedGrowthBookFeatures": {"big": "cache"},
        "mcpServers": {"google-docs": {"command": "npx"}},
        "hasCompletedOnboarding": True,
        "autoUpdates": True,
        "unpinFableBanner": True,
        "tipsHistory": {"tip": 1},
        "numStartups": 999,
        "projects": {
            "/home/you/projects/demo-app": {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["Bash(npm:*)"],
                "exampleFiles": ["a", "b"],
                "lastTotalWebSearchRequests": 42,
            },
            "/tmp/empty": {"lastCost": 1},
        },
    }

    def test_identity_and_caches_never_copied(self):
        out = L.seed_claude_json(self.SRC)
        for key in ("oauthAccount", "userID", "anonymousId", "firstStartTime",
                    "cachedGrowthBookFeatures"):
            self.assertNotIn(key, out)

    def test_onboarding_mcp_and_unpin_copied(self):
        out = L.seed_claude_json(self.SRC)
        self.assertEqual(out["mcpServers"], self.SRC["mcpServers"])
        self.assertTrue(out["hasCompletedOnboarding"])
        self.assertTrue(out["unpinFableBanner"])
        self.assertEqual(out["tipsHistory"], {"tip": 1})

    def test_auto_updates_forced_off(self):
        # The default account owns updates of the shared native install.
        self.assertFalse(L.seed_claude_json(self.SRC)["autoUpdates"])

    def test_projects_stripped_to_trust_and_approvals(self):
        out = L.seed_claude_json(self.SRC)
        proj = out["projects"]["/home/you/projects/demo-app"]
        self.assertEqual(proj, {"hasTrustDialogAccepted": True,
                                "allowedTools": ["Bash(npm:*)"]})
        # a project with nothing seed-worthy is dropped entirely
        self.assertNotIn("/tmp/empty", out["projects"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
