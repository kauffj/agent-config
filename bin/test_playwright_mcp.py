#!/usr/bin/env python3
"""Hermetic tests for the shared Playwright MCP runtime wrapper."""

import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
import tomllib
import unittest
from unittest import mock
from pathlib import Path

import _playwright_mcp_runtime as RUNTIME


WRAPPER = Path(__file__).with_name("playwright-mcp")
INSTALLER = Path(__file__).with_name("playwright-mcp-install")
SECRET = "Abcdefghijklmnopqrstuvwxyz0123456789_-ABCDE"
VALID_TOKEN = "Abcdefghijklmnopqrstuvwxyz0123456789_-ABCDE"
ROUTING_VARS = (
    "BROWSER",
    "CLAUDE_ACCT",
    "CLAUDE_ACCT_BROWSER_PROFILE",
    "CLAUDE_CONFIG_DIR",
)
def write_test_node_toolchain(home):
    actual_node = Path(shutil.which("node") or "").resolve()
    if not actual_node.is_file():
        raise RuntimeError("tests require an installed Node executable")
    version_root = home / ".nvm" / "versions" / "node" / "v24.14.0"
    node = version_root / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text(
        "#!/bin/sh\nexec " + shlex.quote(str(actual_node)) + ' "$@"\n'
    )
    node.chmod(0o755)
    npm = version_root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm.parent.mkdir(parents=True)
    npm.write_text("// fake npm entrypoint; subprocess is mocked in install tests\n")
    npm.chmod(0o644)
    return node, npm


class PlaywrightMcpWrapperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.capture = self.home / "capture.json"
        self.token_dir = self.home / ".config" / "playwright-mcp"
        self.token_file = self.token_dir / "extension-token"
        self.profile = (self.home / ".config" / "BraveSoftware" /
                        "Brave-Browser")
        self.output_dir = self.home / ".local" / "state" / "playwright-mcp"
        self.runtime = self.home / ".local" / "share" / "playwright-mcp-runtime"
        self.runtime_node, _ = write_test_node_toolchain(self.home)

        self.fake_bin.mkdir()
        self.token_dir.mkdir(parents=True, mode=0o700)
        self.token_dir.chmod(0o700)
        self.profile.mkdir(parents=True)
        self.write_token()

        cli = self.runtime / "node_modules" / "@playwright" / "mcp" / "cli.js"
        cli.parent.mkdir(parents=True)
        self.runtime.chmod(0o700)
        node_file = self.runtime / "node-path"
        node_file.write_text(str(self.runtime_node) + "\n")
        node_file.chmod(0o600)
        cli.write_text(textwrap.dedent("""\
            const fs = require("fs");
            const names = [
              "BROWSER",
              "CLAUDE_ACCT",
              "CLAUDE_ACCT_BROWSER_PROFILE",
              "CLAUDE_CONFIG_DIR",
              "_PLAYWRIGHT_MCP_EXECUTABLE_PATH",
              "NODE_OPTIONS",
              "NODE_PATH",
              "NPM_CONFIG_REGISTRY",
              "BASH_ENV",
              "SAFE_MARKER",
            ];
            const payload = {
              argv: process.argv.slice(2),
              token: process.env.PLAYWRIGHT_MCP_EXTENSION_TOKEN,
              present: names.filter(name => name in process.env),
              cwd: process.cwd(),
            };
            fs.writeFileSync(process.env.HOME + "/capture.json",
                             JSON.stringify(payload));
            """))

        hostile_node = self.fake_bin / "node"
        hostile_node.write_text("#!/bin/sh\nexit 99\n")
        hostile_node.chmod(0o755)
        hostile_bash = self.fake_bin / "bash"
        hostile_bash.write_text("#!/bin/sh\nexit 98\n")
        hostile_bash.chmod(0o755)
        self.hostile_bash_env = self.root / "bash-env"
        self.hostile_bash_env.write_text("exit 97\n")

    def tearDown(self):
        self.temp.cleanup()

    def write_token(self, value=SECRET, mode=0o600, path=None):
        path = path or self.token_file
        path.write_text(value + "\n")
        path.chmod(mode)
        return path

    def env(self, **updates):
        env = {
            "HOME": str(self.home),
            "PATH": str(self.fake_bin) + ":/usr/bin:/bin",
            "BASH_ENV": str(self.hostile_bash_env),
        }
        env.update(updates)
        return env

    def run_wrapper(self, env=None):
        return subprocess.run(
            [str(WRAPPER)],
            env=env or self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def captured(self):
        return json.loads(self.capture.read_text())

    def assert_secret_not_reported(self, result):
        self.assertNotIn(SECRET, result.stdout)
        self.assertNotIn(SECRET, result.stderr)

    def test_pinned_invocation_uses_home_defaults_and_fixed_browser(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.captured()
        self.assertEqual(payload["argv"], [
            "--extension",
            "--executable-path=/usr/bin/brave-browser",
            f"--user-data-dir={self.profile}",
            f"--output-dir={self.output_dir}",
            "--output-max-size=10485760",
        ])
        self.assertEqual(payload["token"], SECRET)
        self.assertEqual(payload["cwd"], str(self.runtime))
        self.assertNotIn(SECRET, " ".join(payload["argv"]))
        self.assert_secret_not_reported(result)

    def test_production_executable_default_is_brave(self):
        self.assertIn(
            'browser_exe="/usr/bin/brave-browser"',
            WRAPPER.read_text(),
        )

    def test_browser_executable_is_immutable(self):
        result = self.run_wrapper(self.env(
            _PLAYWRIGHT_MCP_EXECUTABLE_PATH="/untrusted/project/browser"
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.captured()
        self.assertIn("--executable-path=/usr/bin/brave-browser", payload["argv"])
        self.assertIn(f"--user-data-dir={self.profile}", payload["argv"])
        self.assertIn(f"--output-dir={self.output_dir}", payload["argv"])
        self.assertNotIn("_PLAYWRIGHT_MCP_EXECUTABLE_PATH", payload["present"])
        self.assert_secret_not_reported(result)

    def test_output_falls_back_to_private_state_directory(self):
        expected = self.home / ".local" / "state" / "playwright-mcp"
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--output-dir={expected}", self.captured()["argv"])
        self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o700)

    def test_output_uses_xdg_runtime_directory(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        expected = runtime / "playwright-mcp"
        env = self.env(XDG_RUNTIME_DIR=str(runtime))
        result = self.run_wrapper(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--output-dir={expected}", self.captured()["argv"])
        self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o700)

    def test_output_symlink_path_refuses_before_runtime(self):
        real = self.root / "real-output"
        real.mkdir(mode=0o700)
        state = self.home / ".local" / "state"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.symlink_to(real, target_is_directory=True)
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains a symlink", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assert_secret_not_reported(result)

    def test_public_output_directory_refuses(self):
        self.output_dir.mkdir(mode=0o755, parents=True)
        self.output_dir.chmod(0o755)
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output directory must have mode 700", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assert_secret_not_reported(result)

    def test_runtime_environment_is_an_explicit_allowlist(self):
        dirty = {name: "must-not-reach-runtime" for name in ROUTING_VARS}
        dirty.update({
            "SAFE_MARKER": "must-not-reach-node",
            "NODE_OPTIONS": "--require=/untrusted/project/hook.js",
            "NODE_PATH": "/untrusted/project/node_modules",
            "NPM_CONFIG_REGISTRY": "https://untrusted.invalid/",
        })
        dirty["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = "stale-token"
        result = self.run_wrapper(self.env(**dirty))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.captured()
        self.assertFalse(set(ROUTING_VARS) & set(payload["present"]))
        self.assertFalse({
            "SAFE_MARKER", "NODE_OPTIONS", "NODE_PATH", "NPM_CONFIG_REGISTRY",
            "BASH_ENV",
        } & set(payload["present"]))
        self.assertEqual(payload["token"], SECRET)
        self.assert_secret_not_reported(result)

    def test_missing_token_refuses_without_disclosing_token(self):
        self.token_file.unlink()
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or is not a regular file", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assert_secret_not_reported(result)

    def test_group_readable_token_refuses_without_disclosure(self):
        self.token_file.chmod(0o640)
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must have mode 600", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assert_secret_not_reported(result)

    def test_symlink_token_refuses(self):
        target = self.root / "real-token"
        self.write_token(path=target)
        self.token_file.unlink()
        self.token_file.symlink_to(target)
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or is not a regular file", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assert_secret_not_reported(result)

    def test_public_token_directory_refuses(self):
        self.token_dir.chmod(0o755)
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must have mode 700", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assert_secret_not_reported(result)

    def test_malformed_token_refuses(self):
        for value in (
            "",
            "A" * 42,
            "A" * 44,
            "first\nsecond",
            "PLAYWRIGHT_MCP_EXTENSION_TOKEN=" + SECRET,
        ):
            with self.subTest(value=value):
                self.write_token(value=value)
                result = self.run_wrapper()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("token file must contain", result.stderr)
                self.assertFalse(self.capture.exists())
                self.assert_secret_not_reported(result)

    def test_missing_profile_refuses_before_runtime(self):
        self.profile.rmdir()
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("browser user-data directory is missing", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assert_secret_not_reported(result)

    def test_token_permissions_fixture_is_exact(self):
        self.assertEqual(stat.S_IMODE(self.token_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)


class PlaywrightMcpInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.log = self.root / "client-log.jsonl"
        self.home.mkdir()
        self.fake_bin.mkdir()

        self.stable_wrapper = (
            self.home / ".config" / "agent-config" / "bin" / "playwright-mcp"
        )
        self.stable_wrapper.parent.mkdir(parents=True)
        self.stable_wrapper.write_text("#!/bin/sh\nexit 0\n")
        self.stable_wrapper.chmod(0o755)
        self.wrapper = self.stable_wrapper
        self.runtime_node, self.runtime_npm = write_test_node_toolchain(self.home)
        self.runtime = (
            self.home / ".local" / "share" / "playwright-mcp-runtime"
        )
        self.write_runtime(self.runtime)

        self.token_dir = self.home / ".config" / "playwright-mcp"
        self.token_file = self.token_dir / "extension-token"
        self.write_token()
        self.write_extension("0.4.0")

        self.alt_one = self.root / "claude-one"
        self.alt_two = self.root / "claude-two"
        roster = [
            {"name": "main", "configDir": None},
            {"name": "one", "configDir": str(self.alt_one)},
            {"name": "two", "configDir": str(self.alt_two)},
        ]
        roster_path = self.home / ".claude" / "meta" / "accounts.json"
        roster_path.parent.mkdir(parents=True)
        roster_path.write_text(json.dumps(roster))
        self.roster_path = roster_path

        self.write_fake_clients()

    def tearDown(self):
        self.temp.cleanup()

    def write_token(self, value=VALID_TOKEN, mode=0o600):
        self.token_dir.mkdir(parents=True, exist_ok=True)
        self.token_dir.chmod(0o700)
        self.token_file.write_text(value + "\n")
        self.token_file.chmod(mode)

    def write_extension(self, version):
        root = (
            self.home / ".config" / "BraveSoftware" / "Brave-Browser" /
            "Default" / "Extensions" / "mmlmfjhmonkocbjadbfplnigmagldckm"
        )
        if root.exists():
            for manifest in root.glob("*/manifest.json"):
                manifest.unlink()
        manifest = root / f"{version}_0" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "name": "Playwright Extension",
            "version": version,
        }))
        self.extension_root = root

    def write_runtime(self, root):
        source = WRAPPER.parent.parent / "lib" / "playwright-mcp-runtime"
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root.chmod(0o700)
        shutil.copyfile(source / "package.json", root / "package.json")
        shutil.copyfile(source / "package-lock.json", root / "package-lock.json")
        node_file = root / "node-path"
        node_file.write_text(str(self.runtime_node) + "\n")
        node_file.chmod(0o600)
        lock = json.loads((source / "package-lock.json").read_text())
        versions = {
            package.removeprefix("node_modules/"): metadata["version"]
            for package, metadata in lock["packages"].items()
            if package.startswith("node_modules/")
        }
        for package, version in versions.items():
            metadata = root / "node_modules" / package / "package.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(json.dumps({"name": package, "version": version}))
        cli = root / "node_modules" / "@playwright" / "mcp" / "cli.js"
        cli.write_text("#!/usr/bin/env node\n")

    def write_executable(self, name, source):
        path = self.fake_bin / name
        path.write_text(textwrap.dedent(source))
        path.chmod(0o755)
        return path

    def write_fake_clients(self):
        self.write_executable("brave-browser", """\
            #!/usr/bin/python3
            import json, os, pathlib, sys
            with open(os.environ["FAKE_CLIENT_LOG"], "a") as stream:
                stream.write(json.dumps({
                "kind": "brave",
                "argv": sys.argv[1:],
            }) + "\\n")
            manifest = os.environ.get("FAKE_BRAVE_INSTALL_EXTENSION_MANIFEST")
            if manifest:
                path = pathlib.Path(manifest)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    "name": "Playwright Extension", "version": "0.4.0"
                }))
        """)
        self.write_executable("claude", """\
            #!/usr/bin/python3
            import json, os, pathlib, sys

            argv = sys.argv[1:]
            selected = os.environ.get("CLAUDE_CONFIG_DIR")
            config = (pathlib.Path(selected) / ".claude.json"
                      if selected else pathlib.Path(os.environ["HOME"]) / ".claude.json")
            entry = {
                "kind": "claude",
                "argv": argv,
                "config_dir_present": "CLAUDE_CONFIG_DIR" in os.environ,
                "config_dir": selected,
                "cwd": os.getcwd(),
                "token_present": "PLAYWRIGHT_MCP_EXTENSION_TOKEN" in os.environ,
            }
            with open(os.environ["FAKE_CLIENT_LOG"], "a") as stream:
                stream.write(json.dumps(entry) + "\\n")
            config.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(config.read_text()) if config.exists() else {}
            servers = data.setdefault("mcpServers", {})
            if argv[:2] == ["mcp", "add"]:
                wrapper = argv[-1]
                servers["playwright"] = {
                    "type": "stdio", "command": wrapper, "args": [], "env": {}
                }
                if os.environ.get("FAKE_CLAUDE_DRIFT"):
                    servers["playwright"]["args"] = ["--foreign-after-mutation"]
                collide_next = os.environ.get("FAKE_COLLIDE_NEXT")
                if collide_next and selected is None:
                    next_config = pathlib.Path(collide_next)
                    next_config.parent.mkdir(parents=True, exist_ok=True)
                    next_config.write_text(json.dumps({"mcpServers": {
                        "playwright": {
                            "type": "stdio",
                            "command": "/concurrent/foreign/server",
                            "args": [],
                            "env": {},
                        }
                    }}))
            elif argv[:2] == ["mcp", "remove"]:
                servers.pop("playwright", None)
            else:
                raise SystemExit(23)
            config.write_text(json.dumps(data))
        """)
        self.write_executable("codex", """\
            #!/usr/bin/python3
            import json, os, pathlib, sys

            argv = sys.argv[1:]
            entry = {
                "kind": "codex",
                "argv": argv,
                "cwd": os.getcwd(),
                "routing_present": [name for name in (
                    "BROWSER", "CLAUDE_ACCT", "CLAUDE_ACCT_BROWSER_PROFILE",
                    "CLAUDE_CONFIG_DIR") if name in os.environ],
                "codex_home": os.environ.get("CODEX_HOME"),
                "token_present": "PLAYWRIGHT_MCP_EXTENSION_TOKEN" in os.environ,
            }
            with open(os.environ["FAKE_CLIENT_LOG"], "a") as stream:
                stream.write(json.dumps(entry) + "\\n")
            config = pathlib.Path(os.environ["CODEX_HOME"]) / "config.toml"
            config.parent.mkdir(parents=True, exist_ok=True)
            if argv[:3] == ["mcp", "add", "playwright"]:
                wrapper = argv[-1]
                config.write_text(
                    "[mcp_servers.playwright]\\ncommand = " + json.dumps(wrapper) + "\\n"
                )
            elif argv[:3] == ["mcp", "remove", "playwright"]:
                config.write_text("")
            else:
                raise SystemExit(24)
        """)

    def env(self, **updates):
        env = {
            "HOME": str(self.home),
            "PATH": str(self.fake_bin),
            "FAKE_CLIENT_LOG": str(self.log),
            "BROWSER": "account-browser",
            "CLAUDE_ACCT": "caller-alt",
            "CLAUDE_ACCT_BROWSER_PROFILE": "caller-profile",
            "CLAUDE_CONFIG_DIR": str(self.root / "caller-config"),
            "PLAYWRIGHT_MCP_EXTENSION_TOKEN": "must-not-leak",
        }
        env.update(updates)
        return env

    def run_installer(self, *args, input_text=None, env=None):
        return subprocess.run(
            [str(INSTALLER), *args],
            env=env or self.env(),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def logs(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def clear_log(self):
        self.log.unlink(missing_ok=True)

    def claude_configs(self):
        return [
            self.home / ".claude.json",
            self.alt_one / ".claude.json",
            self.alt_two / ".claude.json",
        ]

    def expected_claude(self, wrapper=None):
        return {
            "type": "stdio",
            "command": str(wrapper or self.wrapper),
            "args": [],
            "env": {},
        }

    def seed_exact(self, wrapper=None):
        wrapper = wrapper or self.wrapper
        for config in self.claude_configs():
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({
                "mcpServers": {"playwright": self.expected_claude(wrapper)}
            }))
        codex = self.home / ".codex" / "config.toml"
        codex.parent.mkdir(parents=True, exist_ok=True)
        codex.write_text(
            "[mcp_servers.playwright]\ncommand = " + json.dumps(str(wrapper)) + "\n"
        )

    def assert_installed(self, wrapper=None):
        wrapper = wrapper or self.wrapper
        for config in self.claude_configs():
            server = json.loads(config.read_text())["mcpServers"]["playwright"]
            self.assertEqual(server, self.expected_claude(wrapper))
        codex = tomllib.loads(
            (self.home / ".codex" / "config.toml").read_text()
        )
        self.assertEqual(
            codex["mcp_servers"]["playwright"], {"command": str(wrapper)}
        )

    def assert_secret_absent(self, result):
        combined = result.stdout + result.stderr
        self.assertNotIn(VALID_TOKEN, combined)
        self.assertNotIn("must-not-leak", combined)
        if self.log.exists():
            self.assertNotIn(VALID_TOKEN, self.log.read_text())
            self.assertNotIn("must-not-leak", self.log.read_text())
        for config in self.claude_configs() + [self.home / ".codex" / "config.toml"]:
            if config.exists():
                self.assertNotIn(VALID_TOKEN, config.read_text())
                self.assertNotIn("must-not-leak", config.read_text())

    def test_install_targets_default_all_alts_and_codex(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed()
        logs = self.logs()
        claude = [entry for entry in logs if entry["kind"] == "claude"]
        codex = [entry for entry in logs if entry["kind"] == "codex"]
        self.assertEqual(len(claude), 3)
        self.assertFalse(claude[0]["config_dir_present"])
        self.assertIsNone(claude[0]["config_dir"])
        self.assertEqual(
            {entry["config_dir"] for entry in claude[1:]},
            {str(self.alt_one), str(self.alt_two)},
        )
        self.assertTrue(all(entry["argv"] == [
            "mcp", "add", "--scope", "user", "playwright", str(self.wrapper)
        ] for entry in claude))
        self.assertEqual(len(codex), 1)
        self.assertEqual(codex[0]["argv"], [
            "mcp", "add", "playwright", "--", str(self.wrapper)
        ])
        self.assertEqual(codex[0]["routing_present"], [])
        self.assertEqual(codex[0]["codex_home"], str(self.home / ".codex"))
        self.assertTrue(Path(codex[0]["cwd"]).name.startswith("playwright-mcp-install-"))
        self.assertFalse(Path(codex[0]["cwd"]).joinpath(".git").exists())
        self.assertTrue(all(
            not entry["token_present"]
            for entry in claude + codex
        ))
        self.assert_secret_absent(result)

    def test_install_is_idempotent(self):
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stderr)
        mutation_count = len(self.logs())
        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.logs()), mutation_count)
        self.assertIn("already exact", second.stdout)
        self.assert_installed()

    def test_collision_aborts_all_mutation(self):
        shutil.rmtree(self.runtime)
        config = self.home / ".claude.json"
        config.write_text(json.dumps({"mcpServers": {"playwright": {
            "type": "stdio", "command": "/someone/elses/server",
            "args": [], "env": {},
        }}}))
        before = config.read_text()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing all changes", result.stderr)
        self.assertEqual(config.read_text(), before)
        self.assertFalse((self.alt_one / ".claude.json").exists())
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.logs(), [])
        self.assert_secret_absent(result)

    def test_check_is_read_only_and_structural(self):
        self.seed_exact()
        files = self.claude_configs() + [self.home / ".codex" / "config.toml"]
        before = {path: path.read_bytes() for path in files}
        result = self.run_installer("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("structurally correct", result.stdout)
        self.assertEqual(self.logs(), [])
        self.assertEqual({path: path.read_bytes() for path in files}, before)

    def test_check_reports_missing_without_mutation(self):
        result = self.run_installer("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registration is missing", result.stderr)
        self.assertEqual(self.logs(), [])

    def test_installer_shebang_ignores_python_environment_injection(self):
        hostile = self.root / "hostile-python"
        marker = self.root / "python-environment-loaded"
        hostile.mkdir()
        (hostile / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('loaded')\n"
        )
        result = self.run_installer(
            "--check",
            env=self.env(
                PYTHONPATH=str(hostile),
                PYTHONHOME=str(self.root / "invalid-python-home"),
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registration is missing", result.stderr)
        self.assertFalse(marker.exists())

    def test_check_requires_locked_runtime_without_repairing_it(self):
        self.seed_exact()
        shutil.rmtree(self.runtime)
        result = self.run_installer("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned runtime is missing", result.stderr)
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.logs(), [])

    def test_check_rejects_runtime_that_no_longer_matches_the_lock(self):
        self.seed_exact()
        (self.runtime / "package-lock.json").write_text("{}\n")
        result = self.run_installer("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the repository lock", result.stderr)
        self.assertEqual(self.logs(), [])

    def test_install_runtime_uses_locked_npm_ci_in_private_staging(self):
        shutil.rmtree(self.runtime)
        commands = []

        def fake_npm(command, **kwargs):
            if command == [str(self.runtime_node), "--version"]:
                return subprocess.CompletedProcess(
                    command, 0, "v24.14.0\n", ""
                )
            commands.append((command, kwargs))
            self.write_runtime(Path(kwargs["cwd"]))
            return subprocess.CompletedProcess(command, 0, "", "")

        installer_subprocess = RUNTIME.subprocess
        with mock.patch.object(installer_subprocess, "run", side_effect=fake_npm):
            repaired = RUNTIME.install_runtime(self.home, self.runtime)

        self.assertTrue(repaired)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        self.assertIsNone(RUNTIME._runtime_problem(self.home, self.runtime))
        self.assertEqual(len(commands), 1)
        command, kwargs = commands[0]
        self.assertEqual(command[0], str(self.runtime_node))
        self.assertEqual(command[1], str(self.runtime_npm))
        self.assertIn("ci", command)
        self.assertIn("--ignore-scripts", command)
        self.assertIn("--registry=https://registry.npmjs.org/", command)
        self.assertNotEqual(Path(kwargs["cwd"]), Path.cwd())
        self.assertEqual(
            set(kwargs["env"]),
            {"HOME", "PATH", "LANG"},
        )

    def test_post_mutation_drift_is_reported_as_verification_failure(self):
        result = self.run_installer(env=self.env(FAKE_CLAUDE_DRIFT="1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("post-install verification failed", result.stderr)
        self.assertNotIn("refusing all changes", result.stderr)

    def test_each_registration_is_rechecked_immediately_before_mutation(self):
        next_config = self.alt_one / ".claude.json"
        result = self.run_installer(env=self.env(
            FAKE_COLLIDE_NEXT=str(next_config)
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registration changed before add", result.stderr)
        self.assertIn("/concurrent/foreign/server", next_config.read_text())
        mutations = [
            entry for entry in self.logs()
            if entry["kind"] in {"claude", "codex"}
        ]
        self.assertEqual(len(mutations), 1)

    def test_uninstall_removes_only_exact_and_preserves_browser_state(self):
        self.seed_exact()
        manifest = next(self.extension_root.glob("*/manifest.json"))
        result = self.run_installer("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for config in self.claude_configs():
            self.assertNotIn(
                "playwright", json.loads(config.read_text()).get("mcpServers", {})
            )
        codex = tomllib.loads(
            (self.home / ".codex" / "config.toml").read_text()
        )
        self.assertNotIn("playwright", codex.get("mcp_servers", {}))
        self.assertTrue(self.token_file.exists())
        self.assertTrue(manifest.exists())
        self.assertIn("Already-running clients", result.stdout)
        self.assertEqual(
            [entry["argv"][1] for entry in self.logs()],
            ["remove", "remove", "remove", "remove"],
        )

    def test_uninstall_collision_refuses_all_mutation(self):
        self.seed_exact()
        collision = self.alt_two / ".claude.json"
        data = json.loads(collision.read_text())
        data["mcpServers"]["playwright"]["args"] = ["--foreign"]
        collision.write_text(json.dumps(data))
        before = {path: path.read_bytes() for path in self.claude_configs()}
        result = self.run_installer("--uninstall")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing all changes", result.stderr)
        self.assertEqual(self.logs(), [])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_missing_codex_is_reported_and_claude_still_installs(self):
        (self.fake_bin / "codex").unlink()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex CLI not found; skipped Codex", result.stdout)
        self.assertTrue(all(entry["kind"] == "claude" for entry in self.logs()))
        for config in self.claude_configs():
            self.assertEqual(
                json.loads(config.read_text())["mcpServers"]["playwright"],
                self.expected_claude(),
            )

    def test_missing_claude_is_reported_and_codex_still_installs(self):
        (self.fake_bin / "claude").unlink()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Claude CLI not found; skipped Claude accounts", result.stdout)
        self.assertEqual([entry["kind"] for entry in self.logs()], ["codex"])

    def test_no_supported_client_refuses(self):
        (self.fake_bin / "claude").unlink()
        (self.fake_bin / "codex").unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no supported client found", result.stderr)
        self.assertEqual(self.logs(), [])

    def test_missing_roster_manages_default_claude_honestly(self):
        self.roster_path.unlink()
        (self.fake_bin / "codex").unlink()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("roster not found; managing only the default", result.stdout)
        self.assertEqual(len(self.logs()), 1)
        self.assertFalse(self.logs()[0]["config_dir_present"])

    def test_token_bootstrap_accepts_raw_and_full_assignment(self):
        for supplied in (VALID_TOKEN, f"PLAYWRIGHT_MCP_EXTENSION_TOKEN={VALID_TOKEN}"):
            with self.subTest(supplied=supplied[:15]):
                self.token_file.unlink(missing_ok=True)
                if self.token_dir.exists():
                    self.token_dir.rmdir()
                for config in self.claude_configs():
                    config.unlink(missing_ok=True)
                codex = self.home / ".codex" / "config.toml"
                codex.unlink(missing_ok=True)
                self.clear_log()
                result = self.run_installer(input_text=supplied + "\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.token_file.read_text(), VALID_TOKEN + "\n")
                self.assertEqual(stat.S_IMODE(self.token_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
                brave = [entry for entry in self.logs() if entry["kind"] == "brave"]
                self.assertEqual(len(brave), 1)
                self.assertEqual(brave[0]["argv"], [
                    "https://chromewebstore.google.com/detail/playwright-mcp-bridge/"
                    "mmlmfjhmonkocbjadbfplnigmagldckm",
                    "chrome-extension://mmlmfjhmonkocbjadbfplnigmagldckm/status.html",
                ])
                self.assert_secret_absent(result)

    def test_fresh_install_bootstraps_extension_before_registration(self):
        self.token_file.unlink()
        self.token_dir.rmdir()
        shutil.rmtree(self.extension_root)
        manifest = self.extension_root / "0.4.0_0" / "manifest.json"
        result = self.run_installer(
            input_text=VALID_TOKEN + "\n",
            env=self.env(FAKE_BRAVE_INSTALL_EXTENSION_MANIFEST=str(manifest)),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(manifest.exists())
        self.assert_installed()
        kinds = [entry["kind"] for entry in self.logs()]
        self.assertEqual(kinds[0], "brave")
        self.assertEqual(kinds[1:], ["claude", "claude", "claude", "codex"])
        self.assert_secret_absent(result)

    def test_existing_token_requires_extension_without_prompting(self):
        shutil.rmtree(self.extension_root)
        result = self.run_installer(input_text=VALID_TOKEN + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not installed in Brave's Default profile", result.stderr)
        self.assertEqual(self.logs(), [])
        self.assertFalse((self.home / ".claude.json").exists())

    def test_invalid_existing_token_refuses_without_overwrite_or_mutation(self):
        self.write_token("short")
        before = self.token_file.read_bytes()
        result = self.run_installer(input_text=VALID_TOKEN + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("43-character base64url token", result.stderr)
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assertEqual(self.logs(), [])

    def test_token_permissions_are_preflighted(self):
        self.token_file.chmod(0o640)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("token file must have mode 600", result.stderr)
        self.assertEqual(self.logs(), [])

    def test_uninstall_works_without_runtime_prerequisites(self):
        self.seed_exact()
        self.token_file.unlink()
        self.token_dir.rmdir()
        shutil.rmtree(self.extension_root)
        shutil.rmtree(self.runtime)
        self.wrapper.unlink()
        (self.fake_bin / "brave-browser").unlink()
        result = self.run_installer("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        for config in self.claude_configs():
            self.assertNotIn(
                "playwright", json.loads(config.read_text()).get("mcpServers", {})
            )
        codex = tomllib.loads(
            (self.home / ".codex" / "config.toml").read_text()
        )
        self.assertNotIn("playwright", codex.get("mcp_servers", {}))
        self.assertEqual(len(self.logs()), 4)

    def test_extension_minimum_version_is_preflighted(self):
        self.write_extension("0.3.9")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("too old", result.stderr)
        self.assertIn("0.4.0+", result.stderr)
        self.assertEqual(self.logs(), [])

    def test_codex_mutation_is_bound_to_the_inspected_home(self):
        hostile_home = self.root / "hostile-codex"
        hostile_config = hostile_home / "config.toml"
        hostile_home.mkdir()
        hostile_config.write_text(
            "[mcp_servers.playwright]\ncommand = \"/someone/elses/server\"\n"
        )
        before = hostile_config.read_bytes()
        result = self.run_installer(env=self.env(CODEX_HOME=str(hostile_home)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed(self.stable_wrapper)
        self.assertEqual(hostile_config.read_bytes(), before)
        codex = [entry for entry in self.logs() if entry["kind"] == "codex"]
        self.assertEqual(len(codex), 1)
        self.assertEqual(codex[0]["codex_home"], str(self.home / ".codex"))

    def test_malformed_roster_refuses_before_mutation(self):
        self.roster_path.write_text(json.dumps([{
            "name": "bad", "configDir": 42,
        }]))
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configDir must be a string or null", result.stderr)
        self.assertEqual(self.logs(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
