#!/usr/bin/python3 -E
"""Run one command with bounded stdout/stderr for synchronous Node callers."""

import os
import selectors
import subprocess
import sys


OUTPUT_LIMIT_BYTES = 1024 * 1024
READ_BYTES = 64 * 1024
OVERFLOW_EXIT = 125


def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def stop(process):
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run(argv):
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError as exc:
        write_all(2, f"bounded-command: cannot start command: {exc.strerror or exc}\n".encode())
        return 127

    selector = selectors.DefaultSelector()
    streams = {
        process.stdout: ("stdout", bytearray()),
        process.stderr: ("stderr", bytearray()),
    }
    for stream in streams:
        selector.register(stream, selectors.EVENT_READ)

    overflow = None
    try:
        while selector.get_map():
            for key, _mask in selector.select():
                stream = key.fileobj
                label, captured = streams[stream]
                chunk = os.read(stream.fileno(), READ_BYTES)
                if not chunk:
                    selector.unregister(stream)
                    continue
                remaining = OUTPUT_LIMIT_BYTES - len(captured)
                captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow = label
                    break
            if overflow:
                break
    finally:
        selector.close()

    if overflow:
        stop(process)
        write_all(
            2,
            f"bounded-command: {overflow} exceeded {OUTPUT_LIMIT_BYTES} bytes\n".encode(),
        )
        return OVERFLOW_EXIT

    returncode = process.wait()
    stdout = streams[process.stdout][1]
    stderr = streams[process.stderr][1]
    process.stdout.close()
    process.stderr.close()
    write_all(1, stdout)
    write_all(2, stderr)
    return 128 + (-returncode) if returncode < 0 else returncode


def main():
    if len(sys.argv) < 2:
        write_all(2, b"bounded-command: command is required\n")
        return 2
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
