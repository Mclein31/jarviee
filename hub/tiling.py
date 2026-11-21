"""Pane layout for the Jarviee iTerm2 window: split trees, tiling order and evenness checks.

A tab's panes form iTerm2's split tree. A leaf is a session id; a split is
{"o": "V" | "H", "c": [children]}: "V" = side by side (vertical divider), "H" = stacked.

The model follows what iTerm2 3.7 does (checked on this Mac, 2026-11-09):
- splitting a pane in its parent's direction adds a sibling, otherwise it nests;
- a new sibling starts at the split pane's size, then all siblings are scaled to fit;
- closing a pane scales its siblings up proportionally, drops one-child splits and merges
  a child split into a parent of the same direction.
So if every split is made where the siblings are already equal, the layout stays even with no
resizing at all. That matters because a native full screen window ignores scripted pane sizes.
"""

GAP_COLS, GAP_ROWS = 1, 2   # cells lost to a divider (rows include the per-pane title bar)

# Canonical layout per pane count, as (columns, rows in the tallest column).
GRID = {1: (1, 1), 2: (2, 1), 3: (2, 2), 4: (2, 2), 5: (2, 3), 6: (2, 3)}


def grid_for(n):
    """Columns × rows for n panes in one tab (n is capped at 6; more panes go to a new tab)."""
    return GRID[max(1, min(int(n), 6))]


def leaves(t):
    if t is None:
        return []
    if isinstance(t, str):
        return [t]
    return [x for c in t["c"] for x in leaves(c)]


def _copy(t):
    return t if isinstance(t, str) else {"o": t["o"], "c": [_copy(c) for c in t["c"]]}


def _path(t, sid, trail=()):
    if isinstance(t, str):
        return trail if t == sid else None
    for i, c in enumerate(t["c"]):
        p = _path(c, sid, trail + (i,))
        if p is not None:
            return p
    return None


def split(t, sid, orient, new):
    """The tree after iTerm2 splits pane `sid` ("V" or "H") and the new pane is `new`."""
    if t is None:
        return new
    t = _copy(t)
    path = _path(t, sid)
    if path is None:
        raise KeyError(sid)
    if not path:
        return {"o": orient, "c": [sid, new]}
    parent = t
    for i in path[:-1]:
        parent = parent["c"][i]
    i = path[-1]
    if parent["o"] == orient:
        parent["c"].insert(i + 1, new)
    else:
        parent["c"][i] = {"o": orient, "c": [sid, new]}
    return t


def remove(t, sid):
    """The tree after pane `sid` closes (None when it was the last one)."""
    if t is None or t == sid:
        return None
    if isinstance(t, str):
        return t
    kids = []
    for c in t["c"]:
        k = remove(c, sid)
        if k is None:
            continue
        if isinstance(k, dict) and k["o"] == t["o"]:
            kids.extend(k["c"])
        else:
            kids.append(k)
    if not kids:
        return None
    return kids[0] if len(kids) == 1 else {"o": t["o"], "c": kids}


def columns(t):
    """Panes per column, top to bottom, when the tree is 'columns of stacked panes'; else None."""
    if isinstance(t, str):
        return [[t]]
    if t["o"] != "V":
        return None
    cols = []
    for c in t["c"]:
        if isinstance(c, str):
            cols.append([c])
        elif c["o"] == "H" and all(isinstance(x, str) for x in c["c"]):
            cols.append(list(c["c"]))
        else:
            return None
    return cols


def shape(t):
    """Rows per column, e.g. (1, 2) = one pane left, two stacked right; None if not in columns."""
    cols = columns(t) if t is not None else []
    return tuple(len(c) for c in cols) if cols is not None else None


def rects(t, x=0.0, y=0.0, w=1.0, h=1.0):
    """{session: (x, y, w, h)} as fractions of the tab, siblings sharing equally (as iTerm2 keeps them)."""
    if isinstance(t, str):
        return {t: (x, y, w, h)}
    out, n = {}, len(t["c"])
    for i, c in enumerate(t["c"]):
        if t["o"] == "V":
            out.update(rects(c, x + w * i / n, y, w / n, h))
        else:
            out.update(rects(c, x, y + h * i / n, w, h / n))
    return out


def rect_of(t, sid, dims, x, y, w, h):
    """Screen rectangle (x, y, w, h) of pane `sid` inside the area (x, y, w, h), splitting each node in proportion to the
    measured sizes (a divider counts as one gap). None if the pane isn't in the tree."""
    if isinstance(t, str):
        return (x, y, w, h) if t == sid else None
    kids = t["c"]
    gap = GAP_COLS if t["o"] == "V" else GAP_ROWS
    sizes = [size(c, dims)[0 if t["o"] == "V" else 1] for c in kids]
    total = float(sum(sizes) + gap * (len(kids) - 1)) or 1.0
    span = w if t["o"] == "V" else h
    pos = 0.0
    for c, n in zip(kids, sizes):
        part = span * n / total
        sub = (x + pos, y, part, h) if t["o"] == "V" else (x, y + pos, w, part)
        hit = rect_of(c, sid, dims, *sub)
        if hit:
            return hit
        pos += part + span * gap / total
    return None


def next_split(t, aspect=1.6):
    """(pane to split, "V" | "H") to add one pane while keeping siblings equal.

    1 → side by side; then the column with the fewest panes (ties: the right one) gets one
    more pane under its last. That gives 1 / 2 / one left + two right / 2×2 / 2 columns × 3 rows.
    Any other tree (left over after closes): split the biggest pane along its long side.
    `aspect` is the tab's width / height.
    """
    if isinstance(t, str):
        return t, "V"
    cols = columns(t)
    if cols and len(cols) == 2:
        k = min(range(2), key=lambda i: (len(cols[i]), -i))
        return cols[k][-1], "H"
    r = rects(t)
    sid = max(r, key=lambda s: (round(r[s][2] * r[s][3], 6), r[s][0], -r[s][1]))
    _, _, w, h = r[sid]
    return sid, ("V" if w * aspect >= h else "H")


def grid_tree(cells):
    """The split tree iTerm2 builds for a grid, from [(column, row, session)] (1-based): columns side by side,
    each column's panes stacked."""
    cols = {}
    for c, r, sid in sorted(cells):
        cols.setdefault(c, []).append(sid)
    kids = [col[0] if len(col) == 1 else {"o": "H", "c": col} for _, col in sorted(cols.items())]
    if not kids:
        return None
    return kids[0] if len(kids) == 1 else {"o": "V", "c": kids}


def grid_split(t, cols, rows):
    """(pane to split, "V" | "H") that grows a tab back toward `cols` × `rows` with equal siblings, or None when
    the grid is full or the tree can't grow cleanly (then next_split takes over). A one-pane column split side by
    side adds a column; a short column gets one more pane under its last."""
    c = columns(t) if t is not None else None
    if not c:
        return None
    singles = [col for col in c if len(col) == 1]
    if len(c) < cols and singles:
        return singles[-1][0], "V"
    short = [col for col in c if len(col) < rows]
    return (short[0][-1], "H") if short else None


def reading_order(t):
    """Panes top row first, left to right (the order free grid panes are filled in)."""
    r = rects(t) if t is not None else {}
    return sorted(r, key=lambda s: (round(r[s][1], 6), round(r[s][0], 6)))


def pick_tab(tabs, max_panes=6, skip=()):
    """Index of the first tab with room for one more pane, or None to open a new tab. `skip`: tab indexes that
    belong to another kind of tab (the /orchestrate grid) and never take this kind of pane."""
    for i, t in enumerate(tabs):
        if i not in skip and len(leaves(t)) < max_panes:
            return i
    return None


def size(t, dims):
    """(columns, rows) a subtree spans, from the measured sizes {session: (cols, rows)}."""
    if isinstance(t, str):
        return dims[t]
    s = [size(c, dims) for c in t["c"]]
    n = len(s)
    if t["o"] == "V":
        return sum(a for a, _ in s) + GAP_COLS * (n - 1), max(b for _, b in s)
    return max(a for a, _ in s), sum(b for _, b in s) + GAP_ROWS * (n - 1)


def _share(total, n):
    base, extra = divmod(max(int(total), n), n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def targets(t, cols, rows):
    """{session: (cols, rows)} that gives every split equal children inside cols × rows."""
    if isinstance(t, str):
        return {t: (cols, rows)}
    n, out = len(t["c"]), {}
    if t["o"] == "V":
        for c, part in zip(t["c"], _share(cols - GAP_COLS * (n - 1), n)):
            out.update(targets(c, part, rows))
    else:
        for c, part in zip(t["c"], _share(rows - GAP_ROWS * (n - 1), n)):
            out.update(targets(c, cols, part))
    return out


def uneven(t, dims, tol=2):
    """Panes more than `tol` cells off an even layout (empty list = even)."""
    if t is None or any(s not in dims for s in leaves(t)):
        return []
    want = targets(t, *size(t, dims))
    return [s for s, (c, r) in want.items() if abs(dims[s][0] - c) > tol or abs(dims[s][1] - r) > tol]
