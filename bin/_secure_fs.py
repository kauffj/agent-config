#!/usr/bin/python3 -E
"""Shared fail-closed filesystem ownership and ACL checks."""

import argparse
import errno
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import sys


class Refusal(Exception):
    pass


_PRIVATE_GROUPS = {}
SYSTEM_UID = Path("/").stat().st_uid


def group_is_private(gid):
    if gid not in _PRIVATE_GROUPS:
        current = pwd.getpwuid(os.getuid()).pw_name
        try:
            group = grp.getgrgid(gid)
        except KeyError:
            _PRIVATE_GROUPS[gid] = False
        else:
            primary_users = {
                entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == gid
            }
            listed_users = set(group.gr_mem)
            _PRIVATE_GROUPS[gid] = (
                group.gr_name == current
                and pwd.getpwuid(os.getuid()).pw_gid == gid
                and primary_users | listed_users <= {current}
            )
    return _PRIVATE_GROUPS[gid]


def has_extended_access_acl(fd, path):
    if not hasattr(os, "getxattr"):
        raise Refusal(f"{path}: cannot verify access-control lists")
    try:
        os.getxattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return False
        raise Refusal(f"{path}: cannot verify access-control lists: {exc}") from exc
    return True


def clear_inherited_access_acl(fd, path):
    if not hasattr(os, "removexattr"):
        raise Refusal(f"{path}: cannot clear inherited access-control lists")
    try:
        os.removexattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno not in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise Refusal(
                f"{path}: cannot clear inherited access-control lists: {exc}") from exc
    if has_extended_access_acl(fd, path):
        raise Refusal(f"{path}: inherited access-control list remains")


def writable_by_peer(info, fd, path):
    mode = stat.S_IMODE(info.st_mode)
    if mode & stat.S_IWOTH:
        return True
    if not mode & stat.S_IWGRP:
        return False
    return not group_is_private(info.st_gid) or has_extended_access_acl(fd, path)


def directory_open_flags():
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def open_owned_directory(path):
    try:
        fd = os.open(path, directory_open_flags())
    except OSError as exc:
        raise Refusal(f"{path}: cannot open safely: {exc.strerror or exc}") from exc
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or writable_by_peer(info, fd, path)):
        os.close(fd)
        raise Refusal(
            f"{path}: must be a user-owned directory not writable by another user")
    return fd, info


def read_all(fd):
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def validate_trusted_path(path, *, current_owned_leaf=True):
    """Validate one canonical absolute path and every ancestor without following links."""
    path = Path(path)
    if not path.is_absolute():
        raise Refusal(f"{path}: trusted path must be absolute")
    current = Path(path.anchor)
    entries = [current]
    for part in path.parts[1:]:
        current = current / part
        entries.append(current)
    for index, entry in enumerate(entries):
        leaf = index == len(entries) - 1
        flags = os.O_RDONLY | os.O_CLOEXEC
        if not leaf:
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(entry, flags)
        except OSError as exc:
            raise Refusal(f"{entry}: cannot open safely: {exc.strerror or exc}") from exc
        try:
            info = os.fstat(fd)
            if not leaf and not stat.S_ISDIR(info.st_mode):
                raise Refusal(f"{entry}: path ancestor is not a directory")
            allowed_owners = {SYSTEM_UID, os.getuid()}
            if info.st_uid not in allowed_owners or (leaf and current_owned_leaf
                                                     and info.st_uid != os.getuid()):
                raise Refusal(f"{entry}: is not owned by the current user or the system")
            if writable_by_peer(info, fd, entry):
                raise Refusal(f"{entry}: is writable by another user")
        finally:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate trusted absolute filesystem paths.")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    problems = {}
    for raw in args.paths:
        try:
            validate_trusted_path(raw)
        except (OSError, Refusal) as exc:
            problems[raw] = str(exc)
    print(json.dumps(problems, sort_keys=True))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
