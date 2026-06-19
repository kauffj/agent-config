"""Shared helpers for discovering live Claude Code interactive sessions.

Source of truth is Claude's own runtime registry at ~/.claude/sessions/<pid>.json
(written while a session is active, removed on clean exit). We augment it with a
/proc scan to catch sessions that are alive but not yet/no longer registered
(e.g. sitting at the startup "resume from summary?" prompt), recovering the
session id from the process argv (--resume / --session-id / -r).
"""
import glob
import json
import os

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
REGISTRY_DIR = os.path.join(CLAUDE_DIR, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _proc_argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _proc_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _looks_like_session_id(s):
    # UUIDs: 36 chars with dashes. Be lenient but reject flags/paths.
    return s and len(s) >= 32 and "-" in s and "/" not in s and not s.startswith("-")


def _from_registry():
    sessions = {}
    for path in glob.glob(os.path.join(REGISTRY_DIR, "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        sid, pid = d.get("sessionId"), d.get("pid")
        if not sid or not pid_alive(pid):
            continue
        if d.get("kind") and d.get("kind") != "interactive":
            continue
        sessions[sid] = {
            "sessionId": sid,
            "cwd": d.get("cwd"),
            "pid": pid,
            "status": d.get("status"),
            "source": "registry",
        }
    return sessions


def _argv_session_id(argv):
    for i, a in enumerate(argv):
        if a in ("--resume", "-r", "--session-id") and i + 1 < len(argv):
            if _looks_like_session_id(argv[i + 1]):
                return argv[i + 1]
    return None


def _scan_proc(known_ids, known_pids):
    """Scan /proc for live `claude` processes. Returns (by_id, unresolved) where
    by_id maps sessionId->info for processes whose id is recoverable from argv
    (--resume/--session-id), and unresolved is a list of (pid, cwd) for live
    claude processes whose session id isn't in argv (fresh `claude` launches).
    Processes already accounted for by the registry (known_pids) are skipped."""
    by_id, unresolved = {}, []
    for pdir in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(pdir)
        if int(pid) in known_pids:
            continue
        argv = _proc_argv(pid)
        if not argv or os.path.basename(argv[0]) != "claude":
            continue
        sid = _argv_session_id(argv)
        if sid and sid not in known_ids and sid not in by_id:
            by_id[sid] = {"sessionId": sid, "cwd": _proc_cwd(pid),
                          "pid": int(pid), "status": None, "source": "proc"}
        elif not sid:
            unresolved.append((int(pid), _proc_cwd(pid)))
    return by_id, unresolved


def _project_dir(cwd):
    # Claude encodes a cwd into a transcript dir by replacing "/" with "-".
    return os.path.join(PROJECTS_DIR, cwd.replace("/", "-"))


def _recent_transcripts(cwd, exclude, limit):
    """The `limit` most-recently-modified transcript ids in cwd's project dir,
    skipping ids already accounted for. Used to attribute fresh `claude`
    processes (no id in argv, not in the registry) to their session."""
    pdir = _project_dir(cwd)
    found = []
    try:
        names = os.listdir(pdir)
    except OSError:
        return []
    for fn in names:
        if not fn.endswith(".jsonl"):
            continue
        sid = fn[:-6]
        if sid in exclude:
            continue
        try:
            found.append((os.path.getmtime(os.path.join(pdir, fn)), sid))
        except OSError:
            continue
    found.sort(reverse=True)
    return [sid for _, sid in found[:limit]]


def live_sessions():
    """Return {sessionId: {sessionId, cwd, pid, status, source}} for all live
    interactive Claude sessions.

    Sources, in order of confidence:
      registry  — Claude's own ~/.claude/sessions/<pid>.json (pid + id, exact)
      proc      — session id recovered from process argv (--resume/--session-id)
      heuristic — fresh `claude` (no id in argv, not yet registered) mapped to
                  the most-recently-active transcript(s) in its cwd. When several
                  fresh sessions share a cwd we capture that many recent
                  transcripts (right count, best-effort identity) so none is lost.
    """
    sessions = _from_registry()
    known_pids = {info["pid"] for info in sessions.values() if info.get("pid")}
    by_id, unresolved = _scan_proc(set(sessions), known_pids)
    for sid, info in by_id.items():
        sessions.setdefault(sid, info)

    # Attribute fresh, unregistered claude processes via recent transcripts.
    by_cwd = {}
    for pid, cwd in unresolved:
        if cwd:
            by_cwd.setdefault(cwd, []).append(pid)
    for cwd, pids in by_cwd.items():
        for sid in _recent_transcripts(cwd, set(sessions), len(pids)):
            sessions.setdefault(sid, {"sessionId": sid, "cwd": cwd, "pid": None,
                                      "status": None, "source": "heuristic"})
    return sessions
