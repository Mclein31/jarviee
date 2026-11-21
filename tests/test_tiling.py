"""Pure layout math for the Jarviee iTerm2 window: growth, tabs, closes, sizes."""
import unittest

import helpers  # noqa: F401  (puts the repo on sys.path)
from hub import tiling


def grow(n, aspect=1.6):
    """Add n panes the way the terminal does; returns the tree."""
    t = None
    for i in range(n):
        new = f"s{i + 1}"
        if t is None:
            t = new
        else:
            at, orient = tiling.next_split(t, aspect)
            t = tiling.split(t, at, orient, new)
    return t


class LayoutGrowth(unittest.TestCase):
    def test_pane_count_to_grid(self):
        self.assertEqual([tiling.grid_for(n) for n in range(1, 7)],
                         [(1, 1), (2, 1), (2, 2), (2, 2), (2, 3), (2, 3)])
        self.assertEqual(tiling.grid_for(9), (2, 3))   # capped: the 7th pane goes to a new tab

    def test_growth_gives_the_spec_layouts(self):
        # rows per column: full · side by side · one left + two stacked right · 2x2 · 2 cols x 3 rows
        want = {1: (1,), 2: (1, 1), 3: (1, 2), 4: (2, 2), 5: (2, 3), 6: (3, 3)}
        for n, shape in want.items():
            t = grow(n)
            self.assertEqual(tiling.shape(t), shape, n)
            cols, rows = tiling.grid_for(n)
            self.assertEqual((len(shape), max(shape)), (cols, rows), n)

    def test_every_split_has_equal_siblings(self):
        for n in range(1, 7):
            r = tiling.rects(grow(n))
            self.assertAlmostEqual(sum(w * h for _, _, w, h in r.values()), 1.0)
            widths = {round(w, 6) for _, _, w, _ in r.values()}
            self.assertLessEqual(len(widths), 2 if n > 1 else 1, n)

    def test_same_direction_split_adds_a_sibling(self):
        t = grow(4)                       # V[H[s1,s4], H[s2,s3]]
        t5 = tiling.split(t, "s3", "H", "s5")
        self.assertEqual(t5, {"o": "V", "c": [{"o": "H", "c": ["s1", "s4"]}, {"o": "H", "c": ["s2", "s3", "s5"]}]})

    def test_seventh_pane_opens_a_new_tab(self):
        tabs = [grow(6)]
        self.assertIsNone(tiling.pick_tab(tabs, 6))
        tabs.append("t2")
        self.assertEqual(tiling.pick_tab(tabs, 6), 1)
        self.assertEqual(tiling.pick_tab([grow(3), "x"], 6), 0)
        self.assertEqual(tiling.pick_tab([grow(2)], 2), None)


class Closing(unittest.TestCase):
    def test_close_collapses_and_flattens_like_iterm2(self):
        t = grow(4)                                        # V[H[s1,s4], H[s2,s3]]
        self.assertEqual(tiling.remove(t, "s4"), {"o": "V", "c": ["s1", {"o": "H", "c": ["s2", "s3"]}]})
        self.assertEqual(tiling.remove(tiling.remove(t, "s1"), "s4"), {"o": "H", "c": ["s2", "s3"]})
        nested = {"o": "V", "c": ["a", {"o": "H", "c": ["b", {"o": "V", "c": ["c", "d"]}]}]}
        self.assertEqual(tiling.remove(nested, "b"), {"o": "V", "c": ["a", "c", "d"]})
        self.assertIsNone(tiling.remove("only", "only"))

    def test_after_closes_the_next_split_halves_the_biggest_pane(self):
        t = tiling.remove(tiling.remove(grow(4), "s1"), "s4")   # H[s2, s3]: full-width rows
        at, orient = tiling.next_split(t, aspect=1.6)
        self.assertEqual((at, orient), ("s2", "V"))
        tall = tiling.next_split({"o": "V", "c": ["a", "b", "c"]}, aspect=1.0)
        self.assertEqual(tall, ("c", "H"))


class Rebalance(unittest.TestCase):
    def test_targets_share_cells_evenly(self):
        t = grow(5)                                         # V[H[s1,s4], H[s2,s3,s5]]
        want = tiling.targets(t, 211, 62)
        self.assertEqual(want["s1"], (105, 30))
        self.assertEqual(want["s4"], (105, 30))
        self.assertEqual({want[s] for s in ("s2", "s3", "s5")}, {(105, 19), (105, 20)})

    def test_measured_even_layout_needs_nothing_and_drift_is_found(self):
        t = grow(6)
        dims = {"s1": (105, 19), "s4": (105, 19), "s6": (105, 20), "s2": (106, 19), "s3": (106, 19), "s5": (106, 20)}
        self.assertEqual(tiling.uneven(t, dims), [])        # numbers measured on the laptop screen
        dims["s1"], dims["s4"] = (105, 30), (105, 10)       # a divider dragged by hand
        self.assertEqual(sorted(tiling.uneven(t, dims)), ["s1", "s4"])

    def test_size_adds_dividers(self):
        self.assertEqual(tiling.size(grow(2), {"s1": (105, 61), "s2": (106, 61)}), (212, 61))


if __name__ == "__main__":
    unittest.main()
