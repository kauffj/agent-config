#!/usr/bin/env python3
"""Run one resumed agent under a secure, session-scoped live lease."""
import argparse
import base64
import errno
import fcntl
import os
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from _claude_sessions_lib import (  # noqa: E402
    SESSION_ID_RE, open_live_lease_dir, process_identity_alive,
    process_live_session_ids, session_process_identity,
)


def wait_until_stopped(session_id):
    """Wait for a handoff's old writer by identity, not namespace-local PID."""
    identity = session_process_identity(session_id)
    if identity is not None:
        while process_identity_alive(identity):
            time.sleep(0.5)
        return
    while session_id in process_live_session_ids():
        time.sleep(0.5)


def acquire_lease(session_id, timeout):
    directory_fd = open_live_lease_dir(create=True)
    try:
        fd = os.open(session_id + ".lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise OSError("unsafe live-session lease")
        os.fchmod(fd, 0o600)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if timeout is None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    return fd
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(0.05)
    except Exception:
        os.close(fd)
        raise


def publish_pane_session(session_id):
    """Bind identity to this pane without printing visible terminal text."""
    if not os.environ.get("WEZTERM_PANE") or not sys.stdout.isatty():
        return
    encoded = base64.b64encode(session_id.encode()).decode()
    sys.stdout.write("\033]1337;SetUserVar=agent_session=" + encoded + "\007")
    sys.stdout.flush()


def _stop_group(child):
    """Stop the whole supervised foreground group before releasing its lease."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        if child.poll() is None:
            child.wait()
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if child.poll() is None:
        child.wait()


def _set_foreground_process_group(pgrp):
    """Give the controlling terminal to pgrp without stopping this supervisor."""
    if not sys.stdin.isatty():
        return
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(sys.stdin.fileno(), pgrp)
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _await_foreground(start_fd, command):
    """Exec the interactive child only after its supervisor foregrounds it.

    A background interactive Bash can stop in its own terminal initialization.
    Waiting in this non-interactive Python process avoids that race. If the
    supervisor dies first, its pipe end closes and this process exits, taking
    its inherited lease descriptor with it.
    """
    try:
        ready = os.read(start_fd, 1)
    finally:
        os.close(start_fd)
    if ready != b"1":
        return 125
    os.execvp("bash", ["bash", "-lic", "set +m\n" + command])


def _spawn_gated_agent(command, lease):
    """Spawn a new process group that cannot touch the TTY before release."""
    start_fd, release_fd = os.pipe2(os.O_CLOEXEC)
    try:
        child = subprocess.Popen(
            [sys.executable, os.path.realpath(__file__),
             "--await-foreground", str(start_fd), command],
            close_fds=True, pass_fds=(lease, start_fd), process_group=0)
    except BaseException:
        os.close(release_fd)
        raise
    finally:
        os.close(start_fd)
    return child, release_fd


def run_agent(command, session_id, lease):
    """Supervise one foreground process group under its session lease.

    The child inherits the locked open-file description. If this small
    supervisor is killed, the live agent therefore keeps the lease until its
    own process tree exits. On the normal path main() explicitly unlocks after
    the foreground command has returned.
    """
    terminal_pgrp = os.tcgetpgrp(sys.stdin.fileno()) if sys.stdin.isatty() else None
    child, release_fd = _spawn_gated_agent(command, lease)
    try:
        if terminal_pgrp is not None:
            _set_foreground_process_group(child.pid)
        # Login + interactive loads the account-routing shell function. The
        # child reaches Bash only after it owns the TTY. Monitor mode then stays
        # off so Bash and the native agent remain one supervised process group.
        os.write(release_fd, b"1")
    except BaseException:
        os.close(release_fd)
        _stop_group(child)
        raise
    else:
        os.close(release_fd)

    def forward(signum, _frame):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    previous = {}
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, forward)
    try:
        # A created pane, an acquired lease, or a long-running account router is
        # not yet an agent. Publish only after host process discovery proves the
        # native session is running; lifecycle hooks normally publish even sooner.
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            if session_id in process_live_session_ids():
                publish_pane_session(session_id)
                break
            time.sleep(0.5)
        status = child.wait()
        # A foreground shell should reap its command. If it instead exits while
        # leaving members of the session group behind, stop them before unlock.
        _stop_group(child)
    except BaseException:
        _stop_group(child)
        raise
    finally:
        if terminal_pgrp is not None:
            try:
                _set_foreground_process_group(terminal_pgrp)
            except OSError:
                pass
        # The pane returns to a plain shell after the agent exits; it must stop
        # advertising a live writer at the same boundary that releases its lease.
        publish_pane_session("")
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return status if status >= 0 else 128 - status


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--await-foreground":
        try:
            start_fd = int(sys.argv[2])
        except ValueError:
            raise SystemExit(125)
        raise SystemExit(_await_foreground(start_fd, sys.argv[3]))

    parser = argparse.ArgumentParser(prog="_agent_session_lease.py")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--recover-terminal", action="store_true")
    parser.add_argument("session_id")
    parser.add_argument("command", nargs="?")
    args = parser.parse_args()
    if not SESSION_ID_RE.fullmatch(args.session_id):
        parser.error("unsafe session id")

    if args.recover_terminal:
        if args.wait or args.command is not None:
            parser.error("--recover-terminal accepts only a session id")
        # SIGKILL can bypass the supervisor's terminal-restoration finally.
        # The agent tree inherited this lock, so waiting for it is an exact,
        # PID-namespace-independent way to know the old foreground group ended.
        lease = acquire_lease(args.session_id, None)
        try:
            if sys.stdin.isatty():
                _set_foreground_process_group(os.getpgrp())
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)
            os.close(lease)
        return
    if args.command is None:
        parser.error("command is required")

    if args.wait:
        wait_until_stopped(args.session_id)
    try:
        lease = acquire_lease(args.session_id, 10 if args.wait else 1)
    except TimeoutError:
        print("agent-tab-shell: session %s is already active" % args.session_id,
              file=sys.stderr)
        sys.exit(75)
    except OSError as exc:
        detail = exc.strerror if exc.errno in (errno.EACCES, errno.EPERM) else str(exc)
        sys.exit("agent-tab-shell: cannot create live-session lease: " + detail)
    try:
        status = run_agent(args.command, args.session_id, lease)
        # An interactive tab always continues into the wrapper's clean login
        # shell, so its agent's numeric status is not the tab program's status.
        if sys.stdin.isatty() and sys.stdout.isatty():
            status = 0
        raise SystemExit(status)
    finally:
        fcntl.flock(lease, fcntl.LOCK_UN)
        os.close(lease)


if __name__ == "__main__":
    main()
