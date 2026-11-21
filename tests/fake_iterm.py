"""A stand-in for osascript + iTerm2: records every (script, argv) and can run pane commands."""
import shlex
import signal
import subprocess
from pathlib import Path

import helpers  # noqa: F401
from hub import displays, iterm, terminal

NAMES = {id(iterm.OPEN): "open", id(iterm.SPLIT): "split", id(iterm.TAB): "tab", id(iterm.CLOSE): "close",
         id(iterm.SELECT): "select", id(iterm.LIST): "list", id(iterm.RESIZE): "resize", id(iterm.BOUNDS): "bounds",
         id(iterm.GRID): "grid", id(iterm.TTY): "tty", id(iterm.WRITE): "write"}
SCREENS = displays.to_top_left([
    {"index": 0, "name": "MSI MP251L E2", "builtin": False, "frame": [0, 0, 1920, 1080], "visible": [0, 0, 1920, 1050]},
    {"index": 1, "name": "Built-in Retina Display", "builtin": True, "frame": [1920, -113, 1710, 1112],
     "visible": [1920, -113, 1710, 1074.5]},
])


class FakeITerm:
    def __init__(self, spawn=False, env=None):
        self.calls, self.tabs, self.n, self.spawn, self.env, self.procs = [], [], 0, spawn, env, {}
        self.fail, self.closed = None, []
        self.bounds = "1920,119,3630,1193"   # where a full screen window lands on the laptop

    def _new(self, cmd):
        self.n += 1
        sid = f"S{self.n}"
        if self.spawn:   # what iTerm2 does with `command`: run it in the new pane
            self.procs[sid] = subprocess.Popen(shlex.split(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, env=self.env)
        return sid

    def __call__(self, script, args=()):
        args = [str(a) for a in args]
        self.calls.append((NAMES[id(script)], args, script))
        if self.fail:
            raise self.fail
        if script is iterm.OPEN:
            sid = self._new(args[0])
            self.tabs = [[sid]]
            return f"100 {sid}"
        if script is iterm.SPLIT:
            tab = next(t for t in self.tabs if args[1] in t)
            sid = self._new(args[3])
            tab.insert(tab.index(args[1]) + 1, sid)
            return sid
        if script is iterm.GRID:   # mode, window, first command, free command, focus, bounds, target, profile, cols, rows
            cols, rows = int(args[8]), int(args[9])
            cells = [(c, r) for c in range(1, cols + 1) for r in range(1, rows + 1)]
            sids = {cell: self._new(args[2] if cell == (1, 1) else args[3]) for cell in cells}
            if args[0] == "window":
                self.tabs = [list(sids.values())]
            else:
                self.tabs.append(list(sids.values()))
            return "100\n" + "\n".join(f"{c}|{r}|{sids[(c, r)]}" for c, r in cells)
        if script is iterm.TTY:
            if not any(args[1] in t for t in self.tabs):
                raise iterm.TerminalError("pane not found (1003)")
            return f"/dev/ttys{int(args[1][1:]):03d}"
        if script is iterm.WRITE:
            return "written"
        if script is iterm.TAB:
            sid = self._new(args[1])
            self.tabs.append([sid])
            return sid
        if script is iterm.CLOSE:
            for t in self.tabs:
                if args[1] in t:
                    t.remove(args[1])
                    p = self.procs.pop(args[1], None)
                    if p:
                        self.closed.append(p)
                        if p.poll() is None:
                            p.send_signal(signal.SIGHUP)   # a closed pane hangs up its process
                    self.tabs = [x for x in self.tabs if x]
                    return "closed"
            return "missing"
        if script is iterm.BOUNDS:
            if len(args) > 1:
                self.bounds = args[1]
            return self.bounds
        if script is iterm.LIST:
            if not self.tabs:
                raise iterm.WindowGone("Jarviee window not found (1002)")
            return "\n".join(f"{k + 1}|{s}|80|24" for k, t in enumerate(self.tabs) for s in t)
        return "ok"

    def names(self, skip=("list", "bounds")):
        return [c[0] for c in self.calls if c[0] not in skip]

    def args_of(self, name):
        return [c[1] for c in self.calls if c[0] == name]

    def stop_all(self):
        for p in [*self.procs.values(), *self.closed]:
            if p.poll() is None:
                p.kill()
            p.wait(5)


def make_terminal(cfg, fake, tmp, **kw):
    cfg.setdefault("terminal", {})["mode"] = "iterm2"
    t = terminal.Terminal(cfg, run=fake, screens=lambda *a: [dict(s) for s in SCREENS], problems=lambda: [],
                          state_path=Path(tmp) / "terminal.json", profile_path=Path(tmp) / "jarviee.json",
                          installed=True, **{"front": lambda: "", "activate": lambda b: None, **kw})
    t.reload_wait = t.place_wait = 0
    return t
