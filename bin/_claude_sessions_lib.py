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
from urllib.parse import quote
import re
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


def proc_pane_id(pid):
    """The WezTerm pane a process is running in, from its environment, or None.

    Read from /proc rather than a session's state file because that file records
    the pane from whenever the session last wrote it — after a restore it can name
    a pane id that no longer exists. The environment is set at exec time and can't
    go stale.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for entry in f.read().split(b"\0"):
                if entry.startswith(b"WEZTERM_PANE="):
                    return entry.split(b"=", 1)[1].decode("utf-8", "replace")
    except (OSError, TypeError, ValueError):
        pass
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
    # Claude encodes a cwd into a transcript dir by replacing every
    # non-alphanumeric character with "-", not just "/". Dots count:
    # ~/projects/example.com -> -home-you-projects-example-com. Replacing
    # only "/" left every dotted cwd pointing at a directory that never exists,
    # so transcript_exists() said False and resume_set() silently dropped those
    # sessions from a restore (2026-08-02: 2 of 18 lost; see test_project_dir).
    return os.path.join(PROJECTS_DIR, re.sub(r"[^a-zA-Z0-9]", "-", cwd))


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
    for info in sessions.values():
        info.setdefault("vendor", "claude")
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
    spawns a dead 'No conversation found' tab).

    Vendor-aware: each entry carries the `vendor` its command comes from, so a
    Codex or Grok tab is reopened with that vendor's own resume verb rather
    than handed to `claude --resume`, which would only report an unknown id.

    Ordered to rebuild the tab bar as it was: sessions carrying a recorded `tab`
    position come first in visual order (window, then tab index), since callers
    spawn tabs left to right. Anything without one — no pid when the snapshot was
    taken, a session outside WezTerm, a pre-feature recovery file — sorts by cwd
    after them. Returns [{sessionId, cwd, tab?}]."""
    live = set(live_sessions()) | set(other_vendor_sessions())
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
            if not os.path.isdir(cwd):
                continue
            # Another vendor's transcript is nowhere near ~/.claude/projects,
            # so the snapshot records its path and that is what gets checked.
            vendor = s.get("vendor") or "claude"
            transcript = s.get("transcript")
            if vendor == "claude":
                if not transcript_exists(sid, cwd):
                    continue
            elif not (transcript and os.path.exists(transcript)):
                continue
            seen.add(sid)
            entry = {"sessionId": sid, "cwd": cwd, "vendor": vendor}
            tab = s.get("tab")
            if (isinstance(tab, list) and len(tab) == 2
                    and all(isinstance(v, int) for v in tab)):
                entry["tab"] = tab
            out.append(entry)
    out.sort(key=lambda s: (0, s["tab"][0], s["tab"][1], "")
             if s.get("tab") else (1, 0, 0, s["cwd"] + s["sessionId"]))
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


# --------------------------------------------------------------- other vendors
#
# The fleet is not Claude-only: Codex, Grok and friends run in the same tabs,
# compete for the same attention, and vanish from the picker the same way a
# Claude session does. They just publish nothing like ~/.claude/sessions, so
# each vendor needs a small adapter that returns the SAME record shape —
# {sessionId, cwd, pid, status, source, vendor, transcript} — and the views
# stay vendor-agnostic.
#
# Kept out of live_sessions() on purpose: claude-snapshot and claude-resume
# treat everything it returns as resumable with `claude --resume`, which is
# wrong for another vendor's id. Views that want the whole fleet call
# other_vendor_sessions() and merge.

CODEX_SESSIONS_DIR = os.path.join(HOME, ".codex", "sessions")


def _pids():
    for name in os.listdir("/proc"):
        if name.isdigit():
            yield int(name)


def _codex_rollouts(limit=60):
    """Codex transcripts, newest first. Laid out as sessions/YYYY/MM/DD/
    rollout-<iso>-<uuid>.jsonl, so the directory walk is already chronological
    and only the newest day or two is ever read."""
    paths = []
    for root, dirs, files in os.walk(CODEX_SESSIONS_DIR):
        dirs.sort(reverse=True)
        for f in files:
            if f.startswith("rollout-") and f.endswith(".jsonl"):
                paths.append(os.path.join(root, f))
    paths.sort(reverse=True)          # ISO timestamp in the name sorts as time
    return paths[:limit]


def _codex_meta(path):
    """First line of a rollout is a session_meta event carrying id and cwd.

    `subagent` marks the threads Codex spawns off a session: they are rollouts
    of their own, written at the same time and in the same directory as the
    conversation that owns them, and only `thread_source` tells them apart."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            ev = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if ev.get("type") != "session_meta":
        return None
    payload = ev.get("payload") or {}
    sid = payload.get("id") or payload.get("session_id")
    if not sid:
        return None
    return {"sessionId": sid, "cwd": payload.get("cwd") or "",
            "transcript": path,
            "subagent": bool(payload.get("parent_thread_id")
                             or payload.get("thread_source") == "subagent")}


def _open_rollouts(pid):
    """(mtime, path) for every rollout a process holds open.

    Codex appends to its session's rollout for as long as the session lives, so
    the file is right there in /proc/<pid>/fd — no guessing required. A resumed
    or forked session keeps its ancestors open too, hence the mtime: the one
    being written is the one most recently written to."""
    found, fddir = [], "/proc/%s/fd" % pid
    try:
        names = os.listdir(fddir)
    except OSError:
        return found
    prefix = CODEX_SESSIONS_DIR.rstrip(os.sep) + os.sep
    for name in names:
        try:
            target = os.readlink(os.path.join(fddir, name))
        except OSError:
            continue
        base = os.path.basename(target)
        if not (target.startswith(prefix)
                and base.startswith("rollout-") and base.endswith(".jsonl")):
            continue
        try:
            found.append((os.path.getmtime(target), target))
        except OSError:
            continue
    return found


def codex_sessions():
    """Live Codex sessions, identified by the rollout each one holds open.

    Codex keeps no pid registry, so liveness comes from /proc. Identity used to
    come from the newest rollout whose session_meta named the same cwd, which
    broke as soon as two Codex tabs shared a directory: they collapsed into one
    session, and the survivor was handed the *other* tab's transcript — so the
    status published from it (and with it the tab colour and CTRL+SHIFT+A) was
    the wrong tab's. The process already knows which session it is; ask it.

    Grouped by pane rather than cwd because the pane is what a session occupies
    here — and the TUI and its vendored child share one, so they fold together
    without a pid-tree walk."""
    groups = {}
    for pid in _pids():
        argv = _proc_argv(pid)
        if not argv:
            continue
        exe = argv[0]
        # the TUI is `node .../bin/codex ...` or the vendored binary; its
        # code-mode-host child shares the cwd and must not double-count
        if not (os.path.basename(exe) == "codex"
                or (len(argv) > 1 and os.path.basename(argv[1]) == "codex")):
            continue
        if any("code-mode-host" in a for a in argv):
            continue
        cwd = _proc_cwd(pid)
        if not cwd:
            continue
        # No pane means Codex is not running in a WezTerm tab; fall back to the
        # cwd, which is all the old attribution ever had anyway.
        pane = proc_pane_id(pid)
        g = groups.setdefault(("pane", pane) if pane else ("cwd", cwd),
                              {"pid": pid, "cwd": cwd, "rollouts": []})
        g["pid"] = min(g["pid"], pid)      # the TUI, not its vendored child
        g["rollouts"].extend(_open_rollouts(pid))

    out, claimed, blind = {}, set(), []
    for g in groups.values():
        if not g["rollouts"]:
            blind.append(g)               # /proc/<pid>/fd unreadable
            continue
        # A working session holds its subagents' rollouts open alongside its
        # own, all of them being appended to at once, so "newest" picks a
        # different thread from one second to the next. The conversation in
        # the tab is the one thread that nothing spawned.
        threads = []
        for mtime, path in g["rollouts"]:
            meta = _codex_meta(path)
            if meta:
                threads.append((mtime, meta))
        own = [t for t in threads if not t[1]["subagent"]] or threads
        if not own:
            blind.append(g)
            continue
        meta = max(own, key=lambda t: t[0])[1]
        sid = meta["sessionId"]
        if not _looks_like_session_id(sid) or sid in claimed:
            continue
        claimed.add(sid)
        out[sid] = {"sessionId": sid, "cwd": g["cwd"], "pid": g["pid"],
                    "status": None, "source": "proc", "vendor": "codex",
                    "transcript": meta["transcript"]}

    # Last resort for anything /proc would not name: the old cwd match.
    if blind:
        metas = [m for m in (_codex_meta(p) for p in _codex_rollouts()) if m]
        for g in blind:
            for m in metas:
                if m["cwd"] == g["cwd"] and m["sessionId"] not in claimed:
                    claimed.add(m["sessionId"])
                    out[m["sessionId"]] = {
                        "sessionId": m["sessionId"], "cwd": g["cwd"],
                        "pid": g["pid"], "status": None, "source": "proc",
                        "vendor": "codex", "transcript": m["transcript"],
                    }
                    break
    return out


GROK_SESSIONS_DIR = os.path.join(HOME, ".grok", "sessions")


def _grok_session_for_cwd(cwd):
    """Grok's newest session for one working directory.

    Layout is self-describing: sessions/<percent-encoded cwd>/<uuid>/, so the
    cwd needs no file read, the directory name IS the session id, and
    summary.json carries a written title — the one vendor that hands us a topic
    instead of making us parse for one."""
    root = os.path.join(GROK_SESSIONS_DIR, quote(cwd, safe=""))
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    best = None
    for sid in entries:
        chat = os.path.join(root, sid, "chat_history.jsonl")
        try:
            mtime = os.path.getmtime(chat)
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, sid, chat)
    if best is None:
        return None
    _, sid, chat = best
    topic = ""
    try:
        with open(os.path.join(root, sid, "summary.json"),
                  encoding="utf-8", errors="ignore") as f:
            topic = (json.load(f).get("session_summary") or "").strip()
    except (OSError, ValueError):
        pass
    return {"sessionId": sid, "cwd": cwd, "transcript": chat, "topic": topic}


def grok_sessions():
    """Live Grok sessions, matched to their chat history by working directory."""
    out = {}
    for pid in _pids():
        argv = _proc_argv(pid)
        if not argv or os.path.basename(argv[0]) not in ("grok", "agent"):
            continue
        cwd = _proc_cwd(pid)
        if not cwd:
            continue
        found = _grok_session_for_cwd(cwd)
        if not found or found["sessionId"] in out:
            continue
        found.update({"pid": pid, "status": None, "source": "proc",
                      "vendor": "grok"})
        out[found["sessionId"]] = found
    return out


VENDOR_ADAPTERS = {"codex": codex_sessions, "grok": grok_sessions}


def other_vendor_sessions():
    """Every live non-Claude session, same record shape as live_sessions()."""
    out = {}
    for name, adapter in VENDOR_ADAPTERS.items():
        try:
            out.update(adapter())
        except Exception:
            continue          # one broken vendor must never blank the picker
    return out


# How each vendor reopens a session by id. Kept beside the adapters so a new
# vendor lands in one place: discovery and resurrection are the same fact.
VENDOR_RESUME = {
    "claude": "claude --resume %s",
    "codex": "codex resume %s",
    "grok": "grok --resume %s",
}


def resume_command(sid, vendor="claude"):
    """Shell command that reopens this session, or None if the vendor has no
    known resume verb — better to skip a tab than spawn one that errors."""
    template = VENDOR_RESUME.get(vendor or "claude")
    return template % sid if template else None


# ── conversation text, vendor-agnostic ──────────────────────────────────────
# Every vendor writes its own JSONL schema, and each one replays scaffolding
# into the transcript as if a human had typed it. Search results are only
# readable once both are handled, so the "what did a person actually say"
# question lives here rather than being re-answered per tool.

REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)

# Preambles a vendor injects as a "user" turn. Identical in every session, and
# nothing anyone typed — matching them is noise, not a hit.
INJECTED = ("# AGENTS.md", "<INSTRUCTIONS>", "<environment_context>",
            "<user_instructions>", "<user_info>", "<system-reminder>",
            "<local-command-stdout>")

# A slash command replays as a user turn wrapped in <command-message>,
# <command-name>, <command-args>… — matched by shape so a new wrapper tag does
# not silently start reading as something a person typed.
COMMAND_RE = re.compile(r"^<command-[a-z]+>")


def clean_prompt(txt):
    """The part of a user turn a human actually typed, or '' if it is machinery."""
    txt = (txt or "").strip()
    m = QUERY_RE.search(txt)          # grok wraps the real prompt
    if m:
        txt = m.group(1).strip()
    txt = REMINDER_RE.sub("", txt).strip()
    if not txt or txt.startswith(INJECTED) or COMMAND_RE.match(txt):
        return ""
    return txt


def _blocks(content):
    """Text out of either shape a content field takes: a bare string, or a list
    of blocks of which only the text ones carry conversation."""
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or []
            if isinstance(b, dict) and b.get("type") in (None, "text", "input_text",
                                                         "output_text")]


def conversation_turns(path):
    """Yield (role, text) for the human-readable turns of a transcript, in
    order, for any vendor. Tool calls, sidechains and injected preambles are
    skipped: this is what the user could have scrolled past in the terminal."""
    codex = path.startswith(CODEX_SESSIONS_DIR)
    grok = path.startswith(GROK_SESSIONS_DIR)
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if codex:
                    if e.get("type") != "response_item":
                        continue
                    payload = e.get("payload") or {}
                    if payload.get("type") != "message":
                        continue
                    role, texts = payload.get("role"), _blocks(payload.get("content"))
                elif grok:
                    if e.get("synthetic_reason"):
                        continue
                    role, texts = e.get("type"), _blocks(e.get("content"))
                else:
                    if e.get("isSidechain") or e.get("isMeta"):
                        continue
                    role = e.get("type")
                    texts = _blocks((e.get("message") or {}).get("content"))
                if role not in ("user", "assistant"):
                    continue
                for txt in texts:
                    txt = clean_prompt(txt) if role == "user" else (txt or "").strip()
                    if txt:
                        yield role, txt
    except OSError:
        return


def first_prompt(path, limit=100):
    """The opening human prompt of a session — what identifies its terminal
    window to the person looking for it. '' when the transcript has none."""
    for role, txt in conversation_turns(path):
        if role == "user":
            one = " ".join(txt.split())
            return one[:limit] + ("…" if len(one) > limit else "")
    return ""
