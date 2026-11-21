"""One combined report for a batch of agents, instead of one report per window.

A batch opens with every job running at that moment. Jarviee then waits until each member has finished, is
blocked on the owner, has failed or was closed, and sends one report covering all of them. A member that blocks
on the owner gets one short line straight away. Jobs that start after the batch opened count toward the next
report. Members are jobs (row id + job id), so a terminal given a new job joins the next batch with it.

Pure state machine: `observe(rows)` returns actions for the alert watcher to deliver.
"""
import os
import re

from . import alerts, voice
from .titles import make_title

LAST = {}   # the latest combined report, for /api/state
SETTLED = ("done", "failed", "needs", "replied", "closed")
LABEL = {"done": "Done", "failed": "Failed", "needs": "Needs you", "replied": "Replied", "closed": "Closed by you",
         "working": "Still working"}
MAX_WAIT_S = 300   # a finished agent waits at most this long for the rest of its batch (alerts.report_max_wait_s)


def key_of(row):
    return f"{row.get('id')}#{row.get('job_id') or ''}"


def outcome_of(row):
    """None while the job is still working; else one of SETTLED."""
    if row is None or row.get("cleared"):
        return "closed"
    st = row.get("status")
    if st in ("running", "queued"):
        return None
    if st == "waiting":
        return "replied" if alerts.section_of(st, row.get("activity")) == "responded" else "needs"
    if st == "stopped":
        return "closed"
    if st in ("done", "failed"):
        return st
    return None


class Batcher:
    def __init__(self):
        self.members = {}     # key -> {"row": latest row, "outcome": None | SETTLED}
        self.known = set()    # jobs already reported (or present at startup): never reported again
        self.blocked = set()  # jobs that got their short "needs you" line and haven't resumed since

    def seed(self, rows):
        """Jobs already finished at startup are history, not news. Jobs still running are reported when they finish:
        a hub restart must not swallow the report for work that was in flight."""
        for r in rows:
            if r.get("id") and outcome_of(r) is not None:
                self.known.add(key_of(r))

    @property
    def open(self):
        return bool(self.members)

    def observe(self, rows, now=None, max_wait=None):
        """[("blocked", row)...] for jobs that just blocked on the owner, then at most one ("report", [members]).

        A long-running agent must not hold the report back for ever: once a settled member has waited `max_wait`
        seconds, the settled ones are reported and the ones still working ("working") carry over to the next batch."""
        rows = [r for r in rows if r.get("id") and r.get("source") != "subagent"]   # a subagent reports via its parent
        by_key = {key_of(r): r for r in rows}
        actions = []
        for k, row in by_key.items():   # the short line goes out at once, whichever batch the job belongs to
            if k in self.known:
                continue
            out = outcome_of(row)
            if out == "needs" and k not in self.blocked:
                self.blocked.add(k)
                actions.append(("blocked", row))
            elif out is None:
                self.blocked.discard(k)
        if not self.members:   # open a batch: every new job running now, plus any that ran start to finish unseen
            self.members = {k: {"row": r, "outcome": outcome_of(r)} for k, r in by_key.items() if k not in self.known}
        ids = {r["id"] for r in rows}
        for k, m in self.members.items():
            row = by_key.get(k)
            if row is None:
                # the row moved on to a new job (you typed the next one: this one's turn was over), or it was cleared
                m["outcome"] = m["outcome"] or ("replied" if m["row"].get("id") in ids else "closed")
            else:
                m["row"], m["outcome"] = row, outcome_of(row)
            if m["outcome"] in SETTLED:
                m.setdefault("settled_at", now)
            else:
                m.pop("settled_at", None)
        if not self.members:
            return actions
        settled = {k: m for k, m in self.members.items() if m["outcome"] in SETTLED}
        if len(settled) == len(self.members):
            report = self._close(settled)
            actions.append(("report", report))
        elif settled and max_wait and now is not None and any(
                m.get("settled_at") is not None and now - m["settled_at"] >= max_wait for m in settled.values()):
            working = [dict(m["row"], outcome="working") for k, m in self.members.items() if k not in settled]
            report = self._close(settled)
            self.members = {k: m for k, m in self.members.items() if k not in settled}
            actions.append(("report", report + working))
        return actions

    def _close(self, settled):
        out = [dict(m["row"], outcome=m["outcome"]) for m in settled.values()]
        self.known.update(settled)
        self.blocked -= set(settled)
        self.members = {}
        return out


# ---------- wording ----------
def _title(row):
    return voice.sanitise_spoken(row.get("title") or row.get("task") or row.get("id"), 60) or "An agent"


def _took(row):
    started, ended = row.get("started"), row.get("ended")
    return voice._span(ended - started) if started and ended and ended >= started else ""


def _detail(row, now=None, speech=False):
    """What follows the title: how long it took, how much it did, what it changed, or why it stopped.
    Built from row metadata only (times, step count, file names, the mapped reason); run output is never read."""
    out = row.get("outcome")
    plan = row.get("plan")
    parts = []
    if out == "done":
        took = _took(row)
        if took:
            parts.append(f"in {took}")
        steps = int(row.get("steps") or 0)
        if plan:
            parts.append(f"{plan['runs']} agent runs" + (f", {plan['rounds']} fix round{'s' if plan['rounds'] != 1 else ''}"
                                                       if plan.get("rounds") else ""))
        elif steps:
            parts.append(f"{steps} step{'s' if steps != 1 else ''}")
        files = voice._files_phrase(row.get("files"), names=not speech).replace("It changed", "changed")
        parts.append(files or ("changed no files" if parts else ""))
    elif out in ("needs", "failed"):
        if out == "failed":
            took = _took(row)
            if took:
                parts.append(f"after {took}")
        if plan:
            parts.append(voice.sanitise_spoken(plan.get("note"), 100))
        else:
            parts.append(voice.build_reason(row, "waiting" if out == "needs" else "failed"))
        if out == "needs" and now is not None and row.get("updated"):
            waited = now - float(row["updated"])
            if waited >= 60:
                parts.append(f"waiting {voice._span(waited)}")
        if out == "failed" and row.get("files"):
            parts.append(voice._files_phrase(row.get("files"), names=not speech).replace("It changed", "it had changed"))
    return ", ".join(p for p in parts if p)


def blocked_line(row, now=None):
    """The one short line sent the moment an agent blocks on the owner."""
    why = voice.build_reason(row, "waiting")
    ws = voice.sanitise_spoken(os.path.basename(str(row.get("workspace") or "").rstrip("/\\")), 30)
    return f"{_title(row)}{f' in {ws}' if ws else ''} needs you" + (f": {why}" if why else "") + "."


def counts(members):
    c = {}
    for m in members:
        c[m["outcome"]] = c.get(m["outcome"], 0) + 1
    return c


def headline(members):
    c = counts(members)
    parts = [f"{c[k]} {LABEL[k].lower()}" for k in ("done", "needs", "failed", "replied", "closed", "working") if c.get(k)]
    n = len(members) - c.get("working", 0)
    noun = "pipeline" if members and all(m.get("plan") for m in members) else "agent"
    lead = f"{n} {noun}{'s' if n != 1 else ''} finished" + (f", {c['working']} still working" if c.get("working") else "")
    return lead + ": " + ", ".join(p for p in parts if not p.endswith("still working"))


def _short_title(row, words=8):
    t = _title(row).split()
    return " ".join(t[:words]).rstrip(",;:")


def report_lines(members, summaries=None, now=None, speech=False):
    """One line per agent, grouped: what needs you first, then what failed, then the rest.
    Written: 'Done: <title> (<project>), <summary or detail>'. Spoken: with a summary of what the job did, that
    summary is the line ('Done in agent-hub: I rebuilt ...'); the title made from the owner's prompt is only a
    fallback, shortened, and file names are never read out."""
    order = {"needs": 0, "failed": 1, "done": 2, "replied": 3, "closed": 4, "working": 5}
    lines = []
    for m in sorted(members, key=lambda m: order.get(m["outcome"], 9)):
        summary = (summaries or {}).get(key_of(m))
        ws = os.path.basename(str(m.get("workspace") or "").rstrip("/\\"))
        label = LABEL.get(m["outcome"], m["outcome"])
        if speech:
            if summary:
                lines.append(f"{label}{f' in {ws}' if ws else ''}: {voice.speakable(summary)}")
            else:
                what = _detail(m, now, speech=True)
                lines.append(f"{label}: {_short_title(m)}{f' in {ws}' if ws else ''}" + (f", {what}" if what else ""))
            continue
        what = summary or _detail(m, now)
        where = f" ({ws})" if ws else ""
        lines.append(f"{label}: {_title(m)}{where}" + (f", {what}" if what else ""))
    return lines


def report_text(members, summaries=None, now=None):
    return headline(members) + ".\n" + "\n".join(report_lines(members, summaries, now))


def report_speech(members, summaries=None, now=None):
    """The spoken version: what each job did, in sentences."""
    return headline(members) + ". " + " ".join(x.rstrip(".") + "." for x in report_lines(members, summaries, now, speech=True))


# ---------- one report per pipeline plan ----------
PLAN_RE = re.compile(r"\bplan task (plan-[0-9a-f]+)\.")
PLAN_OPEN = ("draft", "approved", "running")
PLAN_OUTCOME = {"done": "done", "failed": "failed", "cancelled": "closed"}


def plan_of(row):
    """Id of the plan whose task this run executes (the scheduler notes it in the run's reasons), or None."""
    reasons = row.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    for r in reasons:
        m = PLAN_RE.search(str(r))
        if m:
            return m.group(1)
    return None


class PlanWatch:
    """A pipeline plan runs as several agents (research, build, test, review, fixes). They are one piece of work,
    so it is reported once, when the plan ends, instead of once per phase.

    `lookup(plan_id)` returns {"status", "goal", "pipeline": dict or None} or None when it cannot tell; a run whose
    plan is unknown or is not a pipeline is left to the ordinary per-batch reports.
    """

    def __init__(self, lookup):
        self.lookup = lookup
        self.pipeline = {}   # plan id -> bool, once known
        self.active = {}     # plan id -> {run id: latest row}
        self.done = set()    # plans already reported (or finished before startup)

    def _brief(self, pid):
        try:
            return self.lookup(pid)
        except Exception:
            return None

    def seed(self, rows):
        for r in rows:
            pid = plan_of(r)
            if pid and pid not in self.pipeline:
                b = self._brief(pid)
                if b:
                    self.pipeline[pid] = bool(b.get("pipeline"))
                    if b["status"] not in PLAN_OPEN:
                        self.done.add(pid)

    def _is_pipeline(self, pid):
        if pid not in self.pipeline:
            b = self._brief(pid)
            if b is None:
                return False
            self.pipeline[pid] = bool(b.get("pipeline"))
        return self.pipeline[pid]

    def is_pipeline_row(self, row):
        pid = plan_of(row)
        return bool(pid and self._is_pipeline(pid))

    def split(self, rows):
        """(rows for the ordinary batches, [member dict of each pipeline plan that just ended])."""
        kept = []
        for r in rows:
            pid = plan_of(r)
            if pid and self._is_pipeline(pid):
                if pid not in self.done and r.get("id"):
                    self.active.setdefault(pid, {})[r["id"]] = r
            else:
                kept.append(r)
        ended = []
        for pid in list(self.active):
            b = self._brief(pid)
            if b and b["status"] not in PLAN_OPEN:
                ended.append(self._member(pid, b, list(self.active.pop(pid).values())))
                self.done.add(pid)
        return kept, ended

    @staticmethod
    def _member(pid, brief, runs):
        pipe = brief.get("pipeline") or {}
        files = []
        for r in runs:
            files += [f for f in (r.get("files") or []) if f not in files]
        starts = [r["started"] for r in runs if r.get("started")]
        ends = [r["ended"] for r in runs if r.get("ended")]
        last = max(runs, key=lambda r: r.get("ended") or r.get("updated") or 0) if runs else {}
        return {"id": pid, "job_id": "", "title": make_title(brief.get("goal") or "") or pid,
                "outcome": PLAN_OUTCOME.get(brief["status"], "done"),
                "files": files, "steps": sum(int(r.get("steps") or 0) for r in runs),
                "started": min(starts) if starts else None, "ended": max(ends) if ends else None,
                "workspace": last.get("workspace"), "updated": last.get("updated"),
                "plan": {"runs": len(runs), "rounds": int(pipe.get("round") or 0), "note": pipe.get("note") or ""}}
