"""State shared by the run-lifecycle modules (launcher, progress, stopper, panerun, runjob).

Kept in one place so every module sees the same objects; `hub.launcher` re-exports them under their old names
(`launcher._procs`, `launcher.LIVE_RUNS`, ...), which other code and the tests read and patch.
"""

PROCS = {}       # run_id -> Popen (or panes.PaneProc) of a run this process started
THREADS = {}     # run_id -> the worker thread of that run
LIVE_RUNS = set()   # launched runs running as live CLI sessions in a pane: their Stop hook means "your turn"

LIVE = ("running", "waiting")
FINISHED_CLOSED = "finished (pane closed after its turn)"
LIVE_NOTE = "live session in its iTerm2 pane"
LIVE_UNATTENDED = 2   # agents.interactive for a queue / pipeline run in the real TUI: a finished turn ends it
# An unattended run whose turn ended while a background task it started (a backgrounded command, a Monitor, a
# background subagent) is still running: it stays "running" with this activity until a turn ends with nothing pending
# (or guards.background_wait_minutes passes). hooks/pane_runner.py matches the same text, keep them in step.
BG_WAIT = "waiting for its background task"
# An agy queue run whose transcript couldn't be found, so background tasks weren't checked: the pane checks its own
# screen for agy's "N task(s)" footer instead (hooks/pane_runner.py).
FINISHED_UNCHECKED = "finished (background tasks not checked)"


def launcher():
    """The `hub.launcher` module, looked up at call time.

    Code that moved out of launcher.py calls collaborators through it (`launcher().proc_name(...)`), so a test
    (or any caller) that patches `hub.launcher.<name>` still changes what runs, exactly as before the split.
    """
    from . import launcher as module
    return module
