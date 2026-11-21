"""The day book: a quiet one-line summary of every job that finished today, spoken once on /endwork.

Jarviee no longer reads each job out as it ends (config `voice.live_reports: false`). Instead a background thread
summarises each finished job with the small model that already writes TLDRs (`voice.summarise_run`, Haiku, free
fallback = the end of the run's own output) and files it in <data dir>/daybook/YYYY-MM-DD.json. Typing `/endwork`
finishes any summaries still pending, builds one report of the whole day and speaks it. Nothing here is instant and
nothing needs to be: the summaries are ready long before the owner asks.
"""
import html as _html
import json
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import standup, voice

ENDWORK_RE = re.compile(r"^\s*/end[\s_-]*(work|owrk)\b\s*", re.I)    # also the typo "/endowrk"
SWEEP_S = 45
PER_SWEEP = 3          # summaries per pass: one small model call at a time, never a burst
FLUSH_MAX_S = 90       # /endwork waits at most this long for summaries still pending
SPOKEN_JOBS = 14       # more than this and the rest are only counted
COMMITS_PER_PROJECT = 8
GIT_TIMEOUT_S = 5


def is_endwork(text):
    return bool(ENDWORK_RE.match(text or ""))


def _key(row):
    return f"{row.get('id')}#{row.get('job_id') or ''}"


def _when(row):
    return row.get("ended") or row.get("updated") or 0


def counts(row):
    """Does this row belong in the day's record? A finished job always does. A window the owner closed by hand
    (stopped, or still marked running / waiting: a false "needs you") does when the agent got work done in it,
    because closing a window must never erase what the agent accomplished."""
    st = row.get("status")
    if st in ("done", "failed"):
        return True
    closed = st == "stopped" or (st in ("running", "waiting") and row.get("closed_by_user"))
    return bool(closed and (int(row.get("steps") or 0) > 0 or row.get("files")))


def _status(row):
    """How the book files a row: done, failed, or stopped (a window closed by hand, whatever it was labelled)."""
    return row["status"] if row.get("status") in ("done", "failed") else "stopped"


def commits_today(cfg, day=None):
    """{project: [subject, ...]} of what git recorded that day in every workspace project: the record that no closed
    window or lost transcript can touch. Never raises."""
    from . import obsidian
    lo, hi = standup.day_bounds(day)
    out = {}
    try:
        root = obsidian.workspace(cfg)
        dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_")) and (p / ".git").exists())
    except OSError:
        return out
    for p in dirs:
        try:
            r = subprocess.run(["git", "-C", str(p), "log", "--no-merges", f"--since=@{int(lo)}", f"--until=@{int(hi)}",
                                "--pretty=format:%s"], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            continue
        subjects = [" ".join(l.split())[:140] for l in (r.stdout or "").splitlines() if l.strip()] if r.returncode == 0 else []
        if subjects:
            out[p.name] = subjects
    return out


def _first_sentence(text, limit=190):
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:limit].rstrip()


def _job_line(j):
    """Short readable summary for one job in an email/WhatsApp day report."""
    title = (j.get("title") or "").strip()
    summary = _first_sentence(voice.speakable(j.get("summary") or "", 260))
    if summary and title and title.lower() not in summary.lower():
        return title, summary
    return title or summary or "Untitled job", summary if summary and summary != title else ""


def _icon(status):
    return {"failed": "❌", "stopped": "⏹"}.get(status, "✅")


def _commit_lines(rep):
    out = []
    for name, subjects in (rep.get("commits") or {}).items():
        out.append(f"── {name}: {len(subjects)} commit{'s' if len(subjects) != 1 else ''} ──")
        out += [f"• {s}" for s in subjects[:COMMITS_PER_PROJECT]]
        if len(subjects) > COMMITS_PER_PROJECT:
            out.append(f"• …and {len(subjects) - COMMITS_PER_PROJECT} more")
        out.append("")
    return out


def email_text(rep):
    """Readable plain-text day report with each project separated into its own section."""
    if not rep.get("jobs") and not rep.get("commits"):
        return rep.get("text") or "Nothing has finished today yet."
    out = [rep.get("head") or rep.get("text") or "", ""]
    for name, items in (rep.get("projects") or {}).items():
        out.append(f"── {name} ({len(items)}) ──")
        for j in items:
            title, summary = _job_line(j)
            out.append(f"{_icon(j.get('status'))} {title}" + (f" — {summary}" if summary else ""))
        out.append("")
    out += _commit_lines(rep)
    if rep.get("waiting"):
        w = rep["waiting"]
        out.append(f"⏳ {w} still need{'s' if w == 1 else ''} you.")
    return "\n".join(out).strip()


def email_html(rep):
    """HTML day report with each project in its own card."""
    from . import mailout
    cards = []
    for name, items in (rep.get("projects") or {}).items():
        rows = []
        for j in items:
            st = j.get("status")
            pill_bg, pill_fg = {"failed": ("#fee2e2", "#b91c1c"), "stopped": ("#e2e8f0", "#475569")}.get(st, ("#dcfce7", "#15803d"))
            pill_label = {"failed": "Failed", "stopped": "Closed"}.get(st, "Done")
            title, summary = _job_line(j)
            summary_html = (f'<div style="font-size:13px;line-height:1.45;color:#475569;margin-top:3px;">'
                            f'{_html.escape(summary)}</div>' if summary else "")
            rows.append(
                '<div style="padding:8px 0;border-bottom:1px solid #f1f5f9;">'
                f'<span style="display:inline-block;font-size:11px;font-weight:600;color:{pill_fg};'
                f'background:{pill_bg};padding:1px 7px;border-radius:999px;margin-right:6px;">{pill_label}</span>'
                f'<span style="font-size:14px;font-weight:600;color:#0f172a;">{_html.escape(title)}</span>'
                f'{summary_html}</div>'
            )
        # Remove the bottom border on the last item inside each project card
        if rows:
            rows[-1] = rows[-1].replace("border-bottom:1px solid #f1f5f9;", "", 1)
        badge = f"{len(items)} job{'s' if len(items) != 1 else ''}"
        cards.append(mailout.card_html(name, "".join(rows), badge=badge))
    for name, subjects in (rep.get("commits") or {}).items():
        shown = "".join(f'<div style="font-size:13px;color:#334155;padding:2px 0;">• {_html.escape(x)}</div>'
                        for x in subjects[:COMMITS_PER_PROJECT])
        more = len(subjects) - COMMITS_PER_PROJECT
        shown += f'<div style="font-size:12px;color:#64748b;">…and {more} more</div>' if more > 0 else ""
        cards.append(mailout.card_html(f"{name} · git", shown, badge=f"{len(subjects)} commit{'s' if len(subjects) != 1 else ''}"))
    w = rep.get("waiting") or 0
    footer = f"{w} still need{'s' if w == 1 else ''} you." if w else ""
    return mailout.wrap_html(f"Your day · {rep.get('date', '')}", rep.get("head") or "", "".join(cards), footer=footer)


class Daybook:
    def __init__(self, cfg, store, summarise=None, clock=time.time):
        self.cfg, self.store, self.clock = cfg, store, clock
        self.summarise = summarise or (lambda row, status: voice.summarise_run(row, status, cfg=cfg))
        self.dir = Path(cfg["db_path"]).parent / "daybook"
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread = None

    # ---- storage ----
    def _path(self, day):
        return self.dir / f"{day}.json"

    def _load(self, day):
        try:
            return json.loads(self._path(day).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, day, entries):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(day).with_suffix(".tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path(day))
        except OSError:
            pass

    # ---- collecting ----
    def _today(self):
        lo, hi = standup.day_bounds(datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d"))
        return lo, hi

    def finished_today(self):
        lo, hi = self._today()
        return [r for r in self.store.all() if r.get("id") and counts(r) and lo <= _when(r) < hi]

    def pending(self):
        day = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        have = self._load(day)
        return [r for r in self.finished_today() if (have.get(_key(r)) or {}).get("status") != _status(r)]

    def sweep(self, limit=PER_SWEEP, deadline=None):
        """Summarise up to `limit` finished jobs not yet in today's book. Returns how many were added."""
        added = 0
        with self._lock:
            day = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
            entries = self._load(day)
            for row in self.pending()[:limit]:
                if deadline and time.time() > deadline:
                    break
                try:
                    text = self.summarise(row, _status(row)) or ""
                except Exception:
                    text = ""
                final = voice.final_answer(row)
                text = text or _first_sentence(voice.speakable(final, 400))
                entries[_key(row)] = {
                    "id": row.get("id"), "job_id": row.get("job_id"), "status": _status(row),
                    "project": row.get("project") or "", "tool": row.get("tool"), "model": row.get("model"),
                    "title": row.get("title") or (row.get("task") or "")[:80], "summary": text,
                    "started": row.get("started"), "ended": _when(row),
                    "closed": bool(row.get("closed_by_user")),
                    # kept in the book itself, so history outlives the window, the log and the transcript
                    "final": final[-1200:], "steps": int(row.get("steps") or 0)}
                added += 1
            if added:
                self._save(day, entries)
        return added

    def flush(self, max_s=FLUSH_MAX_S):
        """Finish every pending summary (bounded): what /endwork does before reading the day."""
        deadline = time.time() + max_s
        while self.pending() and time.time() < deadline:
            if not self.sweep(limit=PER_SWEEP, deadline=deadline):
                break

    # ---- the report ----
    def report(self, day=None):
        day = day or datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        jobs = sorted(self._load(day).values(), key=lambda e: e.get("ended") or 0)
        # "needs you" counts only agents whose window is still there: a closed one is history, not a request
        waiting = [r for r in self.store.all()
                   if r.get("status") == "waiting" and not r.get("cleared") and not r.get("closed_by_user")]
        commits = commits_today(self.cfg, day)
        done = [j for j in jobs if j["status"] == "done"]
        failed = [j for j in jobs if j["status"] == "failed"]
        closed = [j for j in jobs if j["status"] == "stopped"]
        secs = sum(max(0, int((j.get("ended") or 0) - (j.get("started") or 0))) for j in jobs if j.get("started"))
        projects = {}
        for j in jobs:
            projects.setdefault(j.get("project") or "no project", []).append(j)
        n_commits = sum(len(v) for v in commits.values())
        if not jobs and not commits:
            return {"date": day, "jobs": 0, "waiting": len(waiting), "text": "Nothing has finished today yet.",
                    "projects": {}, "commits": {}}
        mins = secs // 60
        if jobs:
            head = f"That's the day. {len(done)} job{'s' if len(done) != 1 else ''} done"
            head += f", {len(failed)} failed" if failed else ""
            head += f", {len(closed)} closed early" if closed else ""
            head += f", across {len(projects)} project{'s' if len(projects) != 1 else ''}"
            head += f", about {mins // 60} hours {mins % 60} minutes of agent time." if mins >= 60 else f", about {mins} minutes of agent time." if mins else "."
        else:
            head = "That's the day. No agent job is on record, but git has the work."
        parts, shown = [head], 0
        for name, js in projects.items():
            lines = []
            for j in js:
                if shown >= SPOKEN_JOBS:
                    break
                s = _first_sentence(voice.speakable(j.get("summary") or "", 260)) or voice.sanitise_spoken(j.get("title"), 90)
                lines.append(("It failed: " if j["status"] == "failed" else "") + s)
                shown += 1
            if lines:
                parts.append(f"In {voice.sanitise_spoken(name, 40)}: " + " ".join(l if l.endswith((".", "!", "?")) else l + "." for l in lines))
        if len(jobs) > shown:
            parts.append(f"And {len(jobs) - shown} smaller job{'s' if len(jobs) - shown != 1 else ''}.")
        if n_commits:
            parts.append(f"Git recorded {n_commits} commit{'s' if n_commits != 1 else ''} across "
                         f"{len(commits)} project{'s' if len(commits) != 1 else ''}.")
        if waiting:
            parts.append(f"{len(waiting)} still need{'s' if len(waiting) == 1 else ''} you.")
        return {"date": day, "jobs": len(jobs), "done": len(done), "failed": len(failed), "closed": len(closed),
                "waiting": len(waiting), "head": head, "text": " ".join(parts), "commits": commits,
                "projects": {k: [{"title": j["title"], "status": j["status"], "summary": j.get("summary")}
                                 for j in v] for k, v in projects.items()}}

    def endwork(self, speaker=None):
        """Finish pending summaries, build today's report, speak it. Returns the report plus whether it was spoken."""
        self.flush()
        rep = self.report()
        speaker = speaker if speaker is not None else voice.ACTIVE
        rep["spoken"] = bool(speaker and speaker.speak(rep["text"], event="endwork"))
        self.send_summary(rep)
        return rep

    def send_summary(self, rep, send=None):
        """The day's report to WhatsApp or email too, when one is on (in the background)."""
        from . import channels, mailout
        direct = None if send else channels.summary_sender(self.cfg, "summary")
        if send is None and direct is None:
            return False
        if not rep.get("jobs") and not rep.get("commits"):
            return False
        args = (self.cfg, f"Your day · {rep['date']}", email_text(rep))
        kwargs = {"html": email_html(rep)} if direct is mailout.send else {}
        if send is not None:
            send(*args, **kwargs)
        else:
            threading.Thread(target=direct, args=args, kwargs=kwargs, daemon=True).start()
        return True

    # ---- background ----
    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarviee-daybook")
        self._thread.start()
        return self

    def _loop(self):
        while not self._halt.wait(SWEEP_S):
            try:
                self.sweep()
            except Exception as e:
                print(f"[daybook] {e!r}")

    def stop(self):
        self._halt.set()


ACTIVE = None   # the running server's Daybook
