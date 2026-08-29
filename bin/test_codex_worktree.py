#!/usr/bin/env python3
"""Regression tests for the managed Codex linked-worktree launcher."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from unittest import mock


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
        # A developer checkout may install another copy of this launcher ahead
        # of the real CLI. Do not recursively test one wrapper through another.
        if (resolved != WRAPPER.resolve() and resolved.name != WRAPPER.name
                and os.access(resolved, os.X_OK)):
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
        self.real_bin = self.home / ".nvm" / "bin"
        self.codex_home.mkdir(parents=True)
        self.link_bin.mkdir(parents=True)
        self.real_bin.mkdir(parents=True)
        (self.link_bin / "codex").symlink_to(WRAPPER)
        package = self.home / ".nvm" / "package"
        package.mkdir()
        fake = package / "codex.js"
        fake.write_text(textwrap.dedent('''\
            #!/usr/bin/env node
            exec /usr/bin/python3 -E -c 'import json, os, sys; routed={"GIT_DIR","GIT_WORK_TREE","GIT_COMMON_DIR","GIT_CEILING_DIRECTORIES","GIT_CONFIG_COUNT","GIT_CONFIG_PARAMETERS","GIT_GRAFT_FILE","GIT_INTERNAL_SUPER_PREFIX","GIT_NAMESPACE","GIT_PREFIX","GIT_REPLACE_REF_BASE","GIT_SHALLOW_FILE"}; bad=sorted(k for k in os.environ if k in routed or k.startswith(("GIT_CONFIG_KEY_","GIT_CONFIG_VALUE_","GIT_TRACE"))); print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "gitEnv": bad, "home": os.environ.get("HOME"), "codexHome": os.environ.get("CODEX_HOME"), "xdgConfigHome": os.environ.get("XDG_CONFIG_HOME")})); raise SystemExit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))' "$@"
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

    def add_submodule(self, checkout, name):
        source = self.base / f"{name}-source"
        self.git("init", "-q", source, cwd=self.base)
        self.git("config", "user.email", "test@example.com", cwd=source)
        self.git("config", "user.name", "Test", cwd=source)
        (source / "tracked").write_text("source\n")
        self.git("add", "tracked", cwd=source)
        self.git("commit", "-qm", "source", cwd=source)
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            source, name, cwd=checkout)
        return checkout / name

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
        return self.payload(result)["argv"]

    def payload(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    def fleet_arguments(self, wrapper, projects, access):
        """Complete a direct fleet plan the same way main does after budgeting."""
        plan = wrapper.fleet_workspace_plan(projects, access)
        wrapper.materialize_launch_guards(plan["guardContexts"], access)
        return plan["arguments"]

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

    def test_normal_checkout_pins_profile_and_protects_worktree_admin_root(self):
        result = self.run_wrapper("--version", cwd=self.main)
        args = self.argv(result)
        self.assertTrue(any(
            f'{json.dumps(str(self.main / ".git"))}="write"' in argument
            for argument in args), args)
        self.assertEqual(args[-1], "--version")
        self.assertNotIn('default_permissions="git-workspace"', args)
        self.assertTrue((self.main / ".git" / "worktrees").is_dir())

    def test_normal_launch_protects_an_in_tree_linked_worktree_pointer(self):
        nested = self.main / ".workspaces" / "nested"
        nested.parent.mkdir()
        self.git("worktree", "add", "-q", "-b", "nested-test", nested)
        result = self.run_wrapper("--version", cwd=self.main)
        args = self.argv(result)
        self.assertTrue(any(
            f'{json.dumps(str(nested / ".git"))}="read"' in argument
            for argument in args), (args, result.stderr))

    def test_normal_checkout_augmentation_error_fails_closed(self):
        config = self.codex_home / "config.toml"
        config.write_text(config.read_text() + '\n[permissions.codex-git-fleet-runtime-0]\n'
                          'description = "collision"\n')
        result = self.run_wrapper("--version", cwd=self.main)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("Git augmentation failed closed", result.stderr)

    def test_fleet_scan_error_makes_the_whole_session_read_only(self):
        projects = self.base / "projects"
        projects.mkdir()
        wrapper = load_wrapper()
        executable = {"program": Path("/bin/true"), "argv": ["/bin/true"]}
        with (mock.patch.dict(
                  os.environ, {"HOME": str(self.home)}, clear=True),
              mock.patch.object(wrapper, "find_real_codex", return_value=executable),
              mock.patch.object(wrapper, "account_home", return_value=self.home),
              mock.patch.object(
                  wrapper, "validated_codex_home", return_value=self.codex_home),
              mock.patch.object(
                  wrapper, "invocation_options",
                  return_value={"cwd": projects, "policyOverride": False}),
              mock.patch.object(wrapper, "linked_worktree", return_value=None),
              mock.patch.object(wrapper, "project_boundary", return_value=None),
              mock.patch.object(
                  wrapper, "fleet_workspace_plan",
                  side_effect=RuntimeError("scan budget exceeded")),
              mock.patch.object(wrapper.os, "execve") as execve,
              mock.patch.object(wrapper.sys, "argv", [str(WRAPPER), "--version"])):
            wrapper.main()
        argv = execve.call_args.args[1]
        self.assertEqual(argv[1:4], ["--sandbox", "read-only", "--version"])

    def test_exec_budget_error_makes_the_whole_session_read_only(self):
        projects = self.base / "projects"
        projects.mkdir()
        wrapper = load_wrapper()
        executable = {"program": Path("/bin/true"), "argv": ["/bin/true"]}
        with (mock.patch.dict(
                  os.environ, {"HOME": str(self.home)}, clear=True),
              mock.patch.object(wrapper, "find_real_codex", return_value=executable),
              mock.patch.object(wrapper, "account_home", return_value=self.home),
              mock.patch.object(
                  wrapper, "validated_codex_home", return_value=self.codex_home),
              mock.patch.object(
                  wrapper, "invocation_options",
                  return_value={"cwd": projects, "policyOverride": False}),
              mock.patch.object(wrapper, "linked_worktree", return_value=None),
              mock.patch.object(wrapper, "project_boundary", return_value=None),
              mock.patch.object(
                  wrapper, "fleet_workspace_plan",
                  return_value={"arguments": ["-c", "large"], "guardContexts": ()}),
              mock.patch.object(
                  wrapper, "ensure_exec_budget",
                  side_effect=[RuntimeError(
                      "aggregate exec argument budget was exceeded"), None]),
              mock.patch.object(wrapper.os, "execve") as execve,
              mock.patch.object(
                  wrapper.sys, "argv", [str(WRAPPER), "--version"])):
            wrapper.main()
        argv = execve.call_args.args[1]
        self.assertEqual(argv[1:4], ["--sandbox", "read-only", "--version"])

    def test_exec_budget_is_checked_before_guard_mutation(self):
        projects = self.base / "projects"
        child = projects / "child"
        projects.mkdir()
        self.git("init", "-q", child, cwd=self.base)
        wrapper = load_wrapper()
        executable = {"program": Path("/bin/true"), "argv": ["/bin/true"]}
        with (mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True),
              mock.patch.object(wrapper, "find_real_codex", return_value=executable),
              mock.patch.object(wrapper, "account_home", return_value=self.home),
              mock.patch.object(
                  wrapper, "validated_codex_home", return_value=self.codex_home),
              mock.patch.object(
                  wrapper, "invocation_options",
                  return_value={"cwd": projects, "policyOverride": False}),
              mock.patch.object(wrapper, "linked_worktree", return_value=None),
              mock.patch.object(wrapper, "project_boundary", return_value=None),
              mock.patch.object(
                  wrapper, "ensure_exec_budget",
                  side_effect=[RuntimeError("payload too large"), None]),
              mock.patch.object(wrapper.os, "execve") as execve,
              mock.patch.object(wrapper.sys, "argv", [str(WRAPPER), "--version"])):
            wrapper.main()
        self.assertEqual(
            execve.call_args.args[1][1:4], ["--sandbox", "read-only", "--version"])
        self.assertFalse((child / ".git" / "config.worktree").exists())
        self.assertFalse(
            (child / ".git" / "objects" / "info" / "alternates").exists())

    def test_oversized_user_argument_never_reaches_exec(self):
        wrapper = load_wrapper()
        executable = {"program": Path("/bin/true"), "argv": ["/bin/true"]}
        oversized = "x" * 64
        with (mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True),
              mock.patch.object(wrapper, "EXEC_SINGLE_ARG_MAX_BYTES", 32),
              mock.patch.object(wrapper, "find_real_codex", return_value=executable),
              mock.patch.object(wrapper, "account_home", return_value=self.home),
              mock.patch.object(
                  wrapper, "validated_codex_home", return_value=self.codex_home),
              mock.patch.object(
                  wrapper, "invocation_options",
                  return_value={"cwd": self.main, "policyOverride": True}),
              mock.patch.object(wrapper.os, "execve") as execve,
              mock.patch.object(wrapper.sys, "argv", [str(WRAPPER), oversized]),
              self.assertRaises(SystemExit) as raised):
            wrapper.main()
        self.assertEqual(raised.exception.code, 2)
        execve.assert_not_called()

    def test_home_launch_can_use_a_user_installed_codex(self):
        home_bin = self.real_bin
        path = f"{self.link_bin}:{home_bin}:/usr/bin:/bin"
        wrapper = load_wrapper()
        account = type("Account", (), {"pw_dir": str(self.home)})()
        with (mock.patch.object(wrapper, "project_boundary", return_value=None),
              mock.patch.object(wrapper, "raw_git_boundary", return_value=None),
              mock.patch.object(wrapper.pwd, "getpwuid", return_value=account),
              mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home), "PATH": path})):
            command = wrapper.find_real_codex(self.home)
        self.assertEqual(command["program"], Path("/bin/sh").resolve())

    def test_account_home_launch_is_forced_read_only(self):
        wrapper = load_wrapper()
        with (mock.patch.object(wrapper, "account_home", return_value=self.home),
              mock.patch.object(
                  wrapper, "find_real_codex",
                  return_value={"program": "/bin/true", "argv": ["/bin/true"]}),
              mock.patch.object(wrapper.sys, "argv", [str(WRAPPER), "--version"]),
              mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}),
              mock.patch.object(wrapper.os, "execve", side_effect=RuntimeError("exec"))
              as execute):
            self.assertTrue(wrapper.account_home_is_read_only(self.home))
            self.assertFalse(wrapper.account_home_is_read_only(self.home / "projects"))
            with (mock.patch.object(
                      wrapper, "invocation_options",
                      return_value={"cwd": self.home, "policyOverride": False}),
                  self.assertRaisesRegex(RuntimeError, "exec")):
                wrapper.main()
        self.assertEqual(
            execute.call_args.args[1],
            ["/bin/true", "--sandbox", "read-only", "--version"])

    def test_account_home_rejects_project_controlled_codex_home(self):
        hostile = self.home / "projects" / "hostile" / ".codex"
        hostile.mkdir(parents=True)
        wrapper = load_wrapper()
        with (mock.patch.object(wrapper, "account_home", return_value=self.home),
              mock.patch.object(
                  wrapper, "invocation_options",
                  return_value={"cwd": self.home, "policyOverride": False}),
              mock.patch.object(wrapper.sys, "argv", [str(WRAPPER), "--version"]),
              mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(hostile)}),
              self.assertRaisesRegex(SystemExit, "2")):
            wrapper.main()

    def test_home_launch_rejects_a_project_path_codex(self):
        project_bin = self.home / "projects" / "hostile" / "bin"
        project_bin.mkdir(parents=True)
        malicious = project_bin / "codex"
        marker = self.base / "home-project-codex-ran"
        malicious.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        malicious.chmod(0o755)
        path = f"{self.link_bin}:{project_bin}:{self.real_bin}:/usr/bin:/bin"
        wrapper = load_wrapper()
        account = type("Account", (), {"pw_dir": str(self.home)})()
        with (mock.patch.object(wrapper, "project_boundary", return_value=None),
              mock.patch.object(wrapper, "raw_git_boundary", return_value=None),
              mock.patch.object(wrapper.pwd, "getpwuid", return_value=account),
              mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home), "PATH": path})):
            command = wrapper.find_real_codex(self.home)
        self.assertNotEqual(command["program"], malicious)
        self.assertFalse(marker.exists())

    def test_home_environment_cannot_disable_project_executable_boundary(self):
        projects = self.base / "projects"
        projects.mkdir()
        wrapper = load_wrapper()
        account = type("Account", (), {"pw_dir": str(self.home)})()
        with (mock.patch.object(wrapper, "project_boundary", return_value=None),
              mock.patch.object(wrapper, "raw_git_boundary", return_value=None),
              mock.patch.object(wrapper.pwd, "getpwuid", return_value=account),
              mock.patch.dict(os.environ, {"HOME": str(projects)})):
            self.assertEqual(wrapper.executable_boundary(projects), projects)

    def test_malformed_parent_git_marker_is_the_executable_boundary(self):
        project = self.base / "malformed"
        nested = project / "deep"
        nested.mkdir(parents=True)
        (project / ".git").write_text("not a gitdir\n")
        wrapper = load_wrapper()
        self.assertEqual(wrapper.raw_git_boundary(nested), project)
        self.assertEqual(wrapper.executable_boundary(nested), project)

    def test_non_git_parent_adds_validated_child_repositories(self):
        projects = self.base / "projects"
        projects.mkdir()
        (projects / ".git").mkdir()  # Codex sandbox mount artifact at a non-Git root.
        (projects / ".git").chmod(0o555)
        direct = projects / "direct"
        nested = projects / "umbrella" / "nested"
        nested.parent.mkdir()
        self.git("init", "-q", direct, cwd=self.base)
        self.git("init", "-q", nested, cwd=self.base)

        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        roots = {args[index + 1] for index, value in enumerate(args) if value == "--add-dir"}
        self.assertEqual(roots, {str(direct), str(nested)})
        profile = next(
            args[index + 1] for index, value in enumerate(args)
            if value == "-c" and args[index + 1].startswith(
                f"permissions.{wrapper.FLEET_RUNTIME_PROFILE}-"))
        self.assertIn('extends="no-git"', profile)
        self.assertTrue(any(
            f'{json.dumps(str(direct / ".git"))}="write"' in arg for arg in args))
        for root in (direct, nested):
            self.assertTrue((root / ".git" / "config.worktree").is_file())
            self.assertTrue((root / ".git" / "modules").is_dir())
            self.assertTrue((root / ".git" / "objects" / "info" / "alternates").is_file())
            self.git("status", "--porcelain", cwd=root)

    def test_fleet_scan_protects_nested_pointer_checkouts(self):
        projects = self.base / "projects"
        enabled = projects / "enabled"
        submodule_source = self.base / "submodule-source"
        nested_source = self.base / "nested-source"
        projects.mkdir()
        for repository in (enabled, submodule_source, nested_source):
            self.git("init", "-q", repository, cwd=self.base)
            self.git("config", "user.email", "test@example.com", cwd=repository)
            self.git("config", "user.name", "Test", cwd=repository)
            (repository / "tracked").write_text("one\n")
            self.git("add", "tracked", cwd=repository)
            self.git("commit", "-qm", "initial", cwd=repository)
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            nested_source, "nested", cwd=submodule_source)
        self.git("commit", "-qam", "add nested submodule", cwd=submodule_source)
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            submodule_source, "sub", cwd=enabled)
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "update",
            "--init", "--recursive", "-q", cwd=enabled)

        wrapper = load_wrapper()
        discovery = wrapper.discover_ordinary_checkouts(projects, load_access())
        self.assertEqual(discovery["roots"], [enabled])
        self.assertTrue({
            enabled / ".git",
            enabled / "sub" / ".git",
            enabled / "sub" / "nested" / ".git",
        }.issubset(set(discovery["protectedPaths"])))

    def test_fleet_protects_separate_git_directory_inside_umbrella(self):
        projects = self.base / "projects"
        source = projects / "source"
        control = projects / "control"
        projects.mkdir()
        self.git(
            "init", "-q", "--separate-git-dir", control, source,
            cwd=self.base)
        hooks = source / ".githooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", ".githooks", cwd=source)
        wrapper = load_wrapper()
        access = load_access()
        discovery = wrapper.discover_ordinary_checkouts(projects, access)
        self.assertEqual(discovery["roots"], [])
        self.assertTrue({source / ".git", control, hooks}.issubset(
            set(discovery["protectedPaths"])))
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        self.assertTrue(any(
            f'{json.dumps(str(control))}="read"' in arg for arg in args), args)
        self.assertTrue(any(
            f'{json.dumps(str(hooks))}="read"' in arg for arg in args), args)

    def test_pointer_checkout_config_include_fails_closed(self):
        projects = self.base / "projects"
        source = projects / "source"
        control = projects / "control"
        projects.mkdir()
        self.git(
            "init", "-q", "--separate-git-dir", control, source,
            cwd=self.base)
        included = source / ".mutable-gitconfig"
        included.write_text("[core]\n\thooksPath = .mutable-hooks\n")
        with (control / "config").open("a") as config:
            config.write(f"\n[include]\n\tpath = {included}\n")

        wrapper = load_wrapper()
        with (mock.patch.dict(os.environ, {"HOME": str(self.home)}),
              self.assertRaisesRegex(RuntimeError, "config includes")):
            wrapper.discover_ordinary_checkouts(projects, load_access())

    def test_lexically_symlinked_pointer_target_fails_closed(self):
        projects = self.base / "projects"
        source = projects / "source"
        actual = projects / "actual"
        control = actual / "control"
        projects.mkdir()
        actual.mkdir()
        self.git(
            "init", "-q", "--separate-git-dir", control, source,
            cwd=self.base)
        route = projects / "route"
        route.symlink_to(actual, target_is_directory=True)
        (source / ".git").write_text(f"gitdir: {route / 'control'}\n")

        wrapper = load_wrapper()
        access = load_access()
        with self.assertRaisesRegex(access.Refusal, "cannot open safely"):
            wrapper.discover_ordinary_checkouts(projects, access)

    def test_empty_non_git_parent_uses_no_git_profile(self):
        projects = self.base / "projects"
        projects.mkdir()
        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        self.assertEqual(args, ["-c", 'default_permissions="no-git"'])

    def test_fleet_profile_keeps_each_argument_below_linux_limit(self):
        wrapper = load_wrapper()
        roots = [Path("/workspace") / ("project-" + str(index).zfill(3) + "-" + "x" * 80)
                 for index in range(wrapper.FLEET_SCAN_MAX_REPOSITORIES)]
        args = wrapper.fleet_runtime_profile(
            roots, [root / ".git" for root in roots], "git-workspace")
        self.assertLessEqual(
            max(len(arg.encode()) for arg in args), wrapper.EXACT_PROFILE_ARG_MAX_BYTES)
        profiles = [arg for arg in args if arg.startswith(
            f"permissions.{wrapper.FLEET_RUNTIME_PROFILE}-")]
        self.assertGreater(len(profiles), 1)
        self.assertLessEqual(
            wrapper.argument_bytes(args), wrapper.EXACT_PROFILE_TOTAL_MAX_BYTES)

    def test_fleet_profile_rejects_an_aggregate_argument_overflow(self):
        wrapper = load_wrapper()
        with (mock.patch.object(wrapper, "EXACT_PROFILE_TOTAL_MAX_BYTES", 64),
              self.assertRaisesRegex(RuntimeError, "aggregate runtime profile")):
            wrapper.exact_profile_args(
                wrapper.FLEET_RUNTIME_PROFILE, "test", "no-git",
                [(Path("/workspace/project/.git"), "read")])

    def test_fleet_profile_budget_is_checked_before_guard_mutation(self):
        projects = self.base / "projects"
        child = projects / "child"
        projects.mkdir()
        self.git("init", "-q", child, cwd=self.base)
        wrapper = load_wrapper()
        access = load_access()
        with (mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}),
              mock.patch.object(wrapper, "EXACT_PROFILE_TOTAL_MAX_BYTES", 64),
              self.assertRaisesRegex(RuntimeError, "aggregate runtime profile")):
            self.fleet_arguments(wrapper, projects, access)
        self.assertFalse((child / ".git" / "config.worktree").exists())
        self.assertFalse(
            (child / ".git" / "objects" / "info" / "alternates").exists())

    def test_git_output_is_streamed_with_a_hard_byte_cap(self):
        wrapper = load_wrapper()
        with mock.patch.object(wrapper, "GIT_OUTPUT_MAX_BYTES", 4):
            with self.assertRaisesRegex(RuntimeError, "output exceeded"):
                wrapper.git_output(self.main, "rev-parse", "--show-toplevel")

    def test_git_subprocess_has_a_hard_time_limit(self):
        wrapper = load_wrapper()
        with (mock.patch.object(wrapper, "GIT_TIMEOUT_SECONDS", 0.05),
              self.assertRaisesRegex(RuntimeError, "timed out")):
            wrapper.bounded_process(
                ["/bin/sleep", "5"], os.environ.copy(), "fixture")

    def test_fleet_scan_has_one_aggregate_deadline(self):
        projects = self.base / "deadline-projects"
        projects.mkdir()
        wrapper = load_wrapper()
        with self.assertRaisesRegex(RuntimeError, "aggregate Git preflight timed out"):
            wrapper.discover_ordinary_checkouts(
                projects, load_access(), deadline=time.monotonic() - 1)

    def test_empty_sandbox_git_mountpoint_does_not_hide_child_repositories(self):
        projects = self.base / "projects"
        child = projects / "child"
        projects.mkdir()
        (projects / ".git").mkdir()
        wrapper = load_wrapper()
        self.assertFalse(wrapper.sandbox_direct_git_mountpoint(projects))
        read_only = type("StatVfs", (), {"f_flag": os.ST_RDONLY})()
        with mock.patch.object(wrapper.os, "statvfs", return_value=read_only):
            self.assertTrue(wrapper.sandbox_direct_git_mountpoint(projects))
        (projects / ".git").chmod(0o555)
        self.git("init", "-q", child, cwd=self.base)

        access = load_access()
        self.assertTrue(wrapper.sandbox_direct_git_mountpoint(projects))
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        roots = {args[index + 1] for index, value in enumerate(args) if value == "--add-dir"}
        self.assertEqual(roots, {str(child)})

    def test_writable_sandbox_marker_requires_read_only_mounted_placeholders(self):
        projects = self.base / "projects"
        marker = projects / ".git"
        marker.mkdir(parents=True)
        for name in ("config", "config.worktree", "hooks"):
            (marker / name).write_text("")
        wrapper = load_wrapper()
        read_only = type("StatVfs", (), {"f_flag": os.ST_RDONLY})()
        with mock.patch.object(wrapper.os, "statvfs", return_value=read_only):
            self.assertTrue(wrapper.sandbox_direct_git_mountpoint(projects))

    def test_read_only_sandbox_git_placeholders_do_not_hide_children(self):
        projects = self.base / "projects"
        child = projects / "child"
        marker = projects / ".git"
        marker.mkdir(parents=True)
        for name in ("config", "config.worktree", "hooks"):
            path = marker / name
            path.write_text("")
            path.chmod(0o444)
        marker.chmod(0o555)
        self.git("init", "-q", child, cwd=self.base)

        wrapper = load_wrapper()
        access = load_access()
        self.assertTrue(wrapper.sandbox_direct_git_mountpoint(projects))
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        roots = {args[index + 1] for index, value in enumerate(args) if value == "--add-dir"}
        self.assertEqual(roots, {str(child)})

    def test_existing_malformed_git_marker_fails_the_fleet_closed(self):
        projects = self.base / "projects"
        clean = projects / "clean"
        malformed = projects / "malformed"
        projects.mkdir()
        self.git("init", "-q", clean, cwd=self.base)
        malformed.mkdir()
        (malformed / ".git").mkdir()

        wrapper = load_wrapper()
        access = load_access()
        with (mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}),
              self.assertRaises(RuntimeError)):
            self.fleet_arguments(wrapper, projects, access)
        self.assertFalse((clean / ".git" / "config.worktree").exists())

    def test_existing_special_git_marker_fails_the_fleet_closed(self):
        projects = self.base / "projects"
        malformed = projects / "malformed"
        malformed.mkdir(parents=True)
        os.mkfifo(malformed / ".git")
        wrapper = load_wrapper()
        with self.assertRaisesRegex(RuntimeError, "unsafe type"):
            wrapper.discover_ordinary_checkouts(projects, load_access())

    def test_non_git_parent_honors_child_no_git_policy(self):
        projects = self.base / "projects"
        projects.mkdir()
        (projects / ".git").mkdir()
        (projects / ".git").chmod(0o555)
        enabled = projects / "enabled"
        disabled = projects / "disabled"
        self.git("init", "-q", enabled, cwd=self.base)
        self.git("init", "-q", disabled, cwd=self.base)
        local = disabled / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "no-git"\n')

        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        roots = {args[index + 1] for index, value in enumerate(args) if value == "--add-dir"}
        self.assertEqual(roots, {str(enabled)})
        self.assertTrue(any(
            f'{json.dumps(str(local))}="read"' in argument for argument in args))
        self.assertTrue((enabled / ".git" / "config.worktree").exists())
        self.assertFalse((disabled / ".git" / "config.worktree").exists())

    def test_fleet_unsafe_child_policy_fails_entire_workspace_read_only(self):
        projects = self.base / "unsafe-policy-projects"
        child = projects / "child"
        projects.mkdir()
        self.git("init", "-q", child, cwd=self.base)
        external = self.base / "unsafe-child-policy"
        external.write_text('default_permissions = "no-git"\n')
        local = child / ".codex" / "config.toml"
        local.parent.mkdir()
        local.symlink_to(external)
        wrapper = load_wrapper()
        access = load_access()
        with (mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}),
              self.assertRaises(access.Refusal)):
            self.fleet_arguments(wrapper, projects, access)
        self.assertFalse((child / ".git" / "config.worktree").exists())

    def test_in_tree_worktree_unsafe_policy_fails_entire_workspace_read_only(self):
        nested = self.main / ".workspaces" / "unsafe-policy-linked"
        nested.parent.mkdir()
        self.git("worktree", "add", "-q", "-b", "unsafe-policy-linked", nested)
        external = self.base / "unsafe-linked-policy"
        external.write_text('default_permissions = "no-git"\n')
        local = nested / ".codex" / "config.toml"
        local.parent.mkdir()
        local.symlink_to(external)
        result = self.run_wrapper("--version", cwd=self.main)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("cannot open safely", result.stderr)

    def test_fleet_validates_every_child_policy_before_any_guard_mutation(self):
        projects = self.base / "projects"
        enabled = projects / "a-enabled"
        unsafe = projects / "z-unsafe"
        projects.mkdir()
        self.git("init", "-q", enabled, cwd=self.base)
        self.git("init", "-q", unsafe, cwd=self.base)
        policy = unsafe / ".codex" / "config.toml"
        policy.parent.mkdir()
        policy.write_text('default_permissions = "git-workspace"\n')
        policy.chmod(0o666)

        wrapper = load_wrapper()
        access = load_access()
        try:
            with (mock.patch.dict(os.environ, {
                      "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}),
                  self.assertRaisesRegex(access.Refusal, "writable by another user")):
                self.fleet_arguments(wrapper, projects, access)
        finally:
            policy.chmod(0o600)
        self.assertFalse((enabled / ".git" / "config.worktree").exists())
        self.assertFalse(
            (enabled / ".git" / "objects" / "info" / "alternates").exists())

    def test_non_git_parent_no_git_policy_disables_all_children(self):
        projects = self.base / "projects"
        child = projects / "child"
        projects.mkdir()
        self.git("init", "-q", child, cwd=self.base)
        local = projects / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "no-git"\n')

        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        self.assertNotIn("--add-dir", args)
        self.assertFalse((child / ".git" / "config.worktree").exists())

    def test_online_parent_tightens_the_fleet_for_an_offline_child(self):
        projects = self.base / "projects"
        offline = projects / "offline"
        projects.mkdir()
        self.git("init", "-q", offline, cwd=self.base)
        local = offline / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "git-workspace-offline"\n')

        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        self.assertIn(str(offline), args)
        profile = next(
            value for value in args
            if value.startswith(f"permissions.{wrapper.FLEET_RUNTIME_PROFILE}-"))
        self.assertIn('extends="no-git-offline"', profile)

    def test_peer_writable_umbrella_fails_closed(self):
        peer = self.base / "peer"
        projects = peer / "projects"
        child = projects / "child"
        projects.mkdir(parents=True)
        self.git("init", "-q", child, cwd=self.base)
        peer.chmod(0o777)

        wrapper = load_wrapper()
        access = load_access()
        with (mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}),
              self.assertRaisesRegex(access.Refusal, "writable by another user")):
            self.fleet_arguments(wrapper, projects, access)

    def test_system_owned_sticky_ancestor_is_safe_but_not_a_writable_leaf(self):
        sticky = self.base / "sticky"
        owned = sticky / "owned"
        owned.mkdir(parents=True)
        sticky.chmod(0o1777)
        access = load_access()
        access.secure_fs.validate_trusted_path(owned)
        with self.assertRaisesRegex(access.Refusal, "writable by another user"):
            access.secure_fs.validate_trusted_path(sticky)

    def test_fleet_protects_a_sibling_worktrees_custom_hooks(self):
        projects = self.base / "projects"
        main = projects / "main"
        linked = projects / "linked"
        projects.mkdir()
        self.git("init", "-q", main, cwd=self.base)
        self.git("config", "user.email", "test@example.com", cwd=main)
        self.git("config", "user.name", "Test", cwd=main)
        (main / "tracked").write_text("one\n")
        self.git("add", "tracked", cwd=main)
        self.git("commit", "-qm", "initial", cwd=main)
        self.git("worktree", "add", "-q", "-b", "fleet-linked", linked, cwd=main)
        self.git("config", "extensions.worktreeConfig", "true", cwd=main)
        hooks = linked / ".githooks"
        hooks.mkdir()
        self.git("config", "--worktree", "core.hooksPath", ".githooks", cwd=linked)

        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        self.assertTrue(any(
            f'{json.dumps(str(hooks))}="read"' in argument for argument in args),
            args)

    def test_symlinked_ordinary_git_directory_is_never_mutated(self):
        project = self.base / "symlinked-git"
        external = self.base / "external-git"
        self.git("init", "-q", project, cwd=self.base)
        (project / ".git").rename(external)
        (project / ".git").symlink_to(external, target_is_directory=True)

        wrapper = load_wrapper()
        access = load_access()
        with self.assertRaisesRegex(RuntimeError, "not a real directory"):
            wrapper.ordinary_checkout(project, access)
        self.assertFalse((external / "config.worktree").exists())
        self.assertFalse((external / "objects" / "info" / "alternates").exists())

    def test_symlinked_or_hardlinked_git_config_fails_closed(self):
        wrapper = load_wrapper()
        access = load_access()
        config = self.main / ".git" / "config"
        original = config.read_text()
        for shape in ("symlink", "hardlink"):
            with self.subTest(shape=shape):
                external = self.base / f"external-config-{shape}"
                if shape == "symlink":
                    config.rename(external)
                    config.symlink_to(external)
                else:
                    os.link(config, external)
                with self.assertRaises(access.Refusal):
                    wrapper.validate_git_config_paths([config], access)
                self.assertFalse((self.main / ".git" / "config.worktree").exists())
                if shape == "symlink":
                    config.unlink()
                    external.rename(config)
                else:
                    external.unlink()
                self.assertEqual(config.read_text(), original)

    def test_local_git_config_include_fails_closed_before_guard_mutation(self):
        wrapper = load_wrapper()
        access = load_access()
        included = self.base / "included-config"
        included.write_text("[core]\n\thooksPath = hostile\n")
        config = self.main / ".git" / "config"
        with config.open("a") as target:
            target.write(f"\n[include]\n\tpath = {included}\n")
        with self.assertRaisesRegex(RuntimeError, "config includes"):
            wrapper.validate_git_config_paths([config], access)
        self.assertFalse((self.main / ".git" / "config.worktree").exists())

    def test_local_fsmonitor_fails_closed_before_guard_mutation(self):
        wrapper = load_wrapper()
        access = load_access()
        self.git("config", "core.fsmonitor", "./mutable-monitor", cwd=self.main)
        with self.assertRaisesRegex(RuntimeError, "core.fsmonitor"):
            wrapper.validate_git_config_paths(
                [self.main / ".git" / "config"], access)
        self.assertFalse((self.main / ".git" / "config.worktree").exists())

    def test_global_include_of_project_config_fails_closed(self):
        included = self.main / ".mutable-gitconfig"
        included.write_text("[core]\n\thooksPath = .mutable-hooks\n")
        (self.home / ".gitconfig").write_text(
            f'[includeIf "gitdir:{self.main}/"]\n\tpath = {included}\n')
        wrapper = load_wrapper()
        with (mock.patch.dict(os.environ, {"HOME": str(self.home)}),
              self.assertRaisesRegex(RuntimeError, "effective Git config includes")):
            wrapper.effective_git_config_sources(
                self.main, self.main, load_access())
        self.assertFalse((self.main / ".git" / "config.worktree").exists())

    def test_global_include_cannot_be_created_after_preflight(self):
        missing = self.main / ".future-gitconfig"
        (self.home / ".gitconfig").write_text(
            f'[includeIf "gitdir:{self.main}/"]\n\tpath = {missing}\n')
        wrapper = load_wrapper()
        with (mock.patch.dict(os.environ, {"HOME": str(self.home)}),
              self.assertRaisesRegex(RuntimeError, "effective Git config includes")):
            wrapper.effective_git_config_sources(
                self.main, self.main, load_access())

    def test_hardlinked_global_git_config_fails_closed(self):
        global_config = self.home / ".gitconfig"
        global_config.write_text("[user]\n\tname = Global User\n")
        os.link(global_config, self.main / ".writable-global-config")
        wrapper = load_wrapper()
        with (mock.patch.dict(os.environ, {"HOME": str(self.home)}),
              self.assertRaisesRegex(RuntimeError, "config source is unsafe")):
            wrapper.effective_git_config_sources(
                self.main, self.main, load_access())

    def test_worktree_fsmonitor_fails_closed_before_guard_mutation(self):
        wrapper = load_wrapper()
        access = load_access()
        admin = Path(self.git(
            "rev-parse", "--absolute-git-dir", cwd=self.linked).stdout.strip())
        config = admin / "config.worktree"
        self.git(
            "config", "--file", config, "core.fsmonitor", "./mutable-monitor",
            cwd=self.base)
        with self.assertRaisesRegex(RuntimeError, "core.fsmonitor"):
            wrapper.validate_git_config_paths([config], access)
        self.assertFalse(
            (self.main / ".git" / "objects" / "info" / "alternates").exists())

    def test_sibling_worktree_config_include_fails_closed_before_guard_mutation(self):
        included = self.base / "sibling-worktree-include"
        included.write_text("[core]\n\thooksPath = hostile\n")
        sibling_config = self.main / ".git" / "worktrees" / "linked" / "config.worktree"
        sibling_config.write_text(f"[includeIf \"gitdir:**\"]\n\tpath = {included}\n")
        result = self.run_wrapper("--version", cwd=self.main)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("config includes", result.stderr)
        self.assertFalse(
            (self.main / ".git" / "objects" / "info" / "alternates").exists())

    def test_worktree_config_includes_fail_closed_before_guard_mutation(self):
        wrapper = load_wrapper()
        access = load_access()
        included = self.base / "included-worktree-config"
        included.write_text("[core]\n\thooksPath = hostile\n")
        admin = Path(self.git(
            "rev-parse", "--absolute-git-dir", cwd=self.linked).stdout.strip())
        cases = (
            (self.main, self.main / ".git" / "config.worktree", []),
            (self.linked, admin / "config.worktree", [admin / "config.worktree"]),
        )
        for root, config, worktree_paths in cases:
            with self.subTest(config=config):
                config.write_text(f"[include]\n\tpath = {included}\n")
                with self.assertRaisesRegex(RuntimeError, "config includes"):
                    wrapper.validate_git_config_paths([config], access)
                config.unlink()
        self.assertFalse((self.main / ".git" / "config.worktree").exists())
        self.assertFalse((admin / "config.worktree").exists())

    def test_fleet_validates_every_config_before_mutating_any_repository(self):
        projects = self.base / "config-validation-projects"
        clean = projects / "a-clean"
        hostile = projects / "z-hostile"
        projects.mkdir()
        self.git("init", "-q", clean, cwd=self.base)
        self.git("init", "-q", hostile, cwd=self.base)
        included = self.base / "fleet-included-config"
        included.write_text("[core]\n\thooksPath = hostile\n")
        with (hostile / ".git" / "config").open("a") as target:
            target.write(f"\n[include]\n\tpath = {included}\n")
        wrapper = load_wrapper()
        access = load_access()
        with (mock.patch.dict(os.environ, {
                  "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}),
              self.assertRaisesRegex(RuntimeError, "config includes")):
            self.fleet_arguments(wrapper, projects, access)
        self.assertFalse((clean / ".git" / "config.worktree").exists())
        self.assertFalse(
            (clean / ".git" / "objects" / "info" / "alternates").exists())

    def test_symlinked_active_hook_target_is_protected(self):
        wrapper = load_wrapper()
        access = load_access()
        external = self.base / "external-hook"
        external.write_text("#!/bin/sh\nexit 0\n")
        hook = self.main / ".git" / "hooks" / "pre-commit"
        hook.symlink_to(external)
        protected = wrapper.validate_checkout_hooks(
            self.main, self.main / ".git", access)
        self.assertEqual(protected, [external])
        self.assertFalse((self.main / ".git" / "config.worktree").exists())

    def test_active_hook_symlink_chain_fails_closed_before_guard_mutation(self):
        wrapper = load_wrapper()
        access = load_access()
        external = self.base / "external-hook"
        external.write_text("#!/bin/sh\nexit 0\n")
        intermediate = self.base / "hook-target-link"
        intermediate.symlink_to(external)
        hook = self.main / ".git" / "hooks" / "pre-commit"
        hook.symlink_to(intermediate)
        with self.assertRaisesRegex(access.Refusal, "cannot open safely"):
            wrapper.validate_checkout_hooks(self.main, self.main / ".git", access)
        self.assertFalse((self.main / ".git" / "config.worktree").exists())

    def test_hooks_path_preserves_trailing_whitespace(self):
        wrapper = load_wrapper()
        access = load_access()
        hooks = self.main / ".githooks "
        hooks.mkdir()
        self.git("config", "core.hooksPath", ".githooks ")
        protected = wrapper.validate_checkout_hooks(
            self.main, self.main / ".git", access)
        self.assertEqual(protected, [hooks])

    def test_symlinked_hooks_path_fails_closed(self):
        wrapper = load_wrapper()
        access = load_access()
        target = self.main / ".hooks-safe"
        target.mkdir()
        (self.main / ".hook-link").symlink_to(target, target_is_directory=True)
        self.git("config", "core.hooksPath", ".hook-link")
        with self.assertRaisesRegex(access.Refusal, "cannot open safely"):
            wrapper.validate_checkout_hooks(
                self.main, self.main / ".git", access)

    def test_fifo_hooks_path_fails_closed_without_blocking(self):
        fifo = self.main / ".hooks-fifo"
        os.mkfifo(fifo)
        self.git("config", "core.hooksPath", ".hooks-fifo")
        started = time.monotonic()
        result = self.run_wrapper("--version", cwd=self.main)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("cannot open safely", result.stderr)

    def test_peer_writable_ancestor_fails_closed_for_linked_worktree(self):
        peer = self.base / "peer"
        main = peer / "main"
        linked = peer / "linked"
        peer.mkdir()
        self.git("init", "-q", main, cwd=self.base)
        self.git("config", "user.email", "test@example.com", cwd=main)
        self.git("config", "user.name", "Test", cwd=main)
        (main / "tracked").write_text("one\n")
        self.git("add", "tracked", cwd=main)
        self.git("commit", "-qm", "initial", cwd=main)
        self.git("worktree", "add", "-q", "-b", "peer-linked", linked, cwd=main)
        peer.chmod(0o777)

        result = self.run_wrapper("--version", cwd=linked)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("writable by another user", result.stderr)
        self.assertFalse((main / ".git" / "config.worktree").exists())

    def test_peer_writable_policy_ancestor_fails_closed_for_normal_checkout(self):
        peer = self.main / "peer-policy"
        cwd = peer / "deep"
        policy = peer / ".codex" / "config.toml"
        cwd.mkdir(parents=True)
        policy.parent.mkdir()
        policy.write_text('default_permissions = "git-workspace"\n')
        peer.chmod(0o777)
        try:
            result = self.run_wrapper("--version", cwd=cwd)
        finally:
            peer.chmod(0o700)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("writable by another user", result.stderr)

    def test_unsafe_in_tree_worktree_policy_fails_before_guard_mutation(self):
        nested = self.main / ".workspaces" / "nested"
        nested.parent.mkdir()
        self.git("worktree", "add", "-q", "-b", "unsafe-nested", nested)
        policy = nested / ".codex" / "config.toml"
        policy.parent.mkdir()
        policy.write_text('default_permissions = "git-workspace"\n')
        policy.chmod(0o666)
        try:
            result = self.run_wrapper("--version", cwd=self.main)
        finally:
            policy.chmod(0o600)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("writable by another user", result.stderr)
        self.assertFalse((self.main / ".git" / "config.worktree").exists())
        self.assertFalse(
            (self.main / ".git" / "objects" / "info" / "alternates").exists())

    def test_linked_worktree_no_git_policy_closes_shared_common_git(self):
        projects = self.base / "projects"
        main = projects / "main"
        linked = projects / "linked"
        projects.mkdir()
        self.git("init", "-q", main, cwd=self.base)
        self.git("config", "user.email", "test@example.com", cwd=main)
        self.git("config", "user.name", "Test", cwd=main)
        (main / "tracked").write_text("one\n")
        self.git("add", "tracked", cwd=main)
        self.git("commit", "-qm", "initial", cwd=main)
        self.git("worktree", "add", "-q", "-b", "linked", linked, cwd=main)
        local = linked / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "no-git"\n')

        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            args = self.fleet_arguments(wrapper, projects, access)
        self.assertNotIn(str(main), args)

    def test_non_git_parent_does_not_execute_child_codex(self):
        projects = self.base / "projects"
        project_bin = projects / "child" / "bin"
        project_bin.mkdir(parents=True)
        (projects / ".git").mkdir()
        malicious = project_bin / "codex"
        marker = self.base / "fleet-project-codex-ran"
        malicious.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        malicious.chmod(0o755)
        wrapper = load_wrapper()
        self.assertIsNone(wrapper.trusted_candidate(malicious, projects))
        self.assertFalse(marker.exists())

    def test_permission_plan_carries_every_policy_source(self):
        root_policy = self.linked / ".codex" / "config.toml"
        nested = self.linked / "nested"
        nested_policy = nested / ".codex" / "config.toml"
        root_policy.parent.mkdir()
        nested_policy.parent.mkdir(parents=True)
        root_policy.write_text('default_permissions = "git-workspace-offline"\n')
        nested_policy.write_text('default_permissions = "no-git-offline"\n')

        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            context = wrapper.linked_worktree(self.linked, access)
            plan = wrapper.permission_plan(context, nested, access)
        self.assertEqual(plan["active"], "no-git-offline")
        self.assertEqual(plan["protectedPaths"], (root_policy, nested_policy))

    def test_offline_and_no_git_project_overrides(self):
        local = self.linked / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "git-workspace-offline"\n')
        offline = self.argv(self.run_wrapper("resume", "one"))
        self.assertIn('extends="no-git-offline"', offline[3])

        local.write_text('default_permissions = "no-git-offline"\n')
        disabled = self.argv(self.run_wrapper("resume", "two"))
        self.assertTrue(any(
            f'{json.dumps(str(local))}="read"' in argument for argument in disabled))
        self.assertIn("resume", disabled)
        self.assertEqual(disabled[-1], "two")

    def test_local_policy_cannot_widen_global_git_or_network_access(self):
        access = load_access()
        local = self.linked / ".codex" / "config.toml"
        local.parent.mkdir()

        (self.codex_home / "config.toml").write_text(
            'default_permissions = "no-git"\n\n' + access.PROFILE_BLOCK)
        local.write_text('default_permissions = "git-workspace"\n')
        git_result = self.run_wrapper("resume", "git")
        self.assertEqual(
            self.argv(git_result),
            ["--sandbox", "read-only", "resume", "git"])
        self.assertIn("cannot re-enable Git", git_result.stderr)

        (self.codex_home / "config.toml").write_text(
            'default_permissions = "git-workspace-offline"\n\n' + access.PROFILE_BLOCK)
        local.write_text('default_permissions = "git-workspace"\n')
        network_result = self.run_wrapper("resume", "network")
        self.assertEqual(
            self.argv(network_result),
            ["--sandbox", "read-only", "resume", "network"])
        self.assertIn("cannot re-enable network", network_result.stderr)

    def test_reserved_runtime_profile_collision_fails_closed(self):
        config = self.codex_home / "config.toml"
        config.write_text(config.read_text() + '\n[permissions.codex-git-linked-runtime]\n'
                          'description = "collision"\n')
        result = self.run_wrapper("resume", "one")
        self.assertEqual(
            self.argv(result),
            ["--sandbox", "read-only", "resume", "one"])
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
        trace = self.base / "git-trace-ran"
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
            GIT_CONFIG_PARAMETERS="'core.bare'='true'",
            GIT_GRAFT_FILE=str(foreign / "grafts"),
            GIT_INTERNAL_SUPER_PREFIX="hostile/",
            GIT_NAMESPACE="hostile",
            GIT_PREFIX="hostile/",
            GIT_REPLACE_REF_BASE="refs/hostile/",
            GIT_SHALLOW_FILE=str(foreign / "shallow"),
            GIT_TRACE=str(trace),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.argv(result)
        self.assertEqual(args[0:2], ["--add-dir", str((self.main / ".git").resolve())])
        self.assertEqual(self.payload(result)["gitEnv"], [])
        self.assertFalse(trace.exists())

    def test_project_environment_cannot_redirect_home_or_codex_config(self):
        contaminated_home = self.main / "hostile-home"
        contaminated_home.mkdir()
        contaminated_xdg = self.main / "hostile-xdg"
        contaminated_xdg.mkdir()
        result = self.run_wrapper(
            "--version", cwd=self.main, HOME=str(contaminated_home),
            XDG_CONFIG_HOME=str(contaminated_xdg))
        payload = self.payload(result)
        self.assertEqual(payload["home"], pwd.getpwuid(os.getuid()).pw_dir)
        self.assertEqual(payload["codexHome"], str(self.codex_home))
        self.assertIsNone(payload["xdgConfigHome"])

        hostile_codex_home = self.main / "hostile-codex-home"
        hostile_codex_home.mkdir()
        refused = self.run_wrapper(
            "--version", cwd=self.main, CODEX_HOME=str(hostile_codex_home))
        self.assertEqual(refused.returncode, 2)
        self.assertIn("CODEX_HOME is project-controlled", refused.stderr)
        self.assertFalse(refused.stdout)

    def test_explicit_policy_override_is_preserved(self):
        args = self.argv(self.run_wrapper("--sandbox", "read-only", "resume", "one"))
        self.assertEqual(args, ["--sandbox", "read-only", "resume", "one"])
        args = self.argv(self.run_wrapper("--profile=locked", "resume", "one"))
        self.assertEqual(args, ["--profile=locked", "resume", "one"])
        args = self.argv(self.run_wrapper("--sandbox=read-only", "resume", "one"))
        self.assertEqual(args, ["--sandbox=read-only", "resume", "one"])
        args = self.argv(self.run_wrapper("-c", 'default_permissions=":read-only"', "resume", "one"))
        self.assertEqual(args, ["-c", 'default_permissions=":read-only"', "resume", "one"])
        permission = 'sandbox_permissions=["disk-full-read-access"]'
        args = self.argv(self.run_wrapper("-c", permission, "resume", "one"))
        self.assertEqual(args, ["-c", permission, "resume", "one"])
        extra = self.base / "explicit-extra"
        extra.mkdir()
        args = self.argv(self.run_wrapper("--add-dir", extra, "resume", "one"))
        self.assertEqual(args, ["--add-dir", str(extra), "resume", "one"])
        for flag in ("--approve-for-me", "--not-so-yolo"):
            with self.subTest(flag=flag):
                args = self.argv(self.run_wrapper(flag, "resume", "one"))
                self.assertEqual(args, [flag, "resume", "one"])

    def test_project_sandbox_permissions_fail_closed(self):
        local = self.main / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text(
            'default_permissions = "git-workspace"\n'
            'sandbox_permissions = ["disk-full-read-access"]\n')
        result = self.run_wrapper("--version", cwd=self.main)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("project permission keys", result.stderr)

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
        self.assertEqual(
            self.argv(result),
            ["--sandbox", "read-only", "resume", "one"])
        self.assertIn("augmentation failed closed", result.stderr)

    def test_submodule_shape_is_not_treated_as_a_worktree(self):
        sub = self.main / "sub"
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(self.main), "sub")
        result = self.run_wrapper("--version", cwd=sub)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("augmentation failed closed", result.stderr)

    def test_normal_launch_protects_initialized_submodule_pointer_and_target(self):
        submodule = self.add_submodule(self.main, "normal-sub")
        marker = submodule / ".git"
        original_git_dir = Path(self.git(
            "rev-parse", "--absolute-git-dir", cwd=submodule).stdout.strip()).resolve()
        writable_git_dir = self.main / "writable-submodule-git"
        original_git_dir.rename(writable_git_dir)
        marker.write_text(f"gitdir: {writable_git_dir}\n")
        self.git(
            "config", "--file", writable_git_dir / "config", "core.worktree",
            submodule, cwd=self.base)
        result = self.run_wrapper("--version", cwd=self.main)
        args = self.argv(result)
        for path in (marker, writable_git_dir):
            self.assertTrue(any(
                f'{json.dumps(str(path))}="read"' in argument for argument in args),
                (path, args, result.stderr))

    def test_linked_launch_protects_initialized_submodule_pointer_and_target(self):
        submodule = self.add_submodule(self.linked, "linked-sub")
        marker = submodule / ".git"
        original_git_dir = Path(self.git(
            "rev-parse", "--absolute-git-dir", cwd=submodule).stdout.strip()).resolve()
        writable_git_dir = self.linked / "writable-submodule-git"
        original_git_dir.rename(writable_git_dir)
        marker.write_text(f"gitdir: {writable_git_dir}\n")
        self.git(
            "config", "--file", writable_git_dir / "config", "core.worktree",
            submodule, cwd=self.base)
        result = self.run_wrapper("--version", cwd=self.linked)
        args = self.argv(result)
        for path in (marker, writable_git_dir):
            self.assertTrue(any(
                f'{json.dumps(str(path))}="read"' in argument for argument in args),
                (path, args, result.stderr))

    def test_arbitrary_external_submodule_git_directory_fails_closed(self):
        submodule = self.add_submodule(self.main, "external-sub")
        marker = submodule / ".git"
        original_git_dir = Path(self.git(
            "rev-parse", "--absolute-git-dir", cwd=submodule).stdout.strip()).resolve()
        external_git_dir = self.base / "external-submodule-git"
        original_git_dir.rename(external_git_dir)
        marker.write_text(f"gitdir: {external_git_dir}\n")
        self.git(
            "config", "--file", external_git_dir / "config", "core.worktree",
            submodule, cwd=self.base)
        result = self.run_wrapper("--version", cwd=self.main)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("escapes the checkout and common modules", result.stderr)
        self.assertFalse((self.main / ".git" / "config.worktree").exists())

    def test_peer_writable_submodule_ancestor_fails_read_only(self):
        peer = self.main / "peer-submodule"
        peer.mkdir()
        self.add_submodule(peer, "sub")
        peer.chmod(0o777)
        try:
            result = self.run_wrapper("--version", cwd=self.main)
        finally:
            peer.chmod(0o700)
        self.assertEqual(
            self.argv(result), ["--sandbox", "read-only", "--version"])
        self.assertIn("writable by another user", result.stderr)

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
        sandbox_tmp = self.base / "sandbox-tmp"
        sandbox_tmp.mkdir()
        env["TMPDIR"] = str(sandbox_tmp)
        result = subprocess.run(
            [str(installed_codex()), "--strict-config", *extra, "doctor", "--json"],
            cwd=self.linked, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        report = json.loads(result.stdout)
        self.assertEqual(
            report["checks"]["config.load"]["status"], "ok",
            json.dumps(report["checks"]["config.load"], indent=2))

    @unittest.skipUnless(installed_codex(), "Codex CLI is not installed")
    def test_installed_codex_strictly_accepts_fleet_profile(self):
        projects = self.base / "projects"
        child = projects / "child"
        projects.mkdir()
        self.git("init", "-q", child, cwd=self.base)
        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            extra = self.fleet_arguments(wrapper, projects, access)
        env = self.env()
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        result = subprocess.run(
            [str(installed_codex()), "--strict-config", *extra, "doctor", "--json"],
            cwd=projects, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        report = json.loads(result.stdout)
        self.assertEqual(
            report["checks"]["config.load"]["status"], "ok",
            json.dumps(report["checks"]["config.load"], indent=2))

    @unittest.skipUnless(installed_codex(), "Codex CLI is not installed")
    def test_installed_codex_sandbox_enforces_linked_git_guards(self):
        self.git("config", "extensions.worktreeConfig", "true", cwd=self.main)
        hooks = self.linked / ".githooks"
        hooks.mkdir()
        self.git(
            "config", "--worktree", "core.hooksPath", ".githooks", cwd=self.linked)
        policy = self.linked / ".codex" / "config.toml"
        policy.parent.mkdir()
        policy.write_text('default_permissions = "git-workspace"\n')
        launch = self.run_wrapper("--version", cwd=self.linked)
        extra = self.argv(launch)[:-1]
        script = textwrap.dedent('''\
            attempt() { label="$1"; shift; if "$@" 2>/dev/null; then
              printf '%s=allowed\\n' "$label"
            else
              printf '%s=denied\\n' "$label"
            fi; }
            attempt object /usr/bin/touch "$1/.git/objects/linked-probe"
            attempt config /bin/sh -c 'printf x >> "$1"' sh "$1/.git/config"
            attempt hook /usr/bin/touch "$1/.git/hooks/new-hook"
            attempt newworktree /usr/bin/mkdir "$1/.git/worktrees/new"
            attempt marker /bin/sh -c 'printf x >> "$1"' sh "$2/.git"
            attempt adminconfig /bin/sh -c 'printf x >> "$1"' sh "$1/.git/worktrees/linked/config.worktree"
            attempt adminhead /bin/sh -c 'printf x >> "$1"' sh "$1/.git/worktrees/linked/HEAD"
            attempt adminscratch /usr/bin/touch "$1/.git/worktrees/linked/allowed-probe"
            attempt policy /bin/sh -c 'printf x >> "$1"' sh "$2/.codex/config.toml"
            attempt hooksource /usr/bin/touch "$2/.githooks/new-hook"
            attempt source /usr/bin/touch "$2/source-probe"
            attempt mainsource /usr/bin/touch "$1/main-source-probe"
            ''')
        env = self.env()
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        sandbox_tmp = self.base / "linked-sandbox-runtime-tmp"
        sandbox_tmp.mkdir()
        env["TMPDIR"] = str(sandbox_tmp)
        result = subprocess.run(
            [str(installed_codex()), *extra, "sandbox", "--",
             "/bin/sh", "-c", script, "probe", str(self.main), str(self.linked)],
            cwd=self.linked, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcomes = {
            line.strip() for line in result.stdout.splitlines()
            if "=" in line and line.split("=", 1)[0] in {
                "object", "config", "hook", "newworktree", "marker",
                "adminconfig", "adminhead", "adminscratch", "policy",
                "hooksource", "source", "mainsource"}
        }
        self.assertEqual(outcomes, {
            "object=allowed", "config=denied", "hook=denied",
            "newworktree=denied", "marker=denied", "adminconfig=denied",
            "adminhead=allowed", "adminscratch=allowed", "policy=denied", "hooksource=denied",
            "source=allowed", "mainsource=denied",
        })

    @unittest.skipUnless(installed_codex(), "Codex CLI is not installed")
    def test_installed_codex_sandbox_enforces_ordinary_git_guards(self):
        launch = self.run_wrapper("--version", cwd=self.main)
        extra = self.argv(launch)[:-1]
        config_before = (self.main / ".git" / "config").read_bytes()
        script = textwrap.dedent('''\
            attempt() { label="$1"; shift; if "$@" 2>/dev/null; then
              printf '%s=allowed\\n' "$label"
            else
              printf '%s=denied\\n' "$label"
            fi; }
            attempt object /usr/bin/touch "$1/.git/objects/allowed-probe"
            attempt config /bin/sh -c 'printf x >> "$1"' sh "$1/.git/config"
            attempt hooks /usr/bin/touch "$1/.git/hooks/new-hook"
            attempt worktrees /usr/bin/mkdir "$1/.git/worktrees/new"
            attempt source /usr/bin/touch "$1/source-probe"
            ''')
        env = self.env()
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        sandbox_tmp = self.base / "ordinary-sandbox-runtime-tmp"
        sandbox_tmp.mkdir()
        env["TMPDIR"] = str(sandbox_tmp)
        result = subprocess.run(
            [str(installed_codex()), *extra, "sandbox", "--",
             "/bin/sh", "-c", script, "probe", str(self.main)],
            cwd=self.main, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcomes = {
            line.strip() for line in result.stdout.splitlines()
            if "=" in line and line.split("=", 1)[0] in {
                "object", "config", "hooks", "worktrees", "source"}
        }
        self.assertEqual(outcomes, {
            "object=allowed", "config=denied", "hooks=denied",
            "worktrees=denied", "source=allowed",
        })
        self.assertEqual((self.main / ".git" / "config").read_bytes(), config_before)

    @unittest.skipUnless(installed_codex(), "Codex CLI is not installed")
    def test_installed_codex_offline_sandbox_runs_node_workspace_git(self):
        policy = self.main / ".codex" / "config.toml"
        policy.parent.mkdir()
        policy.write_text('default_permissions = "git-workspace-offline"\n')
        launch = self.run_wrapper("--version", cwd=self.main)
        extra = self.argv(launch)[:-1]
        module = ROOT / "lib" / "workspace-git.mjs"
        script = (
            f'import {{ git, stdout }} from {json.dumps(str(module))}; '
            'console.log(stdout(git(["rev-parse", "--show-toplevel"])))')
        env = self.env()
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        sandbox_tmp = self.base / "offline-node-sandbox-runtime-tmp"
        sandbox_tmp.mkdir()
        env["TMPDIR"] = str(sandbox_tmp)

        result = subprocess.run(
            [str(installed_codex()), *extra, "sandbox", "--", "node",
             "--input-type=module", "-e", script],
            cwd=self.main, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.main))
        self.assertEqual(list(self.main.glob(".workspace-command-*")), [])

    @unittest.skipUnless(installed_codex(), "Codex CLI is not installed")
    def test_installed_codex_sandbox_enforces_fleet_git_guards(self):
        projects = self.base / "projects"
        enabled = projects / "enabled"
        disabled = projects / "disabled"
        deep_disabled = projects / "a" / "b" / "c" / "d" / "repo"
        submodule_source = self.base / "submodule-source"
        nested_source = self.base / "nested-source"
        separate_source = projects / "separate-source"
        separate_control = projects / "separate-control"
        projects.mkdir()
        self.git("init", "-q", enabled, cwd=self.base)
        self.git("init", "-q", disabled, cwd=self.base)
        self.git("init", "-q", deep_disabled, cwd=self.base)
        self.git("init", "-q", submodule_source, cwd=self.base)
        self.git("init", "-q", nested_source, cwd=self.base)
        for repository in (submodule_source, nested_source):
            self.git("config", "user.email", "test@example.com", cwd=repository)
            self.git("config", "user.name", "Test", cwd=repository)
            (repository / "tracked").write_text("one\n")
            self.git("add", "tracked", cwd=repository)
            self.git("commit", "-qm", "initial", cwd=repository)
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            nested_source, "nested", cwd=submodule_source)
        self.git("commit", "-qam", "add nested submodule", cwd=submodule_source)
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            submodule_source, "sub", cwd=enabled)
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "update",
            "--init", "--recursive", "-q", cwd=enabled)
        initialized_submodule = enabled / "sub"
        initialized_nested = initialized_submodule / "nested"
        for checkout in (initialized_submodule, initialized_nested):
            (checkout / ".githooks").mkdir()
            self.git("config", "core.hooksPath", ".githooks", cwd=checkout)
        (enabled / ".githooks").mkdir()
        self.git("config", "core.hooksPath", ".githooks", cwd=enabled)
        local = disabled / ".codex" / "config.toml"
        local.parent.mkdir()
        local.write_text('default_permissions = "no-git"\n')
        (disabled / ".githooks").mkdir()
        self.git("config", "core.hooksPath", ".githooks", cwd=disabled)
        deep_local = deep_disabled / ".codex" / "config.toml"
        deep_local.parent.mkdir()
        deep_local.write_text('default_permissions = "no-git"\n')
        (enabled / "node_modules").mkdir()
        self.git(
            "init", "-q", "--separate-git-dir", separate_control,
            separate_source, cwd=self.base)
        (separate_source / ".githooks").mkdir()
        self.git("config", "core.hooksPath", ".githooks", cwd=separate_source)
        wrapper = load_wrapper()
        access = load_access()
        with mock.patch.dict(os.environ, {
                "HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}):
            extra = self.fleet_arguments(wrapper, projects, access)
        config_before = (enabled / ".git" / "config").read_bytes()
        script = textwrap.dedent('''\
            attempt() { label="$1"; shift; if "$@" 2>/dev/null; then
              printf '%s=allowed\\n' "$label"
            else
              printf '%s=denied\\n' "$label"
            fi; }
            attempt object /usr/bin/touch "$1/.git/objects/allowed-probe"
            attempt config /bin/sh -c 'printf x >> "$1"' sh "$1/.git/config"
            attempt hooks /usr/bin/touch "$1/.git/hooks/new-hook"
            attempt worktrees /usr/bin/mkdir "$1/.git/worktrees/new"
            attempt alternates /bin/sh -c 'printf x >> "$1"' sh "$1/.git/objects/info/alternates"
            attempt nested /bin/sh -c 'printf x >> "$1"' sh "$1/sub/.git"
            attempt nested2 /bin/sh -c 'printf x >> "$1"' sh "$1/sub/nested/.git"
            attempt hooksource /usr/bin/touch "$1/.githooks/new-hook"
            attempt subhook /usr/bin/touch "$1/sub/.githooks/new-hook"
            attempt nestedhook /usr/bin/touch "$1/sub/nested/.githooks/new-hook"
            attempt generated /usr/bin/touch "$1/node_modules/probe"
            attempt disabled /usr/bin/touch "$2/.git/objects/disabled-probe"
            attempt disabledhook /usr/bin/touch "$2/.githooks/new-hook"
            attempt disabledpolicy /bin/sh -c 'printf x >> "$1"' sh "$2/.codex/config.toml"
            attempt deep /usr/bin/touch "$3/.git/objects/deep-probe"
            attempt deeppolicy /bin/sh -c 'printf x >> "$1"' sh "$3/.codex/config.toml"
            attempt separatecontrol /usr/bin/touch "$5/objects/probe"
            attempt separatehook /usr/bin/touch "$4/.githooks/new-hook"
            attempt separatesource /usr/bin/touch "$4/source-probe"
            ''')
        env = self.env()
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        sandbox_tmp = self.base / "sandbox-runtime-tmp"
        sandbox_tmp.mkdir()
        env["TMPDIR"] = str(sandbox_tmp)
        result = subprocess.run(
            [str(installed_codex()), *extra, "sandbox", "--",
             "/bin/sh", "-c", script, "probe", str(enabled), str(disabled),
             str(deep_disabled), str(separate_source), str(separate_control)],
            cwd=projects, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcomes = {
            line.strip() for line in result.stdout.splitlines()
            if "=" in line and line.split("=", 1)[0] in {
                "object", "config", "hooks", "worktrees", "alternates", "nested", "nested2",
                "hooksource", "subhook", "nestedhook", "generated", "disabled",
                "disabledhook", "disabledpolicy", "deep", "deeppolicy",
                "separatecontrol", "separatehook", "separatesource"}
        }
        self.assertEqual(outcomes, {
            "object=allowed", "config=denied", "hooks=denied", "worktrees=denied",
            "alternates=denied", "nested=denied", "nested2=denied", "hooksource=denied",
            "subhook=denied", "nestedhook=denied", "generated=allowed",
            "disabled=denied", "disabledhook=denied",
            "disabledpolicy=denied", "deep=denied", "deeppolicy=denied",
            "separatecontrol=denied", "separatehook=denied", "separatesource=allowed",
        })
        self.assertEqual((enabled / ".git" / "config").read_bytes(), config_before)
        self.assertTrue((enabled / ".git" / "objects" / "allowed-probe").exists())
        self.assertFalse((disabled / ".git" / "objects" / "disabled-probe").exists())
        self.assertFalse((deep_disabled / ".git" / "objects" / "deep-probe").exists())


if __name__ == "__main__":
    unittest.main()
