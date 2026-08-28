#!/usr/bin/env python3
"""Regression tests for bin/project-instruction-migrate."""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "project-instruction-migrate"
TEST_TMP = ROOT / ".workspaces" / "tmp" / "tests"


def sha(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def load_script():
    loader = importlib.machinery.SourceFileLoader("project_instruction_migrate_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ProjectInstructionMigrateTest(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True, mode=0o700)
        TEST_TMP.chmod(0o700)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.projects = self.home / "projects"
        self.state = self.base / "state"
        self.payloads = self.base / "payloads"
        self.projects.mkdir(parents=True)
        self.payloads.mkdir()

    def git_project(self, name="project"):
        root = self.projects / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        return root

    def commit_all(self, root):
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)

    def manifest(self, root, *, kind="git", deletions=None, agents_before=None,
                 old="# Old\n", new_agents="# Shared\n", new_claude="@AGENTS.md\n"):
        (root / "CLAUDE.md").write_text(old)
        if agents_before is not None:
            (root / "AGENTS.md").write_text(agents_before)
        agents_payload = self.payloads / f"{root.name}-AGENTS.md"
        claude_payload = self.payloads / f"{root.name}-CLAUDE.md"
        agents_payload.write_text(new_agents)
        claude_payload.write_text(new_claude)
        data = {
            "schemaVersion": 1,
            "id": "fixture",
            "targets": [{
                "id": root.name,
                "root": root.name,
                "kind": kind,
                "claudeBefore": {"kind": "file", "sha256": sha(old)},
                "agentsBefore": ({"kind": "absent"} if agents_before is None else
                                  {"kind": "file", "sha256": sha(agents_before)}),
                "claudeAfter": {"payload": claude_payload.name, "sha256": sha(new_claude)},
                "agentsAfter": {"payload": agents_payload.name, "sha256": sha(new_agents)},
                "deletions": deletions or [],
            }],
        }
        manifest = self.payloads / "manifest.json"
        manifest.write_text(json.dumps(data))
        return manifest

    def env(self):
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.state),
            "GIT_CEILING_DIRECTORIES": str(self.projects),
        })
        return env

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args), "--projects-root", str(self.projects)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())

    def test_report_apply_idempotence_and_restore(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        ready = self.run_cli("report", manifest, "--json")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertEqual(json.loads(ready.stdout)["targets"][0]["status"], "ready")

        applied = self.run_cli("apply", manifest)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((root / "AGENTS.md").read_text(), "# Shared\n")
        self.assertEqual((root / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        bundle = Path(applied.stdout.split("backup: ", 1)[1].splitlines()[0])
        again = self.run_cli("apply", manifest)
        self.assertIn("already applied", again.stdout)
        reported = self.run_cli("report", manifest, "--json")
        self.assertEqual(reported.returncode, 0, reported.stderr)
        self.assertEqual(json.loads(reported.stdout)["targets"][0]["status"], "applied")

        restored = subprocess.run(
            [sys.executable, str(SCRIPT), "restore", str(bundle)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((root / "CLAUDE.md").read_text(), "# Old\n")
        self.assertFalse((root / "AGENTS.md").exists())
        second = subprocess.run(
            [sys.executable, str(SCRIPT), "restore", str(bundle)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.assertIn("already restored", second.stdout)

    def test_unrelated_dirty_file_is_preserved(self):
        root = self.git_project()
        manifest = self.manifest(root)
        (root / "other.txt").write_text("tracked\n")
        self.commit_all(root)
        (root / "other.txt").write_text("user work\n")
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "other.txt").read_text(), "user work\n")

    def test_dirty_or_staged_planned_path_is_refused(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        (root / "CLAUDE.md").write_text("changed\n")
        before = (root / "CLAUDE.md").read_bytes()
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((root / "CLAUDE.md").read_bytes(), before)
        self.assertFalse((root / "AGENTS.md").exists())

    def test_exact_symlink_deletion_is_reversible(self):
        root = self.git_project()
        target = root / "templates" / "command.md"
        target.parent.mkdir()
        target.write_text("command\n")
        link = root / ".claude" / "commands" / "pop.md"
        link.parent.mkdir(parents=True)
        link.symlink_to("../../templates/command.md")
        deletions = [{
            "path": ".claude/commands/pop.md",
            "before": {"kind": "symlink", "rawTarget": "../../templates/command.md"},
        }]
        manifest = self.manifest(root, deletions=deletions)
        self.commit_all(root)
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())
        bundle = Path(result.stdout.split("backup: ", 1)[1].splitlines()[0])
        restored = subprocess.run(
            [sys.executable, str(SCRIPT), "restore", str(bundle)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(os.readlink(link), "../../templates/command.md")

    def test_non_git_umbrella_with_nested_repo(self):
        root = self.projects / "umbrella"
        root.mkdir()
        nested = root / "nested"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        manifest = self.manifest(root, kind="directory")
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_directory_target_inside_git_checkout_is_refused(self):
        parent = self.git_project("parent")
        root = parent / "nested"
        root.mkdir()
        manifest = self.manifest(root, kind="directory")
        raw = json.loads(manifest.read_text())
        raw["targets"][0]["root"] = "parent/nested"
        manifest.write_text(json.dumps(raw))
        self.commit_all(parent)
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 2)
        self.assertIn("inside a Git checkout", result.stderr)

    def test_hash_drift_and_payload_drift_are_refused(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        (root / "CLAUDE.md").write_text("drift\n")
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 2)
        (root / "CLAUDE.md").write_text("# Old\n")
        (self.payloads / "project-AGENTS.md").write_text("payload drift\n")
        result = self.run_cli("report", manifest)
        self.assertEqual(result.returncode, 2)

    def test_path_traversal_unknown_keys_and_symlink_parent_are_refused(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        raw = json.loads(manifest.read_text())
        raw["targets"][0]["root"] = "../escape"
        manifest.write_text(json.dumps(raw))
        self.assertEqual(self.run_cli("report", manifest).returncode, 2)

        manifest = self.manifest(root)
        raw = json.loads(manifest.read_text())
        raw["targets"][0]["surprise"] = True
        manifest.write_text(json.dumps(raw))
        self.assertEqual(self.run_cli("report", manifest).returncode, 2)

        manifest = self.manifest(root, deletions=[{
            "path": "linked/command.md",
            "before": {"kind": "file", "sha256": sha("command")},
        }])
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "command.md").write_text("command")
        (root / "linked").symlink_to(outside)
        self.commit_all(root)
        self.assertEqual(self.run_cli("report", manifest).returncode, 2)

    def test_restore_refuses_post_apply_edits(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = Path(result.stdout.split("backup: ", 1)[1].splitlines()[0])
        (root / "AGENTS.md").write_text("later work\n")
        restored = subprocess.run(
            [sys.executable, str(SCRIPT), "restore", str(bundle)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.assertEqual(restored.returncode, 2)
        self.assertEqual((root / "AGENTS.md").read_text(), "later work\n")

    def test_restore_refuses_post_apply_mode_changes(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = Path(result.stdout.split("backup: ", 1)[1].splitlines()[0])
        (root / "AGENTS.md").chmod(0o600)
        restored = subprocess.run(
            [sys.executable, str(SCRIPT), "restore", str(bundle)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.assertEqual(restored.returncode, 2)
        self.assertIn("changed since migration", restored.stderr)

    def test_restore_refuses_a_bundle_outside_managed_state(self):
        (self.state / "agent-config" / "project-migrations").mkdir(
            parents=True, mode=0o700)
        outside = self.base / "outside-bundle"
        outside.mkdir(mode=0o700)
        (outside / "manifest.json").write_text('{"schemaVersion":1,"entries":[]}\n')
        restored = subprocess.run(
            [sys.executable, str(SCRIPT), "restore", str(outside)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.assertEqual(restored.returncode, 2)
        self.assertIn("outside the managed migration state", restored.stderr)

    def test_restore_refuses_a_nonprivate_bundle(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        result = self.run_cli("apply", manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = Path(result.stdout.split("backup: ", 1)[1].splitlines()[0])
        bundle.chmod(0o755)
        restored = subprocess.run(
            [sys.executable, str(SCRIPT), "restore", str(bundle)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        self.assertEqual(restored.returncode, 2)
        self.assertIn("must be private", restored.stderr)

    def test_apply_refuses_a_peer_writable_backup_state_ancestor(self):
        root = self.git_project()
        manifest = self.manifest(root)
        self.commit_all(root)
        unsafe = self.base / "unsafe-state-parent"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        env = self.env()
        env["XDG_STATE_HOME"] = str(unsafe / "state")
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "apply", str(manifest),
                 "--projects-root", str(self.projects)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        finally:
            unsafe.chmod(0o700)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe backup-state parent", result.stderr)
        self.assertEqual((root / "CLAUDE.md").read_text(), "# Old\n")
        self.assertFalse((root / "AGENTS.md").exists())

    def test_second_write_failure_rolls_back_first(self):
        module = load_script()
        first = self.base / "first"
        second = self.base / "second"
        first.write_text("first old")
        second.write_text("second old")
        operations = [
            {"path": first, "before": {"kind": "file", "sha256": sha("first old")},
             "after": {"kind": "file", "sha256": sha("first new"), "data": b"first new"}},
            {"path": second, "before": {"kind": "file", "sha256": sha("second old")},
             "after": {"kind": "file", "sha256": sha("second new"), "data": b"second new"}},
        ]
        target = {
            "id": "fixture", "kind": "directory", "root": self.base,
            "planned": [], "operations": operations,
        }
        real_install = module.install_state
        calls = 0

        def fail_second(path, desired, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected")
            return real_install(path, desired, **kwargs)

        with mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(module, "install_state", side_effect=fail_second):
            with self.assertRaises(module.Refusal):
                module.apply_transaction("fixture", [target])
        self.assertEqual(first.read_text(), "first old")
        self.assertEqual(second.read_text(), "second old")

    def test_post_replace_failure_rolls_back_the_current_write(self):
        module = load_script()
        first = self.base / "first-post-replace"
        second = self.base / "second-post-replace"
        first.write_text("first old")
        second.write_text("second old")
        operations = [
            {"path": first, "before": {"kind": "file", "sha256": sha("first old")},
             "after": {"kind": "file", "sha256": sha("first new"), "data": b"first new"}},
            {"path": second, "before": {"kind": "file", "sha256": sha("second old")},
             "after": {"kind": "file", "sha256": sha("second new"), "data": b"second new"}},
        ]
        target = {
            "id": "fixture", "kind": "directory", "root": self.base,
            "planned": [], "operations": operations,
        }
        real_install = module.install_state
        calls = 0

        def fail_after_second(path, desired, **kwargs):
            nonlocal calls
            calls += 1
            result = real_install(path, desired, **kwargs)
            if calls == 2:
                raise OSError("injected after replacement")
            return result

        with mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(module, "install_state", side_effect=fail_after_second):
            with self.assertRaises(module.Refusal):
                module.apply_transaction("fixture", [target])
        self.assertEqual(first.read_text(), "first old")
        self.assertEqual(second.read_text(), "second old")


if __name__ == "__main__":
    unittest.main()
