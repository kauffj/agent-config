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
import time
from datetime import datetime

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
REGISTRY_DIR = os.path.join(CLAUDE_DIR, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
SNAPSHOT = os.path.join(CLAUDE_DIR, "sessions-snapshot.json")
# Pre-crash high-water set, written by claude-snapshot on a "cliff" (mass session
# death) and never clobbered by the timer's honest-state writes. The durable record
# a freeze can't erase before you restore. See claude-snapshot and resume_set().
RECOVERY_SNAP = os.path.join(CLAUDE_DIR, "sessions-recovery.json")


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


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
START_MARGIN = 5.0  # seconds of clock-skew slack when matching start times


def _boot_epoch():
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _proc_start_epoch(pid):
    """Wall-clock epoch seconds at which `pid` started, or None."""
    btime = _boot_epoch()
    if btime is None:
        return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # field 22 (starttime) in clock ticks since boot; fields after comm,
        # which is parenthesized and may contain spaces -> split on last ')'.
        fields = data[data.rfind(")") + 2:].split()
        return btime + int(fields[19]) / _CLK_TCK
    except (OSError, ValueError, IndexError):
        return None


def _transcript_first_ts(path):
    """Epoch of the earliest event timestamp in a transcript (reads only the
    first few lines), or None."""
    try:
        with open(path) as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                try:
                    ts = json.loads(line).get("timestamp")
                except ValueError:
                    continue
                if ts:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except OSError:
        pass
    return None


def _attribute_fresh(cwd, pids, exclude):
    """Map fresh `claude` pids (no id in argv, not registered) to their session
    transcripts. A new session's transcript cannot predate its process, so for
    each pid we pick the unclaimed transcript whose first event is nearest to —
    and not meaningfully before — that pid's start time. Pre-existing/closed
    transcripts are thereby excluded; a fresh pid with no qualifying transcript
    (e.g. an untouched session with nothing to resume) is simply left out."""
    pdir = _project_dir(cwd)
    candidates = []  # (first_ts, sid)
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
        ts = _transcript_first_ts(os.path.join(pdir, fn))
        if ts is not None:
            candidates.append((ts, sid))

    starts = sorted((s for s in (_proc_start_epoch(p) for p in pids) if s is not None))
    matched, used = [], set()
    for start in starts:
        best, best_diff = None, None
        for ts, sid in candidates:
            if sid in used or ts < start - START_MARGIN:
                continue  # a session's transcript can't predate its process
            diff = ts - start
            if best_diff is None or diff < best_diff:
                best, best_diff = sid, diff
        if best is not None:
            used.add(best)
            matched.append(best)
    return matched


def live_sessions():
    """Return {sessionId: {sessionId, cwd, pid, status, source}} for all live
    interactive Claude sessions.

    Sources, in order of confidence:
      registry  — Claude's own ~/.claude/sessions/<pid>.json (pid + id, exact)
      proc      — session id recovered from process argv (--resume/--session-id)
      heuristic — fresh `claude` (no id in argv, not yet registered) matched to
                  the transcript whose first event is nearest to, and not before,
                  the process's start time (a new session can't predate its
                  process), so closed/pre-existing transcripts are excluded.
    """
    sessions = _from_registry()
    known_pids = {info["pid"] for info in sessions.values() if info.get("pid")}
    by_id, unresolved = _scan_proc(set(sessions), known_pids)
    for sid, info in by_id.items():
        sessions.setdefault(sid, info)

    # Attribute fresh, unregistered claude processes to their transcripts.
    by_cwd = {}
    for pid, cwd in unresolved:
        if cwd:
            by_cwd.setdefault(cwd, []).append(pid)
    for cwd, pids in by_cwd.items():
        for sid in _attribute_fresh(cwd, pids, set(sessions)):
            sessions.setdefault(sid, {"sessionId": sid, "cwd": cwd, "pid": None,
                                      "status": None, "source": "heuristic"})
    return sessions


def boot_epoch():
    """Wall-clock epoch seconds of the last system boot, or None."""
    return _boot_epoch()


def _transcript_cwd(path):
    """The cwd recorded in a transcript (scans the first lines), or None."""
    try:
        with open(path) as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


def recent_transcript_sessions(limit=12, exclude=()):
    """Reconstruct likely-open sessions from transcript files, most-recently
    active first. Used as a fallback when no usable snapshot survives (e.g. it
    was overwritten post-boot). Returns a list of
    {sessionId, cwd, status, source} dicts. Subagent/workflow transcripts and
    ids in `exclude` are skipped; the real cwd is read from each transcript so
    the resulting `claude --resume` lands in the right directory."""
    exclude = set(exclude)
    rows = []
    for path in glob.glob(os.path.join(PROJECTS_DIR, "**", "*.jsonl"), recursive=True):
        if os.sep + "subagents" + os.sep in path:
            continue
        sid = os.path.basename(path)[:-6]
        if sid in exclude:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        cwd = _transcript_cwd(path)
        if not cwd or not os.path.isdir(cwd):
            continue
        rows.append((mtime, sid, cwd))
    rows.sort(reverse=True)
    out = []
    for _, sid, cwd in rows[:limit]:
        out.append({"sessionId": sid, "cwd": cwd,
                    "status": "reconstructed", "source": "transcripts"})
    return out


def transcript_exists(sid, cwd):
    """True if cwd's transcript dir holds <sid>.jsonl — the file `claude --resume`
    needs. A session whose transcript was rolled back by a freeze fails to resume
    ('No conversation found'); filtering on this keeps such ids from spawning dead
    tabs. Run claude-restore-transcripts first to pull any survivor out of backup."""
    if not sid or not cwd:
        return False
    return os.path.isfile(os.path.join(_project_dir(cwd), sid + ".jsonl"))


def _snap_meta(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def resume_set(snap_path=SNAPSHOT, recovery_max_age_h=12):
    """Sessions to reopen after a restart, ready for `claude --resume`.

    Unions the live snapshot with any pre-crash set preserved in the recovery
    file (a freeze can wipe the live snapshot before you restore, so the recovery
    file is the durable high-water record the snapshot timer is forbidden to
    clobber). Deduped by id, snapshot first; excludes sessions that are already
    live or whose transcript no longer exists on disk (so a rolled-back id never
    spawns a dead 'No conversation found' tab). Returns [{sessionId, cwd}]."""
    live = set(live_sessions())
    sources = [_snap_meta(snap_path).get("sessions", [])]
    rec = _snap_meta(RECOVERY_SNAP)
    preserved = rec.get("preservedAt", 0)
    if rec.get("sessions") and preserved and \
            (time.time() - preserved) <= recovery_max_age_h * 3600:
        sources.append(rec["sessions"])
    out, seen = [], set()
    for src in sources:
        for s in src:
            sid, cwd = s.get("sessionId"), s.get("cwd")
            if not sid or not cwd or sid in seen or sid in live:
                continue
            if not (os.path.isdir(cwd) and transcript_exists(sid, cwd)):
                continue
            seen.add(sid)
            out.append({"sessionId": sid, "cwd": cwd})
    return out


def clear_recovery():
    """Consume the recovery file after a successful restore — archive (don't
    delete) into backups/ so it can't re-suggest the same set on the next run."""
    if not os.path.exists(RECOVERY_SNAP):
        return
    dest_dir = os.path.join(CLAUDE_DIR, "backups")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        os.replace(RECOVERY_SNAP,
                   os.path.join(dest_dir, "sessions-recovery.consumed.json"))
    except OSError:
        pass
