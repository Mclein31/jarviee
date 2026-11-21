"""Carousel tab strip CSS: parses cleanly, uses only defined design tokens, covers every status colour."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "hub" / "static"
START = "/* ---------- horizontal carousel tab strip ---------- */"


def strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def carousel_block():
    css = (STATIC / "pages.css").read_text(encoding="utf-8")
    assert START in css, "carousel section marker missing from pages.css"
    return strip_comments(css[css.index(START):])


def job_tab_block():
    css = (STATIC / "graph.css").read_text(encoding="utf-8")
    i = css.index("job tabs above the pipeline")
    return strip_comments(css[i:])


def defined_tokens():
    names = set()
    for f in STATIC.glob("*.css"):
        names |= set(re.findall(r"(--[\w-]+)\s*:", strip_comments(f.read_text(encoding="utf-8"))))
    return names


class CssWellFormed(unittest.TestCase):
    def test_braces_and_parens_balanced_in_every_stylesheet(self):
        for f in sorted(STATIC.glob("*.css")):
            with self.subTest(sheet=f.name):
                css = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', '""', strip_comments(f.read_text(encoding="utf-8")))
                depth = 0
                for ch in css:
                    depth += (ch == "{") - (ch == "}")
                    self.assertGreaterEqual(depth, 0, "closing brace without an opener")
                self.assertEqual(depth, 0, "unbalanced braces")
                self.assertEqual(css.count("("), css.count(")"), "unbalanced parentheses")

    def test_every_declaration_block_ends_cleanly(self):
        """No rule with an empty selector and no `;;`-style or unterminated declaration in the new blocks."""
        for name, block in (("pages.css carousel", carousel_block()), ("graph.css job tabs", job_tab_block())):
            with self.subTest(block=name):
                self.assertNotRegex(block, r"\{\s*\{")
                self.assertNotRegex(block, r"(^|\})\s*\{")  # rule with no selector


class CssTokenRules(unittest.TestCase):
    def test_every_var_reference_is_a_defined_token(self):
        tokens = defined_tokens()
        for name, block in (("pages.css carousel", carousel_block()), ("graph.css job tabs", job_tab_block())):
            used = set(re.findall(r"var\((--[\w-]+)", block))
            with self.subTest(block=name):
                self.assertTrue(used, "block uses no tokens at all")
                self.assertEqual(sorted(used - tokens), [], "undefined design tokens")

    def test_no_raw_hex_colours_in_new_blocks(self):
        for name, block in (("pages.css carousel", carousel_block()), ("graph.css job tabs", job_tab_block())):
            with self.subTest(block=name):
                self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", block), [])

    def test_neon_red_token_exists_and_marks_failed_tab(self):
        tokens = defined_tokens()
        for t in ("--neon-red", "--neon-red-rgb", "--neon-red-bg", "--glow-red"):
            self.assertIn(t, tokens)
        failed = re.search(r'\.job-tab\[data-s="fail"\]\.sel[^{]*\{([^}]*)\}', carousel_block())
        self.assertIsNotNone(failed)
        self.assertIn("var(--neon-red)", failed.group(1))

    def test_font_sizes_hold_the_11px_floor(self):
        """DESIGN.md: 11px is the text floor (every other .tmono monogram is 11px)."""
        block = carousel_block()
        sizes = re.findall(r"font-size:\s*(\d+)px", block) + re.findall(r"font:\s*\d+\s+(\d+)px", block)
        self.assertTrue(sizes)
        self.assertEqual([s for s in sizes if int(s) < 11], [], "text below the 11px floor")


class CarouselStyles(unittest.TestCase):
    def test_scroll_snap_and_hidden_scrollbar_on_the_track(self):
        block = carousel_block()
        self.assertIn("scroll-snap-type: x mandatory", block)
        self.assertIn("scroll-snap-align: start", block)
        self.assertIn("overflow-x: auto", block)

    def test_status_colours_for_all_four_states(self):
        block = carousel_block()
        for s in ("run", "done", "need", "fail"):
            with self.subTest(status=s):
                self.assertIn('.job-tab[data-s="%s"]' % s, block)
        self.assertRegex(block, r'data-s="run"\][^{]*\.tab-progress > i[^{]*\{[^}]*var\(--grey\)')
        self.assertRegex(block, r'data-s="done"\][^{]*\.tab-progress > i[^{]*\{[^}]*var\(--green\)')
        self.assertRegex(block, r'data-s="need"\][^{]*\.tab-progress > i[^{]*\{[^}]*var\(--ember\)')
        self.assertRegex(block, r'data-s="fail"\][^{]*\.tab-progress > i[^{]*\{[^}]*var\(--neon-red\)')

    def test_progress_bar_animates_width_and_respects_reduced_motion(self):
        block = carousel_block()
        self.assertRegex(block, r"\.tab-progress > i[^{]*\{[^}]*transition:\s*width")
        self.assertRegex(block, r"@media \(prefers-reduced-motion: reduce\)")

    def test_responsive_breakpoints_and_empty_state_style(self):
        block = carousel_block()
        self.assertIn("@media (max-width: 640px)", block)
        self.assertIn(".tab-strip-empty", block)

    def test_selectors_used_by_workflows_js_are_styled(self):
        js = (STATIC / "workflows.js").read_text(encoding="utf-8")
        css = carousel_block() + job_tab_block()
        for cls in ("job-tab", "tab-all", "tab-close", "tab-progress", "tab-strip-empty", "tab-arrow"):
            with self.subTest(cls=cls):
                self.assertIn(cls, js)
                self.assertIn(cls, css)


if __name__ == "__main__":
    unittest.main()
