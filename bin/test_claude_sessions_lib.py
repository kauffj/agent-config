#!/usr/bin/env python3
"""Regression tests for _claude_sessions_lib.

    python3 ~/.claude/bin/test_claude_sessions_lib.py

Stdlib only, no fixtures — the interesting cases are about agreeing with how
Claude actually names transcript directories on this disk, so the end-to-end
test reads the real ones rather than inventing them.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

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

if __name__ == "__main__":
    unittest.main(verbosity=2)
