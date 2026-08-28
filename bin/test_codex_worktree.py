#!/usr/bin/env python3
"""Regression tests for the managed Codex linked-worktree launcher."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "bin" / "codex-worktree"
ACCESS_MODULE = ROOT / "bin" / "_codex_git_access.py"
TEST_TMP = ROOT / ".workspaces" / "tmp" / "tests"


def load_access():
    loader = importlib.machinery.SourceFileLoader("codex_git_access_fixture", str(ACCESS_MODULE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_wrapper():
    loader = importlib.machinery.SourceFileLoader("codex_worktree_fixture", str(WRAPPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def installed_codex():
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "codex"
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved != WRAPPER.resolve() and os.access(resolved, os.X_OK):
            return resolved
    return None


class CodexWorktreeTest(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True, mode=0o700)
        TEST_TMP.chmod(0o700)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.codex_home = self.home / ".codex"
        self.link_bin = self.home / ".local" / "bin"
        self.real_bin = self.base / "real-bin"
        self.codex_home.mkdir(parents=True)
        self.link_bin.mkdir(parents=True)
        self.real_bin.mkdir()
        (self.link_bin / "codex").symlink_to(WRAPPER)
        package = self.base / "package"
        package.mkdir()
        fake = package / "codex.js"
        fake.write_text(textwrap.dedent('''\
            #!/usr/bin/env node
            exec /usr/bin/python3 -E -c 'import json, os, sys; print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()})); raise SystemExit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))' "$@"
            '''))
        fake.chmod(0o755)
        (self.real_bin / "codex").symlink_to(fake)
        (self.real_bin / "node").symlink_to("/bin/sh")
        access = load_access()
        config = 'default_permissions = "git-workspace"\n\n' + access.PROFILE_BLOCK
        (self.codex_home / "config.toml").write_text(config)
        (self.codex_home / "config.toml").chmod(0o600)
        self.main = self.base / "main"
        self.main.mkdir()
        self.git("init", "-q", str(self.main), cwd=self.base)
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.main / "tracked").write_text("one\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "initial")
        self.linked = self.base / "linked"
        self.git("worktree", "add", "-q", "-b", "linked-test", str(self.linked))

    def git(self, *args, cwd=None):
        command = ["git"]
        if cwd is None:
            command.extend(["-C", str(self.main)])
        command.extend(map(str, args))
        return subprocess.run(
            command,
            cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def env(self, **updates):
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home), "CODEX_HOME": str(self.codex_home),
            "PATH": f"{self.link_bin}:{self.real_bin}:/usr/bin:/bin",
        })
        env.update(updates)
        return env

    def run_wrapper(self, *args, cwd=None, **env):
        return subprocess.run(
            [str(self.link_bin / "codex"), *map(str, args)],
            cwd=cwd or self.linked, env=self.env(**env), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def argv(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)["argv"]

    def test_linked_worktree_gets_narrow_runtime_profile(self):
        result = self.run_wrapper("resume", "session-id")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.argv(result)
        common = str((self.main / ".git").resolve())
        self.assertEqual(args[0:2], ["--add-dir", common])
        profile = args[3]
        self.assertIn('extends="no-git"', profile)
        self.assertIn('"."="read"', profile)
        self.assertIn('"objects"="write"', profile)
        self.assertIn('"worktrees/linked/config.worktree"="read"', profile)
        self.assertEqual(args[-2:], ["resume", "session-id"])

    def test_normal_checkout_passes_arguments_unchanged(self):
        result = self.run_wrapper("--version", cwd=self.main)
        self.assertEqual(self.argv(result), ["--version"])

    def test_offline_and_no_git_project_overrides(self):
        local = self.linked / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "git-workspace-offline"\n')
        offline = self.argv(self.run_wrapper("resume", "one"))
        self.assertIn('extends="no-git-offline"', offline[3])

        local.write_text('default_permissions = "no-git-offline"\n')
        disabled = self.argv(self.run_wrapper("resume", "two"))
        self.assertEqual(disabled, ["resume", "two"])

    def test_local_policy_cannot_widen_global_git_or_network_access(self):
        access = load_access()
        local = self.linked / ".codex" / "config.toml"
        local.parent.mkdir()

        (self.codex_home / "config.toml").write_text(
            'default_permissions = "no-git"\n\n' + access.PROFILE_BLOCK)
        local.write_text('default_permissions = "git-workspace"\n')
        git_result = self.run_wrapper("resume", "git")
        self.assertEqual(self.argv(git_result), ["resume", "git"])
        self.assertIn("cannot re-enable Git", git_result.stderr)

        (self.codex_home / "config.toml").write_text(
            'default_permissions = "git-workspace-offline"\n\n' + access.PROFILE_BLOCK)
        local.write_text('default_permissions = "git-workspace"\n')
        network_result = self.run_wrapper("resume", "network")
        self.assertEqual(self.argv(network_result), ["resume", "network"])
        self.assertIn("cannot re-enable network", network_result.stderr)

    def test_reserved_runtime_profile_collision_fails_closed(self):
        config = self.codex_home / "config.toml"
        config.write_text(config.read_text() + '\n[permissions.codex-git-linked-runtime]\n'
                          'description = "collision"\n')
        result = self.run_wrapper("resume", "one")
        self.assertEqual(self.argv(result), ["resume", "one"])
        self.assertIn("reserved runtime profile name", result.stderr)

    def test_project_path_codex_binary_is_never_executed(self):
        project_bin = self.linked / "bin"
        project_bin.mkdir()
        malicious = project_bin / "codex"
        marker = self.base / "project-codex-ran"
        malicious.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        malicious.chmod(0o755)
        path = f"{self.link_bin}:{project_bin}:{self.real_bin}:/usr/bin:/bin"
        result = self.run_wrapper("--version", PATH=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_project_path_codex_is_rejected_when_launched_from_subdirectory(self):
        project_bin = self.linked / "bin"
        nested = self.linked / "nested"
        project_bin.mkdir()
        nested.mkdir()
        malicious = project_bin / "codex"
        marker = self.base / "subdir-project-codex-ran"
        malicious.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        malicious.chmod(0o755)
        path = f"{self.link_bin}:{project_bin}:{self.real_bin}:/usr/bin:/bin"
        result = self.run_wrapper("--version", cwd=nested, PATH=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_python_interpreter_and_import_environment_are_not_project_controlled(self):
        hostile_bin = self.linked / "hostile-bin"
        hostile_modules = self.linked / "hostile-modules"
        hostile_bin.mkdir()
        hostile_modules.mkdir()
        interpreter_marker = self.base / "hostile-python-ran"
        node_marker = self.base / "hostile-node-ran"
        import_marker = self.base / "hostile-import-ran"
        node_import_marker = self.base / "hostile-node-import-ran"
        python = hostile_bin / "python3"
        python.write_text(f'#!/bin/sh\ntouch "{interpreter_marker}"\nexit 99\n')
        python.chmod(0o755)
        node = hostile_bin / "node"
        node.write_text(f'#!/bin/sh\ntouch "{node_marker}"\nexit 99\n')
        node.chmod(0o755)
        (hostile_modules / "json.py").write_text(
            f'from pathlib import Path\nPath({str(import_marker)!r}).touch()\n')
        node_module = hostile_modules / "inject.cjs"
        node_module.write_text(
            f'require("fs").writeFileSync({str(node_import_marker)!r}, "ran");\n')
        path = f"{self.link_bin}:{hostile_bin}:{self.real_bin}:/usr/bin:/bin"
        result = self.run_wrapper(
            "--version", PATH=path, PYTHONPATH=str(hostile_modules),
            NODE_PATH=str(hostile_modules), NODE_OPTIONS=f"--require={node_module}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(interpreter_marker.exists())
        self.assertFalse(node_marker.exists())
        self.assertFalse(import_marker.exists())
        self.assertFalse(node_import_marker.exists())

    def test_git_routing_environment_cannot_redirect_augmentation(self):
        foreign = self.base / "foreign"
        foreign.mkdir()
        self.git("init", "-q", str(foreign), cwd=self.base)
        result = self.run_wrapper(
            "resume", "one",
            GIT_DIR=str(foreign / ".git"),
            GIT_WORK_TREE=str(foreign),
            GIT_COMMON_DIR=str(foreign / ".git"),
            GIT_CEILING_DIRECTORIES=str(self.linked),
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="core.bare",
            GIT_CONFIG_VALUE_0="true",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.argv(result)
        self.assertEqual(args[0:2], ["--add-dir", str((self.main / ".git").resolve())])

    def test_explicit_policy_override_is_preserved(self):
        args = self.argv(self.run_wrapper("--sandbox", "read-only", "resume", "one"))
        self.assertEqual(args, ["--sandbox", "read-only", "resume", "one"])
        args = self.argv(self.run_wrapper("--profile=locked", "resume", "one"))
        self.assertEqual(args, ["--profile=locked", "resume", "one"])
        args = self.argv(self.run_wrapper("--sandbox=read-only", "resume", "one"))
        self.assertEqual(args, ["--sandbox=read-only", "resume", "one"])
        args = self.argv(self.run_wrapper("-c", 'default_permissions=":read-only"', "resume", "one"))
        self.assertEqual(args, ["-c", 'default_permissions=":read-only"', "resume", "one"])

    def test_inner_command_arguments_after_separator_are_not_codex_options(self):
        args = self.argv(self.run_wrapper(
            "sandbox", "--", "tool", "--profile=locked", "--sandbox=read-only",
            "-C", str(self.main)))
        self.assertEqual(args[0], "--add-dir")
        self.assertEqual(
            args[-7:],
            ["sandbox", "--", "tool", "--profile=locked", "--sandbox=read-only",
             "-C", str(self.main)])

    def test_attached_and_ignore_user_config_overrides_are_preserved(self):
        for flag in ("-preadonly", "-sread-only", "--ignore-user-config"):
            with self.subTest(flag=flag):
                args = self.argv(self.run_wrapper(flag, "resume", "one"))
                self.assertEqual(args, [flag, "resume", "one"])

    def test_attached_cd_is_used_for_detection(self):
        args = self.argv(self.run_wrapper(f"-C{self.linked}", "resume", "one", cwd=self.base))
        self.assertEqual(args[0], "--add-dir")
        self.assertEqual(args[-2:], ["resume", "one"])

    def test_cd_is_used_for_detection_and_arguments_survive(self):
        args = self.argv(self.run_wrapper("-C", self.linked, "resume", "one", cwd=self.base))
        self.assertEqual(args[-4:], ["-C", str(self.linked), "resume", "one"])
        self.assertEqual(args[0], "--add-dir")

    def test_malformed_pointer_fails_closed_but_still_launches(self):
        pointer = self.linked / ".git"
        pointer.write_text("not a gitdir\n")
        result = self.run_wrapper("resume", "one")
        self.assertEqual(self.argv(result), ["resume", "one"])
        self.assertIn("augmentation skipped", result.stderr)

    def test_submodule_shape_is_not_treated_as_a_worktree(self):
        sub = self.main / "sub"
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(self.main), "sub")
        result = self.run_wrapper("--version", cwd=sub)
        self.assertEqual(self.argv(result), ["--version"])
        self.assertIn("augmentation skipped", result.stderr)

    def test_real_binary_exit_status_is_propagated(self):
        result = self.run_wrapper("--version", cwd=self.main, FAKE_CODEX_EXIT="23")
        self.assertEqual(result.returncode, 23)

    @unittest.skipUnless(installed_codex(), "Codex CLI is not installed")
    def test_installed_codex_strictly_accepts_runtime_profile(self):
        wrapper = load_wrapper()
        context = {
            "common": (self.main / ".git").resolve(),
            "name": "linked",
        }
        extra = wrapper.runtime_profile(context, "git-workspace")
        env = self.env()
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        result = subprocess.run(
            [str(installed_codex()), "--strict-config", *extra, "doctor", "--json"],
            cwd=self.linked, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        report = json.loads(result.stdout)
        self.assertEqual(report["checks"]["config.load"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
