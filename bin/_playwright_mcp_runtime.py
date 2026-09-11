"""Install and validate the fixed, integrity-locked Playwright MCP runtime."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


RUNTIME_NAME = "playwright-mcp-runtime"
MIN_NODE_VERSION = (20, 0, 0)


class RuntimeInstallError(Exception):
    """The locked runtime is absent, unsafe, or could not be installed."""


def _version_tuple(raw: object) -> tuple[int, ...] | None:
    if not isinstance(raw, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", raw):
        return None
    return tuple(int(part) for part in raw.split("."))


def _require_private_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeInstallError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeInstallError(f"{label} is not a regular directory: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeInstallError(
            f"{label} is not owned by the current user: {path}"
        )
    actual = stat.S_IMODE(info.st_mode)
    if actual != 0o700:
        raise RuntimeInstallError(
            f"{label} must have mode 700, not {actual:o}: {path}"
        )


def _trusted_tool(path: Path, label: str, *, executable: bool) -> Path:
    try:
        raw_info = path.lstat()
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise RuntimeInstallError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(raw_info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeInstallError(f"{label} is not a regular file: {resolved}")
    if executable and not os.access(resolved, os.X_OK):
        raise RuntimeInstallError(f"{label} is not executable: {resolved}")
    if info.st_uid not in (0, os.getuid()) or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeInstallError(
            f"{label} must be owned by root/current user and not "
            f"group/world-writable: {resolved}"
        )
    return resolved


def _node_version(node: Path, home: Path) -> tuple[int, ...] | None:
    result = subprocess.run(
        [str(node), "--version"],
        cwd=home,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    return _version_tuple(result.stdout.strip().removeprefix("v"))


def _validated_runtime_node(
    home: Path, raw_path: str
) -> tuple[Path, tuple[int, ...]]:
    path = Path(raw_path)
    allowed = path == Path("/usr/bin/node")
    if not allowed:
        nvm_root = home / ".nvm" / "versions" / "node"
        try:
            relative = path.relative_to(nvm_root)
        except ValueError:
            relative = None
        allowed = (
            relative is not None
            and len(relative.parts) == 3
            and re.fullmatch(r"v\d+(?:\.\d+){0,3}", relative.parts[0]) is not None
            and relative.parts[1:] == ("bin", "node")
        )
    if not allowed:
        raise RuntimeInstallError(
            f"runtime Node path is outside trusted locations: {path}"
        )
    node = _trusted_tool(path, "runtime Node executable", executable=True)
    version = _node_version(node, home)
    padded = (version or ()) + (0,) * (len(MIN_NODE_VERSION) - len(version or ()))
    if version is None or padded < MIN_NODE_VERSION:
        raise RuntimeInstallError(
            f"runtime Node must be version 20 or newer: {node}"
        )
    return node, version


def _resolve_node_toolchain(home: Path) -> tuple[Path, Path]:
    candidates = [Path("/usr/bin/node")]
    candidates.extend((home / ".nvm" / "versions" / "node").glob("v*/bin/node"))
    viable: list[tuple[tuple[int, ...], Path]] = []
    for candidate in candidates:
        try:
            node, version = _validated_runtime_node(home, str(candidate))
        except RuntimeInstallError:
            continue
        viable.append((version, node))
    if not viable:
        raise RuntimeInstallError(
            "no trusted Node 20+ installation found in /usr/bin or ~/.nvm"
        )
    _, node = max(viable)
    if node == Path("/usr/bin/node"):
        npm_candidate = Path("/usr/bin/npm").resolve()
    else:
        npm_candidate = (
            node.parent.parent / "lib" / "node_modules" / "npm" /
            "bin" / "npm-cli.js"
        )
    npm = _trusted_tool(npm_candidate, "trusted npm CLI", executable=False)
    return node, npm


def _runtime_source() -> Path:
    source = Path(__file__).resolve().parent.parent / "lib" / RUNTIME_NAME
    for name in ("package.json", "package-lock.json"):
        path = source / name
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise RuntimeInstallError(
                f"runtime lock input is missing: {path}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeInstallError(
                f"runtime lock input is not a regular file: {path}"
            )
    return source


def _locked_versions(source: Path) -> dict[str, str]:
    lock_file = source / "package-lock.json"
    try:
        lock = json.loads(lock_file.read_text(encoding="utf-8"))
        packages = lock["packages"]
        versions = {
            location: metadata["version"]
            for location, metadata in packages.items()
            if location.startswith("node_modules/")
        }
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeInstallError(
            f"runtime lockfile has an invalid package graph: {lock_file}"
        ) from exc
    if not versions or not all(
        isinstance(version, str) for version in versions.values()
    ):
        raise RuntimeInstallError(
            f"runtime lockfile has invalid package versions: {lock_file}"
        )
    return versions


def _runtime_problem(home: Path, runtime: Path) -> str | None:
    if not runtime.exists() and not runtime.is_symlink():
        return f"pinned runtime is missing: {runtime}"
    try:
        _require_private_directory(runtime, "pinned runtime directory")
    except RuntimeInstallError as exc:
        return str(exc)

    source = _runtime_source()
    for name in ("package.json", "package-lock.json"):
        installed_lock = runtime / name
        try:
            lock_info = installed_lock.lstat()
            if (
                stat.S_ISLNK(lock_info.st_mode)
                or not stat.S_ISREG(lock_info.st_mode)
            ):
                return f"pinned runtime {name} is not a regular file"
            if installed_lock.read_bytes() != (source / name).read_bytes():
                return f"pinned runtime {name} does not match the repository lock"
        except OSError:
            return f"cannot read pinned runtime {name}: {installed_lock}"

    node_file = runtime / "node-path"
    try:
        node_info = node_file.lstat()
        if (
            stat.S_ISLNK(node_info.st_mode)
            or not stat.S_ISREG(node_info.st_mode)
            or node_info.st_uid != os.getuid()
            or stat.S_IMODE(node_info.st_mode) != 0o600
        ):
            return (
                "pinned runtime Node path must be an owned mode-600 file: "
                f"{node_file}"
            )
        node_raw = node_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return f"cannot read pinned runtime Node path: {node_file}"
    if node_raw.endswith("\n"):
        node_raw = node_raw[:-1]
    if "\n" in node_raw or not node_raw:
        return f"pinned runtime Node path must contain one absolute path: {node_file}"
    try:
        _validated_runtime_node(home, node_raw)
    except RuntimeInstallError as exc:
        return str(exc)

    for location, expected in _locked_versions(source).items():
        metadata = runtime / location / "package.json"
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return f"cannot read installed package metadata: {metadata}"
        if data.get("version") != expected:
            return f"installed {location} version is not the locked {expected}"

    cli = runtime / "node_modules" / "@playwright" / "mcp" / "cli.js"
    try:
        info = cli.lstat()
    except FileNotFoundError:
        return f"pinned Playwright MCP CLI is missing: {cli}"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return f"pinned Playwright MCP CLI is not a regular file: {cli}"
    return None


def require_runtime(home: Path, runtime: Path) -> None:
    problem = _runtime_problem(home, runtime)
    if problem is not None:
        raise RuntimeInstallError(problem)


def install_runtime(home: Path, runtime: Path) -> bool:
    problem = _runtime_problem(home, runtime)
    if problem is None:
        return False
    if runtime.exists() or runtime.is_symlink():
        _require_private_directory(runtime, "pinned runtime directory")

    source = _runtime_source()
    parent = runtime.parent
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise RuntimeInstallError(
            f"cannot inspect runtime parent: {parent}"
        ) from exc
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
    ):
        raise RuntimeInstallError(
            f"runtime parent must be an owned regular directory: {parent}"
        )

    node, npm = _resolve_node_toolchain(home)
    staging = Path(tempfile.mkdtemp(prefix=f".{RUNTIME_NAME}-", dir=parent))
    staging.chmod(0o700)
    backup = parent / f".{RUNTIME_NAME}.previous-{os.getpid()}"
    if backup.exists() or backup.is_symlink():
        shutil.rmtree(staging)
        raise RuntimeInstallError(f"stale runtime backup blocks repair: {backup}")

    try:
        shutil.copyfile(source / "package.json", staging / "package.json")
        shutil.copyfile(source / "package-lock.json", staging / "package-lock.json")
        node_file = staging / "node-path"
        node_file.write_text(str(node) + "\n", encoding="utf-8")
        node_file.chmod(0o600)
        npm_env = {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        result = subprocess.run(
            [
                str(node),
                str(npm),
                "ci",
                "--ignore-scripts",
                "--omit=dev",
                "--no-audit",
                "--no-fund",
                "--registry=https://registry.npmjs.org/",
                "--userconfig=/dev/null",
                f"--cache={home / '.npm'}",
            ],
            cwd=staging,
            env=npm_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            detail = next(
                (
                    line.strip()
                    for line in reversed(result.stderr.splitlines())
                    if line.strip()
                ),
                "no npm diagnostic",
            )
            raise RuntimeInstallError(
                f"locked runtime install failed (exit {result.returncode}): {detail}"
            )
        require_runtime(home, staging)

        moved_old = False
        installed_new = False
        try:
            if runtime.exists():
                os.replace(runtime, backup)
                moved_old = True
            os.replace(staging, runtime)
            installed_new = True
            if moved_old:
                shutil.rmtree(backup)
        except OSError as exc:
            if installed_new and runtime.exists():
                shutil.rmtree(runtime)
            if moved_old and backup.exists():
                os.replace(backup, runtime)
            raise RuntimeInstallError(
                f"cannot atomically replace pinned runtime: {runtime}"
            ) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return True
