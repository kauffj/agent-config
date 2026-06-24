#!/usr/bin/env bash
# Durability hook: flush this session's transcript to disk the instant a turn ends.
#
# Claude appends each turn to its transcript .jsonl, but the kernel may hold those
# writes in the page cache for seconds before flushing — so an unclean shutdown
# (battery death, freeze) can roll a just-finished turn back to nothing. Wiring this
# to the Stop event makes every completed turn durable immediately ("autosave on
# every save"), with the claude-transcript-sync timer as the 20s backstop for
# mid-turn content.
#
# Reads the hook payload (JSON) on stdin; fsyncs `transcript_path`. Always exits 0
# so it can never block or fail a turn.
exec python3 - <<'PY'
import json, os, sys
try:
    p = json.load(sys.stdin).get("transcript_path")
    if p and os.path.exists(p):
        fd = os.open(p, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
except Exception:
    pass
PY
