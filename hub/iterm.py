"""iTerm2 control for Jarviee: one window ("Jarviee" profile), one pane per run.

Every AppleScript below is a fixed string. Values (window and session ids, the pane command,
sizes) travel as osascript arguments (`on run argv`), so nothing is ever parsed as AppleScript.
Task text never reaches osascript at all: the pane runner sets its own title.
"""
import re
import subprocess

SID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class TerminalError(Exception):
    kind = "error"


class PermissionDenied(TerminalError):
    kind = "permission"


class WindowGone(TerminalError):
    kind = "gone"


HELPERS = r'''
on rect(txt)
	set AppleScript's text item delimiters to ","
	set p to text items of txt
	set AppleScript's text item delimiters to ""
	return {(item 1 of p) as integer, (item 2 of p) as integer, (item 3 of p) as integer, (item 4 of p) as integer}
end rect

on hubWindow(wid)
	tell application id "com.googlecode.iterm2"
		repeat with w in windows
			if (id of w) is wid and (count of tabs of w) > 0 then return (window id wid)   -- by id: an index reference changes as soon as the window moves
		end repeat
	end tell
	error "Jarviee window not found" number 1002
end hubWindow

on findSession(w, sid)
	tell application id "com.googlecode.iterm2"
		repeat with t in tabs of w
			repeat with s in sessions of t
				if (id of s) is sid then return {t, s}
			end repeat
		end repeat
	end tell
	return missing value
end findSession

on keepFocus(w, prevWin, wasFront, mode, target)
	-- mode "1": bring the Jarviee window forward. Otherwise give focus back to the window the
	-- owner was using, unless it sits on the Jarviee screen (restoring it would hide the Space).
	tell application id "com.googlecode.iterm2"
		if mode is "1" then
			activate
			select w
			set index of w to 1
			return
		end if
		if (not wasFront) or prevWin is missing value then return
		try
			if (id of prevWin) is (id of w) then return
			set b to bounds of prevWin
			set r to my rect(target)
			set cx to ((item 1 of b) + (item 3 of b)) / 2
			set cy to ((item 2 of b) + (item 4 of b)) / 2
			if cx >= (item 1 of r) and cx < (item 3 of r) and cy >= (item 2 of r) and cy < (item 4 of r) then return
			select prevWin
		end try
	end tell
end keepFocus
'''

PRELUDE = r'''
	tell application id "com.googlecode.iterm2"
		set wasFront to frontmost
		set prevWin to missing value
		try
			set prevWin to current window
		end try
	end tell
'''

# argv: command, focus, bounds ("" = leave to the profile), target screen rect, profile name
OPEN = HELPERS + r'''
on run argv
''' + PRELUDE + r'''
	tell application id "com.googlecode.iterm2"
		set w to missing value
		repeat with i from 1 to 20
			try
				set w to (create window with profile (item 5 of argv) command (item 1 of argv))
				exit repeat
			on error msg number num
				if num is -1743 or i is 20 then error msg number num
				delay 0.25
			end try
		end repeat
		if (item 3 of argv) is not "" then set bounds of w to my rect(item 3 of argv)
		my keepFocus(w, prevWin, wasFront, item 2 of argv, item 4 of argv)
		return ((id of w) as text) & " " & (id of current session of w)
	end tell
end run
'''

# argv: window id, session id, "V" | "H", command, focus, target screen rect
SPLIT = HELPERS + r'''
on run argv
''' + PRELUDE + r'''
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		set hit to my findSession(w, item 2 of argv)
		if hit is missing value then error "pane not found" number 1003
		set s to item 2 of hit
		if (item 3 of argv) is "V" then
			tell s to set n to (split vertically with same profile command (item 4 of argv))
		else
			tell s to set n to (split horizontally with same profile command (item 4 of argv))
		end if
		if (item 5 of argv) is "1" then select (item 1 of hit)   -- show the tab the pane landed in (auto-tile and /orchestrate tabs are separate)
		my keepFocus(w, prevWin, wasFront, item 5 of argv, item 6 of argv)
		return id of n
	end tell
end run
'''

# argv: window id, command, focus, target screen rect
TAB = HELPERS + r'''
on run argv
''' + PRELUDE + r'''
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		tell w to set t to (create tab with profile "Jarviee" command (item 2 of argv))
		my keepFocus(w, prevWin, wasFront, item 3 of argv, item 4 of argv)
		return id of current session of t
	end tell
end run
'''

# argv: "window" | "tab", window id ("" for a new window), first pane's command, free pane command, focus, bounds,
# target screen rect, profile name, columns, rows. Builds the whole grid in one call: columns side by side (each split
# off the last one, so they stay equal), then each column stacked. Returns the window id, then "column|row|session"
# lines.
GRID = HELPERS + r'''
on run argv
''' + PRELUDE + r'''
	tell application id "com.googlecode.iterm2"
		set nc to (item 9 of argv) as integer
		set nr to (item 10 of argv) as integer
		if (item 1 of argv) is "window" then
			set w to missing value
			repeat with i from 1 to 20
				try
					set w to (create window with profile (item 8 of argv) command (item 3 of argv))
					exit repeat
				on error msg number num
					if num is -1743 or i is 20 then error msg number num
					delay 0.25
				end try
			end repeat
			if (item 6 of argv) is not "" then set bounds of w to my rect(item 6 of argv)
			set s0 to current session of w
		else
			set w to my hubWindow((item 2 of argv) as integer)
			tell w to set t to (create tab with profile (item 8 of argv) command (item 3 of argv))
			set s0 to current session of t
		end if
		set tops to {s0}
		repeat with c from 2 to nc
			tell (item -1 of tops) to set n to (split vertically with same profile command (item 4 of argv))
			set end of tops to n
		end repeat
		set out to ((id of w) as text) & linefeed
		repeat with c from 1 to nc
			set s to item c of tops
			set out to out & c & "|1|" & (id of s) & linefeed
			repeat with r from 2 to nr
				tell s to set n to (split horizontally with same profile command (item 4 of argv))
				set s to n
				set out to out & c & "|" & r & "|" & (id of s) & linefeed
			end repeat
		end repeat
		my keepFocus(w, prevWin, wasFront, item 5 of argv, item 7 of argv)
		return out
	end tell
end run
'''

# argv: window id, session id. The pane's tty (e.g. /dev/ttys012), to check what runs in it before handing it a run.
TTY = HELPERS + r'''
on run argv
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		set hit to my findSession(w, item 2 of argv)
		if hit is missing value then error "pane not found" number 1003
		return tty of (item 2 of hit)
	end tell
end run
'''

# argv: window id, session id, text. Types one line into a free grid pane (its waiter reads the run id from it).
WRITE = HELPERS + r'''
on run argv
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		set hit to my findSession(w, item 2 of argv)
		if hit is missing value then error "pane not found" number 1003
		tell (item 2 of hit) to write text (item 3 of argv)
		return "written"
	end tell
end run
'''

# argv: window id, session id
CLOSE = HELPERS + r'''
on run argv
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		set hit to my findSession(w, item 2 of argv)
		if hit is missing value then return "missing"
		close (item 2 of hit)
		return "closed"
	end tell
end run
'''

# argv: window id[, "l,t,r,b" to move it]. Returns the window's bounds as "l,t,r,b".
BOUNDS = HELPERS + r'''
on run argv
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		if (count of argv) > 1 then set bounds of w to my rect(item 2 of argv)
		set b to bounds of w
		return ((item 1 of b) as text) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b)
	end tell
end run
'''

# argv: window id, session id. Switches to the Jarviee Space and selects the pane.
SELECT = HELPERS + r'''
on run argv
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		set hit to my findSession(w, item 2 of argv)
		activate
		select w
		set index of w to 1
		if hit is not missing value then
			select (item 1 of hit)
			select (item 2 of hit)
		end if
		return "selected"
	end tell
end run
'''

# argv: window id. One line per pane: "tab index|session id|columns|rows". (Inside an iTerm2 tell block
# `tab` is iTerm2's tab class, not the tab character, so the separator is "|".)
LIST = HELPERS + r'''
on run argv
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		set out to ""
		set k to 0
		repeat with t in tabs of w
			set k to k + 1
			repeat with s in sessions of t
				set out to out & k & "|" & (id of s) & "|" & (columns of s) & "|" & (rows of s) & linefeed
			end repeat
		end repeat
		return out
	end tell
end run
'''

# argv: window id, then "session,cols,rows"... Sizes resize the window, so its bounds are restored
# after: iTerm2 then scales every pane proportionally. Normal windows only (full screen ignores it).
RESIZE = HELPERS + r'''
on run argv
	tell application id "com.googlecode.iterm2"
		set w to my hubWindow((item 1 of argv) as integer)
		set b to bounds of w
		repeat with i from 2 to count of argv
			set AppleScript's text item delimiters to ","
			set p to text items of (item i of argv)
			set AppleScript's text item delimiters to ""
			set hit to my findSession(w, item 1 of p)
			if hit is not missing value then
				set columns of (item 2 of hit) to (item 2 of p) as integer
				set rows of (item 2 of hit) to (item 3 of p) as integer
			end if
		end repeat
		delay 0.3
		set bounds of w to b
		delay 0.3
		return "ok"
	end tell
end run
'''


def osascript(script, args=(), timeout=30):
    """Run a fixed script with argv values. Raises PermissionDenied / WindowGone / TerminalError."""
    try:
        r = subprocess.run(["osascript", "-", *[str(a) for a in args]], input=script,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TerminalError("iTerm2 didn't answer in time (a dialog may be open in it)")
    except OSError as e:
        raise TerminalError(f"osascript failed: {e}")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "-1743" in err or "Not authorized to send Apple events" in err:
            raise PermissionDenied(err)
        if "(1002)" in err or "(-1728)" in err:
            raise WindowGone(err)
        raise TerminalError(err[-300:] or f"osascript exited {r.returncode}")
    return r.stdout.strip()
