#!/usr/bin/env python3
"""Migrate Codex from legacy workspace sandboxing to Git-capable profiles.

The migration is deliberately narrow. It recognizes the workspace-write shape
used on this machine, preserves unrelated TOML source text, and refuses policy
it cannot translate without guessing.
"""

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tomllib

import _secure_fs as secure_fs
from _secure_fs import (
    Refusal,
    clear_inherited_access_acl,
    group_is_private,
    has_extended_access_acl,
    open_owned_directory,
    writable_by_peer,
)

# Preserve the public test and helper surface while the implementation lives at
# the neutral filesystem boundary shared by both migration tools.
grp = secure_fs.grp
pwd = secure_fs.pwd
_PRIVATE_GROUPS = secure_fs._PRIVATE_GROUPS


PROFILE_BLOCK = """\
# BEGIN managed by codex-git-access
[permissions.no-git-offline]
description = "Workspace editing with Git metadata read-only and network disabled."
extends = ":workspace"

[permissions.no-git-offline.filesystem]
":tmpdir" = "read"
":slash_tmp" = "read"

[permissions.no-git-offline.network]
enabled = false

[permissions.no-git]
description = "Workspace editing with Git metadata read-only and network enabled."
extends = "no-git-offline"

[permissions.no-git.network]
enabled = true

[permissions.git-workspace-offline]
description = "Workspace editing with Git metadata writable and network disabled."
extends = "no-git-offline"

[permissions.git-workspace-offline.filesystem.":workspace_roots"]
".git" = "write"
".git/config" = "read"
".git/config.worktree" = "read"
".git/hooks" = "read"
".git/modules" = "read"
".git/objects/info/alternates" = "read"
".git/objects/info/http-alternates" = "read"
".git/worktrees" = "read"

[permissions.git-workspace]
description = "Workspace editing with Git metadata writable and network enabled."
extends = "git-workspace-offline"

[permissions.git-workspace.network]
enabled = true
# END managed by codex-git-access
"""

MANAGED_PROFILES = tomllib.loads(PROFILE_BLOCK)["permissions"]
# The immediately previous managed profile protected only top-level Git
# config/hooks. Recognize it so `enable` can add the nested persistence guards.
PREVIOUS_MANAGED_PROFILES = copy.deepcopy(MANAGED_PROFILES)
for _path in (
        ".git/modules", ".git/objects/info/alternates",
        ".git/objects/info/http-alternates", ".git/worktrees"):
    del PREVIOUS_MANAGED_PROFILES["git-workspace-offline"]["filesystem"] \
        [":workspace_roots"][_path]
# The first shipped Git-capable profile made all of .git writable. Recognize
# that exact owned shape so `enable` can narrow existing installs safely.
LEGACY_MANAGED_PROFILES = copy.deepcopy(PREVIOUS_MANAGED_PROFILES)
LEGACY_MANAGED_PROFILES["git-workspace-offline"]["filesystem"] = {
    ":workspace_roots": {".git": "write"},
}
BEGIN_MARKER = "# BEGIN managed by codex-git-access"
END_MARKER = "# END managed by codex-git-access"
MANAGED_CHOICES = frozenset(MANAGED_PROFILES)
CONFIG_MAX_BYTES = 1024 * 1024
LEGACY_KEYS = frozenset({
    "network_access", "exclude_slash_tmp", "exclude_tmpdir_env_var",
})

ASSIGNMENT = r'(?m)^(?P<prefix>[ \t]*{key}[ \t]*=[ \t]*)"(?P<value>[^"]*)"(?P<suffix>[ \t]*(?:#.*)?(?:\n|$))'
TABLE_HEADER = re.compile(
    r'(?m)^[ \t]*(?:\[\[[^\]\n]+\]\]|\[[^\]\n]+\])[ \t]*(?:#.*)?(?:\n|$)')


def codex_home():
    value = os.environ.get("CODEX_HOME")
    path = Path(value).expanduser() if value else Path.home() / ".codex"
    if not path.is_absolute():
        raise Refusal("CODEX_HOME must be an absolute path")
    return path


def state_root():
    value = os.environ.get("XDG_STATE_HOME")
    base = Path(value).expanduser() if value else Path.home() / ".local" / "state"
    if not base.is_absolute():
        raise Refusal("XDG_STATE_HOME must be an absolute path")
    return base / "agent-config" / "backups"


def parse_toml(text, path):
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise Refusal(f"{path}: invalid TOML: {exc}") from exc


def read_config(fd, path):
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, CONFIG_MAX_BYTES - total + 1))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > CONFIG_MAX_BYTES:
            raise Refusal(f"{path}: config exceeds the safety limit")


def secure_read(path, *, missing_ok=False):
    path = Path(path)
    try:
        parent_fd, parent = open_owned_directory(path.parent)
    except Refusal as exc:
        if not (missing_ok and isinstance(exc.__cause__, FileNotFoundError)):
            raise
        grand_fd, grand = open_owned_directory(path.parent.parent)
        try:
            try:
                os.stat(path.parent.name, dir_fd=grand_fd, follow_symlinks=False)
            except FileNotFoundError:
                return {"path": path, "exists": False, "text": "", "data": b"",
                        "mode": 0o644, "identity": None, "parent_identity": None,
                        "grand_identity": (grand.st_dev, grand.st_ino),
                        "parent_missing": True}
            raise Refusal(f"{path.parent}: changed while checking the missing directory")
        finally:
            os.close(grand_fd)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if missing_ok:
                return {"path": path, "exists": False, "text": "", "data": b"",
                        "mode": 0o644, "identity": None,
                        "parent_identity": (parent.st_dev, parent.st_ino),
                        "grand_identity": None, "parent_missing": False}
            raise Refusal(f"{path}: file is missing")
        except OSError as exc:
            raise Refusal(f"{path}: cannot open safely: {exc.strerror or exc}") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or writable_by_peer(info, fd, path)):
                raise Refusal(
                    f"{path}: must be one user-owned file not writable by another user")
            data = read_config(fd, path)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal(f"{path}: config is not UTF-8") from exc
    return {"path": path, "exists": True, "text": text, "data": data,
            "mode": stat.S_IMODE(info.st_mode),
            "identity": (info.st_dev, info.st_ino),
            "parent_identity": (parent.st_dev, parent.st_ino),
            "grand_identity": None, "parent_missing": False}


def first_table_offset(text):
    match = TABLE_HEADER.search(text)
    return match.start() if match else len(text)


def assignment_match(text, key):
    regex = re.compile(ASSIGNMENT.format(key=re.escape(key)))
    matches = [m for m in regex.finditer(text) if m.start() < first_table_offset(text)]
    if len(matches) > 1:
        raise Refusal(f"duplicate top-level {key} assignments are not supported")
    return matches[0] if matches else None


def remove_assignment(text, key, expected=None):
    match = assignment_match(text, key)
    if match is None:
        raise Refusal(f"top-level {key} must use a simple quoted assignment")
    if expected is not None and match.group("value") != expected:
        raise Refusal(f"top-level {key} has an unsupported value")
    return text[:match.start()] + text[match.end():]


def set_assignment(text, key, value):
    match = assignment_match(text, key)
    if match:
        return (text[:match.start("value")] + value
                + text[match.end("value"):])
    offset = first_table_offset(text)
    before = text[:offset]
    after = text[offset:]
    if before and not before.endswith("\n"):
        before += "\n"
    if before and not before.endswith("\n\n"):
        before += "\n"
    line = ("# Selected by codex-git-access; trusted project config may override.\n"
            f'{key} = "{value}"\n\n')
    return before + line + after


def remove_legacy_table(text, path, parsed):
    table = parsed.get("sandbox_workspace_write")
    if table is None:
        raise Refusal(f"{path}: [sandbox_workspace_write] is required for migration")
    if not isinstance(table, dict) or set(table) - LEGACY_KEYS:
        unknown = sorted(set(table) - LEGACY_KEYS) if isinstance(table, dict) else []
        raise Refusal(f"{path}: unsupported sandbox_workspace_write keys: {unknown}")
    if any(not isinstance(value, bool) for value in table.values()):
        raise Refusal(f"{path}: legacy sandbox values must be booleans")
    header = re.compile(
        r'(?m)^[ \t]*\[sandbox_workspace_write\][ \t]*(?:#.*)?(?:\n|$)')
    matches = list(header.finditer(text))
    if len(matches) != 1:
        raise Refusal(
            f"{path}: sandbox_workspace_write must use one ordinary table")
    start = matches[0].start()
    next_header = TABLE_HEADER.search(text, matches[0].end())
    end = next_header.start() if next_header else len(text)
    return text[:start] + text[end:], table


def strip_legacy(text, path, parsed):
    if parsed.get("sandbox_mode") != "workspace-write":
        raise Refusal(f'{path}: only legacy sandbox_mode = "workspace-write" is migratable')
    text = remove_assignment(text, "sandbox_mode", "workspace-write")
    text, table = remove_legacy_table(text, path, parsed)
    return text, table


def expected_profile_choice(network, git):
    if git:
        return "git-workspace" if network else "git-workspace-offline"
    return "no-git" if network else "no-git-offline"


def validate_managed_profiles(parsed, path):
    permissions = parsed.get("permissions")
    if not isinstance(permissions, dict):
        raise Refusal(f"{path}: managed permission profiles are missing")
    for name, expected in MANAGED_PROFILES.items():
        if permissions.get(name) != expected:
            raise Refusal(f"{path}: permissions.{name} differs from the managed profile")
    choice = parsed.get("default_permissions")
    if choice not in MANAGED_CHOICES:
        raise Refusal(f"{path}: default_permissions is not a managed profile")
    if "sandbox_mode" in parsed or "sandbox_workspace_write" in parsed:
        raise Refusal(f"{path}: legacy and permission-profile settings cannot coexist")
    return choice


def validate_upgradeable_managed_profiles(parsed, path):
    """Accept the current owned profiles or the one older owned profile shape."""
    permissions = parsed.get("permissions")
    if not isinstance(permissions, dict):
        raise Refusal(f"{path}: managed permission profiles are missing")
    actual = {name: permissions.get(name) for name in MANAGED_CHOICES}
    if actual not in (
            MANAGED_PROFILES, PREVIOUS_MANAGED_PROFILES, LEGACY_MANAGED_PROFILES):
        raise Refusal(f"{path}: managed permission profiles differ from known versions")
    choice = parsed.get("default_permissions")
    if choice not in MANAGED_CHOICES:
        raise Refusal(f"{path}: default_permissions is not a managed profile")
    if "sandbox_mode" in parsed or "sandbox_workspace_write" in parsed:
        raise Refusal(f"{path}: legacy and permission-profile settings cannot coexist")
    return choice


def replace_managed_block(text):
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER, start) + len(END_MARKER)
    if text[end:end + 2] == "\r\n":
        end += 2
    elif text[end:end + 1] == "\n":
        end += 1
    return text[:start] + PROFILE_BLOCK + text[end:]


def unrelated_policy_view(parsed):
    """Return the parsed config with only this migration's fields removed."""
    result = copy.deepcopy(parsed)
    for key in ("sandbox_mode", "sandbox_workspace_write", "default_permissions"):
        result.pop(key, None)
    permissions = result.get("permissions")
    if isinstance(permissions, dict):
        for name in MANAGED_CHOICES:
            permissions.pop(name, None)
        if not permissions:
            result.pop("permissions")
    return result


def assert_unrelated_unchanged(before, after, path):
    if unrelated_policy_view(before) != unrelated_policy_view(after):
        raise Refusal(f"{path}: migration would change unrelated configuration")


def global_transform(source):
    path, text = source["path"], source["text"]
    parsed = parse_toml(text, path)
    has_marker = BEGIN_MARKER in text or END_MARKER in text
    managed_names = set(parsed.get("permissions", {})) & MANAGED_CHOICES \
        if isinstance(parsed.get("permissions", {}), dict) else set()

    if has_marker:
        if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
            raise Refusal(f"{path}: managed profile markers are damaged")
        current = validate_upgradeable_managed_profiles(parsed, path)
        network = current in {"git-workspace", "no-git"}
        choice = expected_profile_choice(network, git=True)
        output = set_assignment(replace_managed_block(text), "default_permissions", choice)
        parsed_output = parse_toml(output, path)
        validate_managed_profiles(parsed_output, path)
        assert_unrelated_unchanged(parsed, parsed_output, path)
        return output, choice
    if managed_names:
        raise Refusal(f"{path}: managed profile names already exist without ownership markers")
    if "default_permissions" in parsed:
        raise Refusal(f"{path}: existing default_permissions is not managed by this tool")

    text, legacy = strip_legacy(text, path, parsed)
    # These were explicit in the legacy global policy. Refuse instead of
    # silently broadening temp-directory writes on a differently shaped config.
    if (legacy.get("exclude_slash_tmp") is not True
            or legacy.get("exclude_tmpdir_env_var") is not True):
        raise Refusal(f"{path}: expected both legacy temporary-directory exclusions")
    network = legacy.get("network_access", False)
    choice = expected_profile_choice(network, git=True)
    text = set_assignment(text, "default_permissions", choice)
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    text += PROFILE_BLOCK
    output = parse_toml(text, path)
    validate_managed_profiles(output, path)
    assert_unrelated_unchanged(parsed, output, path)
    return text, choice


def project_effective_network(parsed, inherited, path):
    legacy = parsed.get("sandbox_workspace_write")
    if legacy is None:
        return inherited
    if not isinstance(legacy, dict) or set(legacy) - LEGACY_KEYS:
        raise Refusal(f"{path}: project has unsupported sandbox_workspace_write keys")
    for key in ("exclude_slash_tmp", "exclude_tmpdir_env_var"):
        if key in legacy and legacy[key] is not True:
            raise Refusal(
                f"{path}: project explicitly broadens {key}; cannot preserve that policy")
    value = legacy.get("network_access", inherited)
    if not isinstance(value, bool):
        raise Refusal(f"{path}: project network_access must be a boolean")
    return value


def reject_project_managed_profiles(parsed, path):
    project_permissions = parsed.get("permissions", {})
    if (isinstance(project_permissions, dict)
            and set(project_permissions) & MANAGED_CHOICES):
        raise Refusal(f"{path}: managed profiles belong only in the user config")


def project_transform(source, *, inherited_network, git, create):
    path, text = source["path"], source["text"]
    if not source["exists"]:
        if not create:
            return text, None
        choice = expected_profile_choice(inherited_network, git=git)
        return ("# Managed project override; global profiles live in ~/.codex/config.toml.\n"
                f'default_permissions = "{choice}"\n'), choice

    parsed = parse_toml(text, path)
    reject_project_managed_profiles(parsed, path)

    legacy = "sandbox_mode" in parsed or "sandbox_workspace_write" in parsed
    if legacy:
        network = project_effective_network(parsed, inherited_network, path)
        text, _table = strip_legacy(text, path, parsed)
        choice = expected_profile_choice(network, git=git)
        text = set_assignment(text, "default_permissions", choice)
    else:
        current = parsed.get("default_permissions")
        if current is None:
            if not create:
                return text, None
            choice = expected_profile_choice(inherited_network, git=git)
            text = set_assignment(text, "default_permissions", choice)
        elif current in MANAGED_CHOICES:
            network = current in {"git-workspace", "no-git"}
            choice = expected_profile_choice(network, git=git)
            text = set_assignment(text, "default_permissions", choice)
        else:
            raise Refusal(f"{path}: unrelated default_permissions policy is present")
    output = parse_toml(text, path)
    if output.get("default_permissions") != choice:
        raise Refusal(f"{path}: transformed project profile did not validate")
    if "sandbox_mode" in output or "sandbox_workspace_write" in output:
        raise Refusal(f"{path}: legacy project sandbox settings remain")
    assert_unrelated_unchanged(parsed, output, path)
    return text, choice


def git_project(value):
    path = Path(value).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise Refusal(f"{path}: not a Git project: {detail}") from exc
    return Path(result.stdout.strip()).resolve()


def git_directory(root):
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
        check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return Path(result.stdout.strip()).resolve()


def inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def probe_git_directory(git_dir):
    """Prove the active sandbox can create and remove one inert Git-dir file."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    name = f".codex-git-access-probe-{os.getpid()}-{secrets.token_hex(6)}"
    directory_fd = None
    probe_fd = None
    created = False
    try:
        directory_fd = os.open(git_dir, directory_flags)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        probe_fd = os.open(name, file_flags, 0o600, dir_fd=directory_fd)
        created = True
        os.write(probe_fd, b"Codex Git metadata write probe; safe to remove.\n")
        os.close(probe_fd)
        probe_fd = None
        os.unlink(name, dir_fd=directory_fd)
        created = False
    except OSError as exc:
        raise Refusal(
            f"Git directory write probe failed for {git_dir}: {exc.strerror or exc}") from exc
    finally:
        if probe_fd is not None:
            os.close(probe_fd)
        if created and directory_fd is not None:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)


def linked_launcher_configured():
    launcher = Path.home() / ".local" / "bin" / "codex"
    expected = Path(__file__).resolve().with_name("codex-worktree")
    try:
        return launcher.is_symlink() and launcher.resolve(strict=True) == expected
    except OSError:
        return False


def change(source, text, label):
    if text == source["text"]:
        return None
    result = dict(source)
    result.update({"new_text": text, "label": label, "parent_created": False})
    return result


def open_parent_for_write(entry):
    parent = entry["path"].parent
    if entry["parent_missing"]:
        grand_fd, grand = open_owned_directory(parent.parent)
        try:
            if (grand.st_dev, grand.st_ino) != entry["grand_identity"]:
                raise Refusal(f"{parent.parent}: changed after preflight")
            try:
                os.mkdir(parent.name, 0o755, dir_fd=grand_fd)
            except OSError as exc:
                raise Refusal(
                    f"{parent}: cannot create safely: {exc.strerror or exc}") from exc
            os.fsync(grand_fd)
            entry["parent_created"] = True
        finally:
            os.close(grand_fd)
        entry["parent_missing"] = False
    parent_fd, info = open_owned_directory(parent)
    expected = entry.get("parent_identity")
    if expected is not None and (info.st_dev, info.st_ino) != expected:
        os.close(parent_fd)
        raise Refusal(f"{parent}: changed after preflight")
    entry["parent_identity"] = (info.st_dev, info.st_ino)
    return parent_fd


def assert_source_unchanged(entry, parent_fd):
    name = entry["path"].name
    if entry["exists"]:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise Refusal(f"{entry['path']}: changed after preflight: {exc}") from exc
        try:
            info = os.fstat(fd)
            safe = (stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and info.st_nlink == 1
                    and not writable_by_peer(info, fd, entry["path"])
                    and stat.S_IMODE(info.st_mode) == entry["mode"]
                    and (info.st_dev, info.st_ino) == entry["identity"])
            data = read_config(fd, entry["path"]) if safe else None
        finally:
            os.close(fd)
        if not safe or data != entry["data"]:
            raise Refusal(f"{entry['path']}: changed after preflight")
        return
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise Refusal(f"{entry['path']}: appeared after preflight")


def atomic_replace(entry, text, mode):
    path = entry["path"]
    parent_fd = open_parent_for_write(entry)
    temp_name = f".{path.name}.codex-git-access-{os.getpid()}-{secrets.token_hex(6)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    temp_fd = None
    try:
        assert_source_unchanged(entry, parent_fd)
        temp_fd = os.open(temp_name, flags, mode, dir_fd=parent_fd)
        clear_inherited_access_acl(temp_fd, path)
        data = text.encode("utf-8")
        view = memoryview(data)
        while view:
            count = os.write(temp_fd, view)
            view = view[count:]
        os.fchmod(temp_fd, mode)
        os.fsync(temp_fd)
        temp_info = os.fstat(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        entry["exists"] = True
        entry["identity"] = (temp_info.st_dev, temp_info.st_ino)
        entry["text"] = text
        entry["data"] = data
        entry["mode"] = mode
        os.fsync(parent_fd)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def remove_created_entry(entry):
    if entry["parent_missing"]:
        return
    parent_fd = open_parent_for_write(entry)
    try:
        assert_source_unchanged(entry, parent_fd)
        if entry["exists"]:
            os.unlink(entry["path"].name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    if entry.get("parent_created"):
        parent = entry["path"].parent
        grand_fd, grand = open_owned_directory(parent.parent)
        try:
            if (grand.st_dev, grand.st_ino) != entry["grand_identity"]:
                raise Refusal(f"{parent.parent}: changed during rollback")
            os.rmdir(parent.name, dir_fd=grand_fd)
            os.fsync(grand_fd)
        finally:
            os.close(grand_fd)


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise Refusal(f"{path}: backup directory is unsafe")
    os.chmod(path, 0o700)


def write_new(path, data, mode=0o600):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            view = view[count:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_bundle(changes):
    root = state_root()
    ensure_private_dir(root)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = root / f"{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    os.mkdir(bundle, 0o700)
    manifest = []
    for index, entry in enumerate(changes):
        backup = None
        if entry["exists"]:
            backup = f"{index:03d}-{entry['path'].name}"
            write_new(bundle / backup, entry["text"].encode("utf-8"))
        manifest.append({"path": str(entry["path"]), "existed": entry["exists"],
                         "mode": entry["mode"], "backup": backup})
    write_new(bundle / "manifest.json",
              (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    directory_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    root_fd, _root_info = open_owned_directory(root)
    try:
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return bundle


def apply_changes(changes):
    if not changes:
        print("codex-git-access: already configured; no changes")
        return None
    bundle = backup_bundle(changes)
    originals = [dict(entry) for entry in changes]
    applied = []
    try:
        for entry in changes:
            applied.append(entry)
            atomic_replace(entry, entry["new_text"], entry["mode"])
    except BaseException as exc:
        rollback_errors = []
        for entry, original in reversed(list(zip(applied, originals))):
            try:
                if original["exists"]:
                    atomic_replace(entry, original["text"], original["mode"])
                else:
                    remove_created_entry(entry)
            except BaseException as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        detail = f"; rollback errors: {rollback_errors}" if rollback_errors else ""
        raise Refusal(f"write failed; originals backed up at {bundle}: {exc}{detail}") from exc
    print(f"codex-git-access: backup: {bundle}")
    for entry in changes:
        print(f"codex-git-access: updated {entry['label']}: {entry['path']}")
    return bundle


def load_global():
    source = secure_read(codex_home() / "config.toml")
    text, choice = global_transform(source)
    parsed = parse_toml(text, source["path"])
    return source, text, choice, choice in {"git-workspace", "no-git"}


def enable(project_values):
    global_source, global_text, choice, inherited_network = load_global()
    changes = []
    item = change(global_source, global_text, "user config")
    if item:
        changes.append(item)
    roots = []
    for value in project_values:
        root = git_project(value)
        if root in roots:
            continue
        roots.append(root)
        source = secure_read(root / ".codex" / "config.toml", missing_ok=True)
        text, _project_choice = project_transform(
            source, inherited_network=inherited_network, git=True, create=False)
        item = change(source, text, f"project {root}")
        if item:
            changes.append(item)
    apply_changes(changes)
    print(f"codex-git-access: default profile: {choice}")
    print("codex-git-access: restart Codex for the new sandbox to take effect")


def opt_out(project_value):
    global_source = secure_read(codex_home() / "config.toml")
    parsed = parse_toml(global_source["text"], global_source["path"])
    choice = validate_managed_profiles(parsed, global_source["path"])
    inherited_network = choice in {"git-workspace", "no-git"}
    root = git_project(project_value)
    source = secure_read(root / ".codex" / "config.toml", missing_ok=True)
    text, project_choice = project_transform(
        source, inherited_network=inherited_network, git=False, create=True)
    item = change(source, text, f"project {root}")
    apply_changes([item] if item else [])
    print(f"codex-git-access: {root}: {project_choice or 'already inherits no Git access'}")
    print("codex-git-access: restart Codex in that project for the override to take effect")


def check(project_values):
    source = secure_read(codex_home() / "config.toml")
    parsed = parse_toml(source["text"], source["path"])
    choice = validate_managed_profiles(parsed, source["path"])
    print(f"ok  user config: {choice}")
    failed = False
    for value in project_values:
        root = git_project(value)
        project_path = root / ".codex" / "config.toml"
        project_source = secure_read(project_path, missing_ok=True)
        active = choice
        if project_source["exists"]:
            project_data = parse_toml(project_source["text"], project_path)
            try:
                reject_project_managed_profiles(project_data, project_path)
            except Refusal as exc:
                print(f"FAIL {root}: {exc}")
                failed = True
                continue
            if "sandbox_mode" in project_data or "sandbox_workspace_write" in project_data:
                print(f"FAIL {root}: legacy project sandbox settings remain")
                failed = True
                continue
            local = project_data.get("default_permissions")
            if local is not None:
                if local not in MANAGED_CHOICES:
                    print(f"FAIL {root}: unrelated default_permissions={local!r}")
                    failed = True
                    continue
                active = local
        if active in {"no-git", "no-git-offline"}:
            print(f"ok  {root}: {active}; Git metadata is intentionally read-only")
            continue
        git_dir = git_directory(root)
        if inside(git_dir, root):
            try:
                probe_git_directory(git_dir)
            except Refusal as exc:
                print(f"FAIL {root}: {exc}")
                failed = True
                continue
            print(f"ok  {root}: {active}; Git metadata write probe passed")
        elif linked_launcher_configured():
            try:
                probe_git_directory(git_dir)
            except Refusal as exc:
                print(f"FAIL {root}: managed launcher is installed but {exc}")
                failed = True
                continue
            print(
                f"ok  {root}: {active}; linked-worktree Git metadata write probe passed")
        else:
            print(
                f"FAIL {root}: linked Git directory is outside the workspace and the "
                f"managed ~/.local/bin/codex launcher is missing: {git_dir}")
            failed = True
    if not project_values:
        print("ok  no project paths requested")
    if failed:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="codex-git-access",
        description="Enable Git metadata writes by default with project opt-out.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_enable = sub.add_parser("enable", help="migrate the user default and named projects")
    p_enable.add_argument("project", nargs="*")
    p_opt = sub.add_parser("opt-out", help="keep Git metadata read-only in one project")
    p_opt.add_argument("project")
    p_check = sub.add_parser("check", help="validate profiles and project Git-directory scope")
    p_check.add_argument("project", nargs="*")
    args = parser.parse_args()
    try:
        if args.command == "enable":
            enable(args.project)
        elif args.command == "opt-out":
            opt_out(args.project)
        else:
            check(args.project)
    except Refusal as exc:
        print(f"codex-git-access: REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
