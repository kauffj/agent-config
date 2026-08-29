#!/usr/bin/env python3
"""Regression tests for bin/codex-git-access."""

import errno
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "codex-git-access"
MODULE = ROOT / "bin" / "_codex_git_access.py"
WRAPPER = ROOT / "bin" / "codex-worktree"


def installed_codex():
    """Find the real CLI without recursively invoking an installed launcher."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "codex"
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if (resolved != WRAPPER.resolve()
                and resolved.name != WRAPPER.name
                and os.access(resolved, os.X_OK)):
            return resolved
    return None


def global_config(*, network=True, slash_tmp=True, env_tmp=True):
    return textwrap.dedent(f'''\
        approval_policy = "never"
        sandbox_mode = "workspace-write"

        model = "gpt-5.6-sol"
        # This unrelated comment and every hook hash must survive.

        [sandbox_workspace_write]
        network_access = {str(network).lower()}
        exclude_slash_tmp = {str(slash_tmp).lower()}
        exclude_tmpdir_env_var = {str(env_tmp).lower()}

        [shell_environment_policy]
        set = {{ SAFE = "kept" }}

        [hooks.state]
        [hooks.state."fixture"]
        trusted_hash = "sha256:kept"
        ''')


def project_config(*, network=False):
    return textwrap.dedent(f'''\
        approval_policy = "never"
        sandbox_mode = "workspace-write"

        # Keep this project-specific policy and comment.
        [sandbox_workspace_write]
        network_access = {str(network).lower()}

        [tools]
        project_value = "kept"
        ''')


def load_script():
    loader = importlib.machinery.SourceFileLoader("codex_git_access_test", str(MODULE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CodexGitAccessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.codex_home = self.home / ".codex"
        self.state = self.base / "state"
        self.codex_home.mkdir(parents=True)
        self.config = self.codex_home / "config.toml"

    def write_global(self, text=None):
        self.config.write_text(text if text is not None else global_config())
        self.config.chmod(0o600)

    def env(self):
        result = os.environ.copy()
        result.update({
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "XDG_STATE_HOME": str(self.state),
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        return result

    def run_cli(self, *args, check=False):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env())
        if check and result.returncode:
            self.fail(f"command failed ({result.returncode}): {result.stderr}\n{result.stdout}")
        return result

    def git_repo(self, name="project"):
        root = self.base / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=self.env())
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"],
                       check=True, env=self.env())
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                       check=True, env=self.env())
        return root

    def backup_bundles(self):
        root = self.state / "agent-config" / "backups"
        return sorted(path for path in root.iterdir()) if root.exists() else []

    def test_enable_preserves_unrelated_text_mode_and_exact_backup(self):
        original = global_config()
        self.write_global(original)
        result = self.run_cli("enable", check=True)

        output = self.config.read_text()
        parsed = tomllib.loads(output)
        self.assertNotIn("sandbox_mode", parsed)
        self.assertNotIn("sandbox_workspace_write", parsed)
        self.assertEqual(parsed["default_permissions"], "git-workspace")
        self.assertEqual(set(parsed["permissions"]), {
            "no-git-offline", "no-git", "git-workspace-offline", "git-workspace",
        })
        self.assertEqual(parsed["permissions"]["no-git-offline"]["extends"], ":workspace")
        self.assertEqual(parsed["permissions"]["no-git-offline"]["filesystem"],
                         {":tmpdir": "read", ":slash_tmp": "read"})
        self.assertFalse(parsed["permissions"]["no-git-offline"]["network"]["enabled"])
        self.assertEqual(parsed["permissions"]["no-git"]["extends"], "no-git-offline")
        self.assertTrue(parsed["permissions"]["no-git"]["network"]["enabled"])
        self.assertEqual(
            parsed["permissions"]["git-workspace-offline"]["filesystem"]
                  [":workspace_roots"], {
                      ".git": "write",
                      ".git/config": "read",
                      ".git/config.worktree": "read",
                      ".git/hooks": "read",
                      ".git/modules": "read",
                      ".git/objects/info/alternates": "read",
                      ".git/objects/info/http-alternates": "read",
                      ".git/worktrees": "read",
                  })
        self.assertEqual(parsed["permissions"]["git-workspace"]["extends"],
                         "git-workspace-offline")
        self.assertTrue(parsed["permissions"]["git-workspace"]["network"]["enabled"])
        self.assertIn("# This unrelated comment and every hook hash must survive.", output)
        self.assertEqual(parsed["hooks"]["state"]["fixture"]["trusted_hash"],
                         "sha256:kept")
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

        bundles = self.backup_bundles()
        self.assertEqual(len(bundles), 1)
        manifest = json.loads((bundles[0] / "manifest.json").read_text())
        self.assertEqual(len(manifest), 1)
        self.assertEqual((bundles[0] / manifest[0]["backup"]).read_text(), original)
        self.assertEqual(stat.S_IMODE(bundles[0].stat().st_mode), 0o700)
        self.assertIn("restart Codex", result.stdout)

        before = output
        second = self.run_cli("enable", check=True)
        self.assertEqual(self.config.read_text(), before)
        self.assertEqual(self.backup_bundles(), bundles)
        self.assertIn("no changes", second.stdout)

    def test_offline_global_keeps_network_disabled(self):
        self.write_global(global_config(network=False))
        self.run_cli("enable", check=True)
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["default_permissions"], "git-workspace-offline")

    def test_enable_upgrades_the_original_owned_profile_shape(self):
        module = load_script()
        old_block = module.PROFILE_BLOCK.replace(
            '".git/config" = "read"\n'
            '".git/config.worktree" = "read"\n'
            '".git/hooks" = "read"\n'
            '".git/modules" = "read"\n'
            '".git/objects/info/alternates" = "read"\n'
            '".git/objects/info/http-alternates" = "read"\n'
            '".git/worktrees" = "read"\n', '')
        text = textwrap.dedent('''\
            approval_policy = "never"
            default_permissions = "git-workspace"

            [hooks.state]
            [hooks.state."fixture"]
            trusted_hash = "sha256:kept"

            ''') + old_block
        self.write_global(text)

        result = self.run_cli("enable", check=True)

        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["default_permissions"], "git-workspace")
        self.assertEqual(
            parsed["permissions"]["git-workspace-offline"]["filesystem"]
                  [":workspace_roots"][".git/hooks"], "read")
        self.assertEqual(parsed["hooks"]["state"]["fixture"]["trusted_hash"],
                         "sha256:kept")
        self.assertEqual(len(self.backup_bundles()), 1)
        self.assertIn("updated user config", result.stdout)

    def test_enable_adds_nested_git_persistence_guards_to_previous_profile(self):
        module = load_script()
        previous = module.PROFILE_BLOCK
        for line in (
                '".git/modules" = "read"\n',
                '".git/objects/info/alternates" = "read"\n',
                '".git/objects/info/http-alternates" = "read"\n',
                '".git/worktrees" = "read"\n'):
            previous = previous.replace(line, "")
        self.write_global('approval_policy = "never"\n'
                          'default_permissions = "git-workspace"\n\n' + previous)

        result = self.run_cli("enable", check=True)

        paths = tomllib.loads(self.config.read_text())["permissions"] \
            ["git-workspace-offline"]["filesystem"][":workspace_roots"]
        self.assertEqual(paths[".git/modules"], "read")
        self.assertEqual(paths[".git/objects/info/alternates"], "read")
        self.assertEqual(paths[".git/worktrees"], "read")
        self.assertEqual(len(self.backup_bundles()), 1)
        self.assertIn("updated user config", result.stdout)

    def test_migration_preserves_an_array_of_tables_after_the_legacy_table(self):
        text = global_config().replace(
            "\n[shell_environment_policy]",
            '\n[[trusted_commands]]\nname = "kept"\n\n[shell_environment_policy]')
        self.write_global(text)
        self.run_cli("enable", check=True)
        self.assertEqual(tomllib.loads(self.config.read_text())["trusted_commands"],
                         [{"name": "kept"}])

    @unittest.skipUnless(installed_codex(), "Codex CLI is not installed")
    def test_installed_codex_strictly_accepts_the_generated_profiles(self):
        self.write_global()
        self.run_cli("enable", check=True)
        result = subprocess.run(
            [str(installed_codex()), "--strict-config", "doctor", "--json"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env())
        report = json.loads(result.stdout)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(result.returncode, 1)
        failed = {key for key, value in report["checks"].items()
                  if value["status"] == "fail"}
        self.assertEqual(failed, {"auth.credentials"})
        self.assertEqual(report["checks"]["config.load"]["status"], "ok")
        self.assertEqual(report["checks"]["sandbox.helpers"]["status"], "ok")

    def test_enable_refuses_a_policy_it_cannot_preserve(self):
        for name, text in {
            "tmp": global_config(slash_tmp=False),
            "mode": global_config().replace('sandbox_mode = "workspace-write"',
                                             'sandbox_mode = "read-only"'),
            "unknown": global_config().replace(
                "exclude_tmpdir_env_var = true",
                "exclude_tmpdir_env_var = true\nunknown_key = true"),
            "malformed": "sandbox_mode = [\n",
            "conflict": global_config().replace(
                'sandbox_mode = "workspace-write"',
                'default_permissions = ":workspace"\nsandbox_mode = "workspace-write"'),
        }.items():
            with self.subTest(name=name):
                self.write_global(text)
                before = self.config.read_bytes()
                result = self.run_cli("enable")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.config.read_bytes(), before)
                self.assertEqual(self.backup_bundles(), [])

    def test_symlinked_user_config_is_refused_without_touching_target(self):
        target = self.base / "outside.toml"
        target.write_text(global_config())
        self.config.symlink_to(target)
        result = self.run_cli("enable")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(), global_config())
        self.assertEqual(self.backup_bundles(), [])

    def test_world_writable_config_and_parent_are_refused(self):
        self.write_global()
        self.config.chmod(0o602)
        result = self.run_cli("enable")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.backup_bundles(), [])

        self.config.chmod(0o600)
        self.codex_home.chmod(0o702)
        try:
            result = self.run_cli("enable")
        finally:
            self.codex_home.chmod(0o700)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.backup_bundles(), [])

    def test_config_read_has_a_hard_byte_cap(self):
        self.write_global("x" * 64)
        module = load_script()
        with (mock.patch.object(module, "CONFIG_MAX_BYTES", 16),
              self.assertRaisesRegex(module.Refusal, "exceeds the safety limit")):
            module.secure_read(self.config)

    def test_unconfirmable_group_and_extended_acl_fail_closed(self):
        module = load_script()
        module._PRIVATE_GROUPS.clear()
        with mock.patch.object(module.grp, "getgrgid", side_effect=KeyError):
            self.assertFalse(module.group_is_private(os.getgid()))

        path = self.base / "group-writable"
        path.write_text("fixture")
        path.chmod(0o660)
        module._PRIVATE_GROUPS[os.getgid()] = True
        with mock.patch.object(module.os, "getxattr", return_value=b"extended-acl"):
            with self.assertRaises(module.Refusal):
                module.secure_read(path)

    @unittest.skipUnless(shutil.which("setfacl"), "setfacl is not installed")
    def test_replacement_strips_a_parent_default_acl(self):
        self.write_global()
        project = self.git_repo()
        local = project / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text(project_config())
        local.chmod(0o664)
        acl = subprocess.run(
            ["setfacl", "-m", "d:u:65534:rwx", str(local.parent)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if acl.returncode:
            self.skipTest(f"default ACLs are unavailable: {acl.stderr.strip()}")

        self.run_cli("enable", project, check=True)
        try:
            value = os.getxattr(local, "system.posix_acl_access")
        except OSError as exc:
            self.assertIn(exc.errno, {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP})
        else:
            self.fail(f"replacement retained an extended access ACL: {value!r}")

    def test_named_project_migrates_legacy_offline_policy(self):
        self.write_global()
        project = self.git_repo()
        local = project / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text(project_config(network=False))

        self.run_cli("enable", project, check=True)
        parsed = tomllib.loads(local.read_text())
        self.assertEqual(parsed["default_permissions"], "git-workspace-offline")
        self.assertNotIn("sandbox_mode", parsed)
        self.assertNotIn("sandbox_workspace_write", parsed)
        self.assertEqual(parsed["tools"]["project_value"], "kept")
        self.assertIn("# Keep this project-specific policy and comment.", local.read_text())

    def test_opt_out_preserves_online_and_offline_network_choices(self):
        self.write_global()
        online = self.git_repo("online")
        self.run_cli("enable", check=True)
        self.run_cli("opt-out", online, check=True)
        online_data = tomllib.loads((online / ".codex" / "config.toml").read_text())
        self.assertEqual(online_data["default_permissions"], "no-git")
        checked = self.run_cli("check", online, check=True)
        self.assertIn("Git metadata is intentionally read-only", checked.stdout)

        offline = self.git_repo("offline")
        local = offline / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "git-workspace-offline"\n')
        self.run_cli("opt-out", offline, check=True)
        offline_data = tomllib.loads(local.read_text())
        self.assertEqual(offline_data["default_permissions"], "no-git-offline")

    def test_opt_out_refuses_an_unrelated_local_permission_policy(self):
        self.write_global()
        self.run_cli("enable", check=True)
        project = self.git_repo()
        local = project / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "security-audit"\n')
        before = local.read_bytes()
        result = self.run_cli("opt-out", project)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(local.read_bytes(), before)

    def test_check_refuses_project_redefinition_of_a_managed_profile(self):
        self.write_global()
        self.run_cli("enable", check=True)
        project = self.git_repo()
        local = project / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text(textwrap.dedent('''\
            default_permissions = "git-workspace"

            [permissions.git-workspace]
            extends = ":full-disk-access"
            '''))
        result = self.run_cli("check", project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("managed profiles belong only in the user config", result.stdout)

    def test_check_accepts_normal_checkout_and_requires_linked_launcher(self):
        self.write_global()
        root = self.git_repo("main")
        tracked = root / "tracked"
        tracked.write_text("one\n")
        subprocess.run(["git", "-C", str(root), "add", "tracked"],
                       check=True, env=self.env())
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"],
                       check=True, env=self.env())
        self.run_cli("enable", check=True)

        normal = self.run_cli("check", root)
        self.assertEqual(normal.returncode, 0)
        self.assertIn("Git metadata write probe passed", normal.stdout)

        linked = self.base / "linked"
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b",
                        "linked-test", str(linked)], check=True, env=self.env())
        result = self.run_cli("check", linked)
        self.assertEqual(result.returncode, 1)
        self.assertIn("managed ~/.local/bin/codex launcher is missing", result.stdout)

        launcher = self.home / ".local" / "bin" / "codex"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(ROOT / "bin" / "codex-worktree")
        configured = self.run_cli("check", linked)
        self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
        self.assertIn("linked-worktree Git metadata write probe passed", configured.stdout)

    def test_check_fails_when_git_metadata_is_not_writable(self):
        self.write_global()
        root = self.git_repo()
        self.run_cli("enable", check=True)
        git_dir = root / ".git"
        git_dir.chmod(0o500)
        try:
            result = self.run_cli("check", root)
        finally:
            git_dir.chmod(0o700)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Git directory write probe failed", result.stdout)

    def test_handled_multi_file_failure_rolls_back_prior_replacement(self):
        module = load_script()
        first_path = self.base / "first"
        second_path = self.base / "second"
        first_path.write_text("first-old")
        second_path.write_text("second-old")
        first = module.secure_read(first_path)
        second = module.secure_read(second_path)
        first.update({"new_text": "first-new", "label": "first", "parent_created": False})
        second.update({"new_text": "second-new", "label": "second", "parent_created": False})
        real_replace = module.atomic_replace
        calls = 0

        def fail_second(entry, text, mode):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected write failure")
            return real_replace(entry, text, mode)

        with mock.patch.dict(os.environ, {"HOME": str(self.home),
                                          "XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(module, "atomic_replace", side_effect=fail_second):
            with self.assertRaises(module.Refusal):
                module.apply_changes([first, second])
        self.assertEqual(first_path.read_text(), "first-old")
        self.assertEqual(second_path.read_text(), "second-old")

    def test_in_place_edit_after_preflight_is_not_overwritten(self):
        module = load_script()
        path = self.base / "config"
        path.write_text("original")
        source = module.secure_read(path)
        entry = module.change(source, "migrated", "fixture")
        path.write_text("concurrent")
        with self.assertRaises(module.Refusal):
            module.atomic_replace(entry, entry["new_text"], entry["mode"])
        self.assertEqual(path.read_text(), "concurrent")

    def test_parent_substitution_after_preflight_is_not_overwritten(self):
        module = load_script()
        parent = self.base / "parent"
        parent.mkdir()
        path = parent / "config"
        path.write_text("original")
        entry = module.change(module.secure_read(path), "migrated", "fixture")
        moved = self.base / "moved-parent"
        parent.rename(moved)
        parent.mkdir()
        replacement = parent / "config"
        replacement.write_text("replacement")
        with self.assertRaises(module.Refusal):
            module.atomic_replace(entry, entry["new_text"], entry["mode"])
        self.assertEqual(replacement.read_text(), "replacement")
        self.assertEqual((moved / "config").read_text(), "original")

    def test_backup_root_is_synced_before_the_first_replacement(self):
        module = load_script()
        path = self.base / "config"
        path.write_text("old")
        entry = module.change(module.secure_read(path), "new", "fixture")
        root = self.state / "agent-config" / "backups"
        root_synced = False
        real_fsync = module.os.fsync
        real_replace = module.atomic_replace

        def observe_fsync(fd):
            nonlocal root_synced
            if root.exists():
                info = os.fstat(fd)
                root_info = root.stat()
                if (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino):
                    root_synced = True
            return real_fsync(fd)

        def assert_synced(*args, **kwargs):
            self.assertTrue(root_synced)
            return real_replace(*args, **kwargs)

        with mock.patch.dict(os.environ, {"HOME": str(self.home),
                                          "XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(module.os, "fsync", side_effect=observe_fsync), \
                mock.patch.object(module, "atomic_replace", side_effect=assert_synced):
            module.apply_changes([entry])
        self.assertEqual(path.read_text(), "new")


if __name__ == "__main__":
    unittest.main()
