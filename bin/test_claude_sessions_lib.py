#!/usr/bin/env python3
"""Regression tests for _claude_sessions_lib.

    python3 ~/.claude/bin/test_claude_sessions_lib.py

Stdlib only, no fixtures — the interesting cases are about agreeing with how
Claude actually names transcript directories on this disk, so the end-to-end
test reads the real ones rather than inventing them.
"""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _claude_sessions_lib as L  # noqa: E402


class TestProjectDir(unittest.TestCase):
    """Claude encodes a cwd into a transcript dir by replacing every
    non-alphanumeric char with "-". Encoding only "/" pointed dotted cwds at a
    directory that never exists, so transcript_exists() returned False and
    resume_set() silently dropped them (2026-08-02: example.com, 2 of 18)."""

    def enc(self, cwd):
        return os.path.basename(L._project_dir(cwd))

    def test_dots_become_dashes(self):
        # The regression. Every dotted project on this machine hit it.
        self.assertEqual(self.enc("/home/you/projects/example.com"),
                         "-home-you-projects-example-com")
        self.assertEqual(self.enc("/home/you/projects/notes.net"),
                         "-home-you-projects-notes-net")
        self.assertEqual(self.enc("/home/you/projects/some.party"),
                         "-home-you-projects-some-party")

    def test_hidden_dir_yields_double_dash(self):
        # A leading dot collapses to its own dash next to the path separator's,
        # matching -home-you-projects-demo-app--claude-worktrees-... on disk.
        self.assertEqual(
            self.enc("/home/you/projects/demo-app/.claude/worktrees/lead-intake-unified"),
            "-home-you-projects-demo-app--claude-worktrees-lead-intake-unified")

    def test_plain_paths_unchanged(self):
        # The pre-fix behaviour these paths already had must not drift.
        self.assertEqual(self.enc("/home/you/projects/demo-app"),
                         "-home-you-projects-demo-app")
        self.assertEqual(self.enc("/home/you"), "-home-you")

    def test_case_and_digits_preserved(self):
        self.assertEqual(self.enc("/home/you/Downloads"), "-home-you-Downloads")
        self.assertEqual(self.enc("/home/you/projects/invoices-2026"),
                         "-home-you-projects-invoices-2026")

    def test_underscores_and_spaces_become_dashes(self):
        self.assertEqual(self.enc("/tmp/my_project dir"), "-tmp-my-project-dir")


class TestAgreesWithDisk(unittest.TestCase):
    """End-to-end: every transcript we can actually find must be locatable via
    the cwd recorded inside it. This is the check that would have caught the
    bug — it fails for any cwd whose encoding we get wrong, without needing to
    know in advance which character class is at fault."""

    def test_every_recent_transcript_is_locatable(self):
        sessions = L.recent_transcript_sessions(limit=60)
        if not sessions:
            self.skipTest("no transcripts on this machine")
        missed = [(s["sessionId"][:8], s["cwd"]) for s in sessions
                  if os.path.isdir(s["cwd"])
                  and not L.transcript_exists(s["sessionId"], s["cwd"])]
        self.assertEqual(missed, [], "transcript_exists() could not find: %r" % (missed,))

    def test_dotted_cwds_are_actually_covered(self):
        # Guards the test above from silently going vacuous: if this machine has
        # no dotted project, the end-to-end check can't prove the fix.
        sessions = L.recent_transcript_sessions(limit=60)
        dotted = [s for s in sessions if "." in os.path.basename(s["cwd"])]
        if not dotted:
            self.skipTest("no dotted-cwd sessions to exercise")
        for s in dotted:
            if os.path.isdir(s["cwd"]):
                self.assertTrue(L.transcript_exists(s["sessionId"], s["cwd"]),
                                "dotted cwd not found: %s" % s["cwd"])


def _load_snapshot():
    """bin/claude-snapshot has no .py suffix; load it by path."""
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        "claude-snapshot")
    spec = importlib.util.spec_from_loader(
        "claude_snapshot",
        importlib.machinery.SourceFileLoader("claude_snapshot", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_resume():
    """Load the extensionless recovery command without running main()."""
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        "claude-resume")
    spec = importlib.util.spec_from_loader(
        "claude_resume_test",
        importlib.machinery.SourceFileLoader("claude_resume_test", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_schedule():
    """Load the extensionless snooze command without running main()."""
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        "claude-schedule")
    spec = importlib.util.spec_from_loader(
        "claude_schedule_test",
        importlib.machinery.SourceFileLoader("claude_schedule_test", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestStateReap(unittest.TestCase):
    """~/.claude/state holds one <session_id>.json per session — and other
    tools' runtime files. The reap deleted claude-acct's usage cache on every
    60s tick (2026-08-19) because it matched *.json, which quietly disabled
    that cache and its anti-herd bump."""

    def test_reaps_dead_sessions_but_spares_other_tools(self):
        snap = _load_snapshot()
        live = "11111111-1111-1111-1111-111111111111"
        dead = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            os.mkdir(state)
            for name in (live + ".json", dead + ".json",
                         "acct-usage.json", "sessions-snapshot.json"):
                with open(os.path.join(state, name), "w") as f:
                    f.write("{}")
            real = snap.CLAUDE_DIR
            snap.CLAUDE_DIR = tmp
            try:
                snap._reap_dead_state({live})
            finally:
                snap.CLAUDE_DIR = real
            exists = lambda n: os.path.exists(os.path.join(state, n))
            self.assertTrue(exists(live + ".json"))
            self.assertFalse(exists(dead + ".json"))
            self.assertTrue(exists("acct-usage.json"))
            self.assertTrue(exists("sessions-snapshot.json"))


class TestVendorState(unittest.TestCase):
    """Lifecycle events outrank transcript mtime for vendors that have hooks."""

    SID = "01a03c16-29cc-7e90-b489-68dbe15bde29"

    def setUp(self):
        self.snap = _load_snapshot()
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "state"))
        self.real_dir = self.snap.CLAUDE_DIR
        self.real_pane = self.snap.proc_pane_id
        self.snap.CLAUDE_DIR = self.tmp.name
        self.snap.proc_pane_id = lambda _pid: "51"
        self.transcript = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(self.transcript, "w") as f:
            f.write("{}\n")
        os.utime(self.transcript, (100, 100))
        self.info = {
            self.SID: {
                "pid": 123,
                "cwd": "/work/project",
                "vendor": "codex",
                "transcript": self.transcript,
            }
        }

    def tearDown(self):
        self.snap.CLAUDE_DIR = self.real_dir
        self.snap.proc_pane_id = self.real_pane
        self.tmp.cleanup()

    @property
    def state_path(self):
        return os.path.join(self.tmp.name, "state", self.SID + ".json")

    def hooked_state(self):
        return {
            "session_id": self.SID,
            "status": "working",
            "since": 900,
            "updated": 900,
            "agents": 1,
            "vendor": "codex",
            "status_source": "hook",
            "wezterm_pane": "51",
        }

    def test_hook_state_survives_a_quiet_rollout(self):
        hooked = self.hooked_state()
        with open(self.state_path, "w") as f:
            json.dump(hooked, f)

        self.snap._publish_vendor_state(self.info, now=1000)

        with open(self.state_path) as f:
            self.assertEqual(json.load(f), hooked)

    def test_hook_write_wins_a_snapshot_race(self):
        """The provenance read and fallback write share the hook's lock."""
        lock_path = os.path.join(self.tmp.name, "state", ".lock")
        started = threading.Event()
        errors = []

        def publish():
            started.set()
            try:
                self.snap._publish_vendor_state(self.info, now=1000)
            except BaseException as exc:  # preserve worker failures for the assertion
                errors.append(exc)

        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            worker = threading.Thread(target=publish)
            worker.start()
            self.assertTrue(started.wait(1))
            worker.join(0.05)
            self.assertTrue(worker.is_alive(), "snapshot did not wait for state lock")
            with open(self.state_path, "w") as f:
                json.dump(self.hooked_state(), f)
            fcntl.flock(lock, fcntl.LOCK_UN)

        worker.join(1)
        self.assertFalse(worker.is_alive(), "snapshot stayed blocked after unlock")
        self.assertEqual(errors, [])
        with open(self.state_path) as f:
            self.assertEqual(json.load(f), self.hooked_state())

    def test_unhooked_vendor_keeps_the_silence_fallback(self):
        self.snap._publish_vendor_state(self.info, now=1000)

        with open(self.state_path) as f:
            state = json.load(f)
        self.assertEqual(state["status"], "waiting")
        self.assertEqual(state["since"], 100)
        self.assertEqual(state["vendor"], "codex")
        self.assertNotIn("status_source", state)



class TestVendorAdapters(unittest.TestCase):
    """Adapters must return the record shape every view already speaks. These
    read the real on-disk stores rather than fixtures, for the same reason the
    transcript-dir tests do: the point is agreeing with what the vendors
    actually write."""

    def test_grok_resolves_a_session_from_its_cwd(self):
        # Grok encodes the cwd into the directory name, so the session for a
        # directory is findable without reading a single transcript byte.
        import os as _os
        root = L.GROK_SESSIONS_DIR
        if not _os.path.isdir(root):
            self.skipTest("no grok sessions on this machine")
        encoded = next((d for d in _os.listdir(root)
                        if d.startswith("%2F")), None)
        if not encoded:
            self.skipTest("no grok session directories")
        from urllib.parse import unquote
        cwd = unquote(encoded)
        rec = L._grok_session_for_cwd(cwd)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["cwd"], cwd)
        self.assertTrue(rec["transcript"].endswith("chat_history.jsonl"))
        self.assertIn(rec["sessionId"], rec["transcript"])

    def test_grok_unknown_cwd_is_none(self):
        self.assertIsNone(L._grok_session_for_cwd("/nonexistent/project/xyz"))

    def test_adapters_agree_on_the_record_shape(self):
        required = {"sessionId", "cwd", "pid", "status", "source", "vendor"}
        for name, adapter in L.VENDOR_ADAPTERS.items():
            for sid, rec in adapter().items():
                self.assertTrue(required.issubset(rec),
                                "%s record missing %s"
                                % (name, required - set(rec)))
                self.assertEqual(rec["vendor"], name)
                self.assertEqual(rec["sessionId"], sid)

    def test_one_broken_adapter_cannot_blank_the_picker(self):
        real = dict(L.VENDOR_ADAPTERS)
        def boom():
            raise RuntimeError("vendor exploded")
        L.VENDOR_ADAPTERS["boom"] = boom
        try:
            L.other_vendor_sessions()      # must not raise
        finally:
            L.VENDOR_ADAPTERS.clear()
            L.VENDOR_ADAPTERS.update(real)


class TestCodexAttribution(unittest.TestCase):
    """Which Codex conversation is in which tab.

    Attribution used to be "the newest rollout whose session_meta names this
    cwd", which is not an identity: two Codex tabs open on one project
    collapsed into a single session, and the one that survived was handed the
    other tab's transcript. Everything downstream reads that — so the tab
    colour and CTRL+SHIFT+A were reporting the wrong tab's status, and the
    second tab, having no state file at all, was permanently "idle" and the
    attend key kept landing on it mid-task (2026-08-22)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = {n: getattr(L, n) for n in
                      ("_pids", "_proc_argv", "_proc_cwd", "proc_pane_id",
                       "_open_rollouts")}
        self.procs = {}          # pid -> (cwd, pane, [rollout path, ...])
        L._pids = lambda: list(self.procs)
        L._proc_argv = lambda pid: ["node", "/usr/bin/codex"]
        L._proc_cwd = lambda pid: self.procs[pid][0]
        L.proc_pane_id = lambda pid: self.procs[pid][1]
        L._open_rollouts = lambda pid: [
            (os.path.getmtime(r), r) for r in self.procs[pid][2]]

    def tearDown(self):
        import shutil
        for n, f in self.saved.items():
            setattr(L, n, f)
        shutil.rmtree(self.dir, ignore_errors=True)

    def rollout(self, sid, cwd, parent=None, mtime=None):
        """A rollout file on disk, real enough for _codex_meta to read."""
        import json as _json
        path = os.path.join(
            self.dir, "rollout-2026-08-22T00-00-00-%s.jsonl" % sid)
        payload = {"id": sid, "session_id": parent or sid, "cwd": cwd}
        if parent:
            payload.update({"parent_thread_id": parent,
                            "thread_source": "subagent"})
        with open(path, "w") as f:
            f.write(_json.dumps({"type": "session_meta",
                                 "payload": payload}) + "\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    ID_A = "01a02922-fd73-73e2-a2de-f522db382782"
    ID_B = "01a025ab-47c3-7f21-9e0e-6d185bc57689"
    ID_SUB = "01a02929-0e0f-7951-a8eb-4539d3bc06dd"

    def test_two_tabs_in_one_directory_are_two_sessions(self):
        # The regression: same cwd, different panes.
        cwd = "/home/you/projects/thing"
        self.procs = {
            100: (cwd, "5", [self.rollout(self.ID_A, cwd)]),
            200: (cwd, "43", [self.rollout(self.ID_B, cwd)]),
        }
        got = L.codex_sessions()
        self.assertEqual(sorted(got), sorted([self.ID_A, self.ID_B]))
        self.assertEqual(got[self.ID_A]["pid"], 100)
        self.assertEqual(got[self.ID_B]["pid"], 200)

    def test_each_tab_gets_its_own_transcript(self):
        # Status is derived from how long the transcript has been silent, so a
        # transcript belonging to the other tab is worse than none at all.
        cwd = "/home/you/projects/thing"
        a = self.rollout(self.ID_A, cwd)
        b = self.rollout(self.ID_B, cwd)
        self.procs = {100: (cwd, "5", [a]), 200: (cwd, "43", [b])}
        got = L.codex_sessions()
        self.assertEqual(got[self.ID_A]["transcript"], a)
        self.assertEqual(got[self.ID_B]["transcript"], b)

    def test_a_spawned_subagent_is_not_the_session(self):
        # A working session holds its subagents' rollouts open and all of them
        # are being appended to, so the newest is whichever wrote last — the
        # session id would flap every tick. Nothing spawned the conversation.
        cwd = "/home/you/projects/thing"
        own = self.rollout(self.ID_A, cwd, mtime=1000)
        sub = self.rollout(self.ID_SUB, cwd, parent=self.ID_A, mtime=9000)
        self.procs = {100: (cwd, "5", [own, sub])}
        got = L.codex_sessions()
        self.assertEqual(list(got), [self.ID_A])
        self.assertEqual(got[self.ID_A]["transcript"], own)

    def test_the_tui_and_its_vendored_child_are_one_session(self):
        # `node .../bin/codex` execs a vendored binary that holds the fds; both
        # match the process filter and both sit in the same pane.
        cwd = "/home/you/projects/thing"
        own = self.rollout(self.ID_A, cwd)
        self.procs = {100: (cwd, "5", []), 101: (cwd, "5", [own])}
        got = L.codex_sessions()
        self.assertEqual(list(got), [self.ID_A])
        self.assertEqual(got[self.ID_A]["pid"], 100)   # the TUI, not the child


class TestLiveSessionIds(unittest.TestCase):
    """Liveness must survive a caller whose PID namespace hides the fleet."""

    def test_caller_ids_are_validated(self):
        got = L.caller_session_ids({
            "CLAUDE_CODE_SESSION_ID": "claude-live",
            "CODEX_SESSION_ID": "bad id; ignored",
        })
        self.assertEqual(got, {"claude-live"})

    def test_only_a_held_lease_is_live(self):
        with tempfile.TemporaryDirectory() as leases:
            path = os.path.join(leases, "leased-session.lock")
            with open(path, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertEqual(L.leased_session_ids(leases),
                                 {"leased-session"})
                fcntl.flock(lock, fcntl.LOCK_UN)
            self.assertEqual(L.leased_session_ids(leases), set(),
                             "an unlocked stale file is not liveness")
            os.symlink(path, os.path.join(leases, "symlink-session.lock"))
            os.mkfifo(os.path.join(leases, "fifo-session.lock"))
            self.assertEqual(L.leased_session_ids(leases), set(),
                             "links and non-regular files are not leases")

    def test_caller_and_leases_supplement_hidden_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "rollout.jsonl")
            open(transcript, "w").close()
            leases = os.path.join(tmp, "leases")
            os.mkdir(leases, 0o700)
            lease_path = os.path.join(leases, "leased-session.lock")
            snap = self._snapshot([
                {"sessionId": sid, "cwd": tmp, "vendor": "codex",
                 "transcript": transcript}
                for sid in ("caller-session", "leased-session", "dead-session")
            ])
            with open(lease_path, "w") as lock, \
                    mock.patch.object(L, "LIVE_LEASE_DIR", leases), \
                    mock.patch.object(L, "RECOVERY_SNAP",
                                      os.path.join(tmp, "no-recovery.json")), \
                    mock.patch.object(L, "live_sessions", return_value={}), \
                    mock.patch.object(L, "other_vendor_sessions", return_value={}), \
                    mock.patch.dict(os.environ,
                                    {"CODEX_SESSION_ID": "caller-session",
                                     "CLAUDE_CODE_SESSION_ID": ""}):
                fcntl.flock(lock, fcntl.LOCK_EX)
                got = L.resume_set(snap_path=snap)
            self.assertEqual([entry["sessionId"] for entry in got],
                             ["dead-session"])

    def _snapshot(self, sessions):
        import json as _json, tempfile, time as _time
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        _json.dump({"capturedAt": int(_time.time()), "sessions": sessions}, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name


class TestRegistryProcessIdentity(unittest.TestCase):
    """A stale Claude registry file cannot bless a reused host PID."""

    SID = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = self.tmp.name
        with open(os.path.join(self.registry, "123.json"), "w") as stream:
            json.dump({"sessionId": self.SID, "pid": 123,
                       "procStart": "900", "cwd": self.tmp.name,
                       "kind": "interactive"}, stream)

    def _read(self, argv, start):
        with mock.patch.object(L, "REGISTRY_DIR", self.registry), \
                mock.patch.object(L, "_proc_argv", return_value=argv), \
                mock.patch.object(L, "_proc_start_ticks", return_value=start):
            return L._from_registry()

    def test_same_claude_process_generation_is_live(self):
        self.assertEqual(set(self._read(["/usr/bin/claude"], 900)), {self.SID})

    def test_reused_pid_is_not_live(self):
        self.assertEqual(self._read(["/usr/bin/claude"], 901), {})

    def test_unrelated_process_at_same_pid_is_not_live(self):
        self.assertEqual(self._read(["/usr/bin/python3"], 900), {})


class TestSandboxedResume(unittest.TestCase):
    """A restricted caller must use mux/hook liveness or fail closed."""

    def setUp(self):
        self.resume = _load_resume()

    def test_wezterm_caller_always_unions_mux_liveness(self):
        with mock.patch.dict(os.environ, {"WEZTERM_PANE": "7"}), \
                mock.patch.object(self.resume, "process_live_session_ids",
                                  return_value={"process-session"}), \
                mock.patch.object(self.resume, "leased_session_ids",
                                  return_value={"leased-session"}), \
                mock.patch.object(self.resume, "caller_session_ids",
                                  return_value={"caller-session"}), \
                mock.patch.object(self.resume, "_mux_live_session_ids",
                                  return_value={"caller-session",
                                                "ordinary-sibling"}):
            got = self.resume._effective_live_session_ids()
        self.assertEqual(got, {"process-session", "leased-session",
                               "caller-session", "ordinary-sibling"})

    def test_wezterm_caller_fails_closed_without_valid_mux_state(self):
        with mock.patch.dict(os.environ, {"WEZTERM_PANE": "7"}), \
                mock.patch.object(self.resume, "process_live_session_ids",
                                  return_value=set()), \
                mock.patch.object(self.resume, "leased_session_ids",
                                  return_value=set()), \
                mock.patch.object(self.resume, "caller_session_ids",
                                  return_value={"caller-session"}), \
                mock.patch.object(self.resume, "_mux_live_session_ids",
                                  return_value=None):
            with self.assertRaises(SystemExit):
                self.resume._effective_live_session_ids()

    def test_mux_join_unions_pane_identity_with_newest_hook_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            os.mkdir(state)
            records = (("stale-collision", 7, 99),
                       ("legacy-old", 9, 1),
                       ("legacy-new", 9, 2),
                       ("closed-session", 8, 3))
            for sid, pane, updated in records:
                with open(os.path.join(state, sid + ".json"), "w") as stream:
                    json.dump({"session_id": sid, "wezterm_pane": pane,
                               "cwd": tmp, "updated": updated}, stream)
            cache = os.path.join(tmp, "live.json")
            with open(cache, "w") as stream:
                json.dump({"schema": 1, "updated": time.time(),
                           "sessions": {"caller-session": "7"}}, stream)
            response = mock.Mock(returncode=0,
                                 stdout=json.dumps([
                                     {"pane_id": 7, "cwd": "file://" + tmp},
                                     {"pane_id": 9, "cwd": "file://" + tmp},
                                 ]))
            with mock.patch.dict(os.environ, {"WEZTERM_PANE": "7"}), \
                    mock.patch.object(self.resume, "CLAUDE_DIR", tmp), \
                    mock.patch.object(self.resume, "LIVE_SESSION_CACHE", cache), \
                    mock.patch.object(self.resume.shutil, "which",
                                      return_value="/usr/bin/wezterm"), \
                    mock.patch.object(self.resume.subprocess, "run",
                                      return_value=response):
                got = self.resume._mux_live_session_ids()
            self.assertEqual(got, {"caller-session", "stale-collision",
                                   "legacy-new"})

    def test_mux_join_rejects_a_response_missing_the_callers_pane(self):
        response = mock.Mock(returncode=0,
                             stdout=json.dumps([{"pane_id": 8}]))
        with mock.patch.dict(os.environ, {"WEZTERM_PANE": "7"}), \
                mock.patch.object(self.resume.shutil, "which",
                                  return_value="/usr/bin/wezterm"), \
                mock.patch.object(self.resume.subprocess, "run",
                                  return_value=response):
            self.assertIsNone(self.resume._mux_live_session_ids())

    def test_mux_join_rejects_a_stale_identity_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "live.json")
            with open(cache, "w") as stream:
                json.dump({"schema": 1, "updated": time.time() - 60,
                           "sessions": {"caller-session": "7"}}, stream)
            response = mock.Mock(returncode=0,
                                 stdout=json.dumps([{"pane_id": 7}]))
            with mock.patch.dict(os.environ, {"WEZTERM_PANE": "7"}), \
                    mock.patch.object(self.resume, "LIVE_SESSION_CACHE", cache), \
                    mock.patch.object(self.resume.shutil, "which",
                                      return_value="/usr/bin/wezterm"), \
                    mock.patch.object(self.resume.subprocess, "run",
                                      return_value=response):
                self.assertIsNone(self.resume._mux_live_session_ids())


class TestScheduleState(unittest.TestCase):
    """Durable snooze state survives startup failures and concurrent edits."""

    def setUp(self):
        self.schedule = _load_schedule()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.schedule.CLAUDE_DIR = self.tmp.name
        self.schedule.SCHEDULED = os.path.join(self.tmp.name, "scheduled.json")
        self.schedule.SCHEDULE_LOCK = os.path.join(self.tmp.name, "scheduled.lock")
        self.entry = {
            "session_id": "scheduled-session", "vendor": "codex",
            "cwd": self.tmp.name, "reopen_at": 1, "label": "scheduled",
            "created_at": 1,
        }

    def test_state_lock_serializes_read_modify_write(self):
        self.schedule._write([self.entry])
        entered = threading.Event()
        finished = threading.Event()

        def cancel():
            entered.set()
            self.schedule.cmd_cancel(SimpleNamespace(sid="scheduled-session"))
            finished.set()

        with self.schedule._schedule_lock():
            worker = threading.Thread(target=cancel)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.05),
                             "concurrent mutation bypassed the schedule lock")
        worker.join(1)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.schedule._read(), [])

    def test_unconfirmed_startup_preserves_schedule(self):
        self.schedule._write([self.entry])
        spawn = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(self.schedule, "_target_window", return_value=1), \
                mock.patch.object(self.schedule.shutil, "which",
                                  return_value="/usr/bin/wezterm"), \
                mock.patch.object(self.schedule.subprocess, "run",
                                  return_value=spawn), \
                mock.patch.object(self.schedule, "process_live_session_ids",
                                  return_value=set()), \
                mock.patch.object(self.schedule.time, "monotonic",
                                  side_effect=[0, 6]):
            started = self.schedule._reopen(SimpleNamespace(dry_run=False),
                                            force_sid="scheduled-session")
        self.assertFalse(started)
        self.assertEqual(self.schedule._read(), [self.entry])

    def test_mux_spawn_timeout_preserves_schedule(self):
        self.schedule._write([self.entry])
        with mock.patch.object(self.schedule, "_target_window", return_value=1), \
                mock.patch.object(self.schedule.shutil, "which",
                                  return_value="/usr/bin/wezterm"), \
                mock.patch.object(self.schedule.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(
                                      "wezterm", 3)), \
                mock.patch.object(self.schedule, "process_live_session_ids",
                                  return_value=set()):
            started = self.schedule._reopen(SimpleNamespace(dry_run=False),
                                            force_sid="scheduled-session")
        self.assertFalse(started)
        self.assertEqual(self.schedule._read(), [self.entry])

    def test_confirmed_startup_consumes_schedule(self):
        self.schedule._write([self.entry])
        spawn = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(self.schedule, "_target_window", return_value=1), \
                mock.patch.object(self.schedule.shutil, "which",
                                  return_value="/usr/bin/wezterm"), \
                mock.patch.object(self.schedule.subprocess, "run",
                                  return_value=spawn), \
                mock.patch.object(self.schedule, "process_live_session_ids",
                                  side_effect=[set(), {"scheduled-session"}]), \
                mock.patch.object(self.schedule, "_notify"):
            started = self.schedule._reopen(SimpleNamespace(dry_run=False),
                                            force_sid="scheduled-session")
        self.assertTrue(started)
        self.assertEqual(self.schedule._read(), [])


class TestVendorResume(unittest.TestCase):
    """A reboot has to bring the whole fleet back, not just the Claude half —
    and must never hand another vendor's id to `claude --resume`."""

    def test_each_vendor_has_its_own_resume_verb(self):
        self.assertEqual(L.resume_command("S", "claude"), "claude --resume S")
        self.assertEqual(L.resume_command("S", "codex"), "codex resume S")
        self.assertEqual(L.resume_command("S", "grok"), "grok --resume S")

    def test_unknown_vendor_has_no_command(self):
        # Skipping the tab beats spawning one that reports an unknown id.
        self.assertIsNone(L.resume_command("S", "venice"))

    def test_unsafe_session_id_has_no_command(self):
        for sid in ("-flag", "has space", "x; touch /tmp/pwned", "$(id)",
                    "x\nclaude"):
            with self.subTest(sid=sid):
                self.assertIsNone(L.resume_command(sid, "codex"))

    def test_missing_vendor_defaults_to_claude(self):
        self.assertEqual(L.resume_command("S"), "claude --resume S")
        self.assertEqual(L.resume_command("S", None), "claude --resume S")

    def _snapshot(self, sessions):
        import json as _json, tempfile, time as _time
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        _json.dump({"capturedAt": int(_time.time()), "sessions": sessions}, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_vendor_transcript_is_checked_where_it_actually_lives(self):
        # The claude check looks under ~/.claude/projects; a vendor transcript
        # is nowhere near it, so the recorded path is what gets verified.
        import tempfile
        cwd = os.path.expanduser("~")
        good = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        good.close()
        self.addCleanup(os.unlink, good.name)
        snap = self._snapshot([
            {"sessionId": "vendor-alive", "cwd": cwd, "vendor": "codex",
             "transcript": good.name},
            {"sessionId": "vendor-gone", "cwd": cwd, "vendor": "codex",
             "transcript": "/nonexistent/rollout.jsonl"},
        ])
        got = {e["sessionId"]: e for e in L.resume_set(snap_path=snap)}
        self.assertIn("vendor-alive", got)
        self.assertEqual(got["vendor-alive"]["vendor"], "codex")
        self.assertNotIn("vendor-gone", got)

    def test_claude_entries_still_gated_on_the_projects_tree(self):
        snap = self._snapshot([
            {"sessionId": "claude-bogus", "cwd": os.path.expanduser("~")},
        ])
        self.assertEqual(L.resume_set(snap_path=snap), [])


class TestVendorTranscriptReader(unittest.TestCase):
    """The reader exists so a conversation can be read without the terminal
    mangling it; that is worth as much on a Codex or Grok tab."""

    @classmethod
    def setUpClass(cls):
        from importlib.machinery import SourceFileLoader
        cls.ct = SourceFileLoader(
            "ct", os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "claude-transcript")).load_module()

    def test_injected_preamble_is_not_a_prompt(self):
        clean = self.ct._clean_prompt
        for junk in ("# AGENTS.md instructions\n<INSTRUCTIONS>\nblah",
                     "<environment_context>x</environment_context>",
                     "<user_info>OS: linux</user_info>", "   ", ""):
            self.assertEqual(clean(junk), "", junk[:30])

    def test_grok_prompt_is_unwrapped(self):
        # Grok wraps what the human typed in <user_query>.
        got = self.ct._clean_prompt("<user_query>\nship it\n</user_query>")
        self.assertEqual(got, "ship it")

    def test_system_reminders_are_stripped(self):
        got = self.ct._clean_prompt(
            "real question<system-reminder>noise</system-reminder>")
        self.assertEqual(got, "real question")

    def test_renderer_is_chosen_by_where_the_file_lives(self):
        import glob as _glob
        for root, fn in ((L.GROK_SESSIONS_DIR, "render_grok"),
                         (L.CODEX_SESSIONS_DIR, "render_codex")):
            if not os.path.isdir(root):
                continue
            hits = _glob.glob(os.path.join(root, "**", "*.jsonl"),
                              recursive=True)
            if not hits:
                continue
            path = max(hits, key=os.path.getmtime)
            text = self.ct.render(path, color=False)
            self.assertIn(os.path.basename(root.rstrip("/")) and "session",
                          text.split("\n")[0])
            self.assertNotIn("<INSTRUCTIONS>", text)

class TestSessionTopic(unittest.TestCase):
    """The picker's topic column must honor /rename: the custom-title event
    beats the generated ai-title, which Claude Code KEEPS re-emitting after a
    rename (2026-08-24: rename showed no effect in the picker because the
    scan returned the first ai-title and never read custom-title at all)."""

    @classmethod
    def setUpClass(cls):
        from importlib.machinery import SourceFileLoader
        cls.cs = SourceFileLoader(
            "cs", os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "claude-sessions")).load_module()

    def _write(self, events, pad_bytes=0):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, f.name)
        if pad_bytes:
            filler = {"type": "assistant", "message": {"content": "x" * 512}}
            line = json.dumps(filler) + "\n"
            for _ in range(pad_bytes // len(line) + 1):
                f.write(line)
        for ev in events:
            f.write(json.dumps(ev) + "\n")
        f.close()
        return f.name

    def test_rename_beats_a_later_ai_title(self):
        # Observed transcript shape: custom-title and the stale ai-title are
        # re-emitted as adjacent pairs, ai-title last.
        path = self._write([
            {"type": "ai-title", "aiTitle": "Generated title"},
            {"type": "custom-title", "customTitle": "my-name"},
            {"type": "ai-title", "aiTitle": "Generated title"},
        ])
        self.assertEqual(self.cs._current_titles(path),
                         ("my-name", "Generated title"))

    def test_newest_rename_wins(self):
        path = self._write([
            {"type": "custom-title", "customTitle": "first"},
            {"type": "custom-title", "customTitle": "second"},
        ])
        self.assertEqual(self.cs._current_titles(path)[0], "second")

    def test_titles_are_read_from_the_tail_of_a_big_file(self):
        path = self._write([{"type": "custom-title", "customTitle": "deep"}],
                           pad_bytes=self.cs._TITLE_TAIL_BYTES + 4096)
        self.assertEqual(self.cs._current_titles(path), ("deep", None))

    def test_a_title_beyond_the_tail_reads_as_none(self):
        # Claude re-emits titles every few turns, so "nothing in the tail"
        # means "no title" — the fallback (founding input) takes over.
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, f.name)
        f.write('{"type": "ai-title", "aiTitle": "buried"}\n')
        line = '{"type": "assistant", "message": {"content": "%s"}}\n' % ("x" * 512)
        for _ in range(self.cs._TITLE_TAIL_BYTES // len(line) + 2):
            f.write(line)
        f.close()
        self.assertEqual(self.cs._current_titles(f.name), (None, None))

    def test_harness_commands_are_not_a_founding_input(self):
        # "/model fable" identifies nothing; the real prompt in the next
        # event is the session's identity.
        ev = {"type": "user", "message": {"role": "user", "content":
              "<command-name>/model</command-name><command-args>fable</command-args>"}}
        self.assertIsNone(self.cs._founding_input(ev))

    def test_substantive_commands_still_count(self):
        ev = {"type": "user", "message": {"role": "user", "content":
              "<command-name>/plan</command-name><command-args>migrate auth</command-args>"}}
        self.assertEqual(self.cs._founding_input(ev), "/plan migrate auth")

    def test_image_paste_caveat_does_not_hide_the_prompt(self):
        # An image paste puts a caveat text block ahead of the typed text;
        # only reading the first block made such sessions topicless.
        ev = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "Caveat: the messages below were pasted"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "fix the login flow"},
        ]}}
        self.assertEqual(self.cs._founding_input(ev), "fix the login flow")

    def test_clip_cuts_at_a_word_with_ellipsis(self):
        s = "one two three four five six seven eight nine ten eleven twelve"
        out = self.cs._clip(s, 44)
        self.assertTrue(out.endswith("…"))
        body = out[:-1]
        self.assertTrue(body and s.startswith(body))
        self.assertFalse(body.endswith(" "))
        self.assertEqual(self.cs._clip("short", 44), "short")


if __name__ == "__main__":
    unittest.main(verbosity=2)
