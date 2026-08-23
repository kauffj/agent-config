#!/usr/bin/env python3
"""Integration tests for agent-session-server's lifecycle guarantee."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


WRAPPER = Path(__file__).with_name("agent-session-server")


def running(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return fields[2] != "Z"


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class AgentSessionServerTest(unittest.TestCase):
    def test_owner_exit_stops_server_and_its_children(self):
        owner = subprocess.Popen(["sleep", "60"])
        wrapper = None
        server_pids = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pid_file = Path(tmp) / "server-pids"
                child_code = "\n".join(
                    [
                        "import os, pathlib, subprocess, time",
                        "grandchild = subprocess.Popen(['sleep', '60'])",
                        f"pathlib.Path({str(pid_file)!r}).write_text(f'{{os.getpid()}} {{grandchild.pid}}')",
                        "while True: time.sleep(1)",
                    ]
                )
                wrapper = subprocess.Popen(
                    [
                        str(WRAPPER),
                        "--session-pid",
                        str(owner.pid),
                        "--",
                        sys.executable,
                        "-c",
                        child_code,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertTrue(wait_until(pid_file.exists), "server never started")
                server_pids = [int(pid) for pid in pid_file.read_text().split()]
                self.assertTrue(all(running(pid) for pid in server_pids))

                wrapper.send_signal(signal.SIGHUP)
                time.sleep(0.2)
                self.assertIsNone(wrapper.poll(), "tool-terminal hangup stopped wrapper")
                self.assertTrue(all(running(pid) for pid in server_pids))

                owner.terminate()
                owner.wait(timeout=2)
                self.assertEqual(wrapper.wait(timeout=5), 0)
                self.assertTrue(
                    wait_until(lambda: not any(running(pid) for pid in server_pids)),
                    "server process group survived its owner",
                )
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=2)
            if wrapper is not None and wrapper.poll() is None:
                wrapper.send_signal(signal.SIGTERM)
                wrapper.wait(timeout=5)
            if wrapper is not None and wrapper.stderr is not None:
                wrapper.stderr.close()
            for pid in server_pids:
                if running(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_server_exit_status_is_preserved(self):
        result = subprocess.run(
            [
                str(WRAPPER),
                "--session-pid",
                str(os.getpid()),
                "--",
                "sh",
                "-c",
                "exit 7",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 7, result.stderr)


if __name__ == "__main__":
    unittest.main()
