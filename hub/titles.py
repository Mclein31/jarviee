"""Short task titles and one-line summaries from raw prompts.

A prompt can be several paragraphs of markdown. The dashboard lists a title
(at most 60 characters) and an optional summary (at most 120) instead.
The same rules are mirrored in dashboard.html (`makeTitle`) for old rows.
Standard library only; hooks/report.py imports this file directly.
"""
import re

TITLE_MAX = 60
SUMMARY_MAX = 120

_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_RULE_LINE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|={3,})\s*$")
_LEAD = re.compile(r"^\s*(?:>\s*)*(?:#{1,6}\s+|[-*+•]\s+|\d+[.)]\s+|\[[ xX]\]\s+)*")
_INLINE_BREAK = re.compile(r"\s(?:#{2,6}|-{3,}|\*{3,})\s")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPH = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|approx|incl)\.$", re.I)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_CLAUSE = re.compile(r"\s*(?:[,;:]|\s[—–-])\s")


def _lines(text):
    """Plain-text lines from markdown as (text, was_heading): no fences, rules, list markers, emphasis or backticks."""
    s = _FENCE.sub(" ", str(text or ""))
    out = []
    for line in s.splitlines():
        if _RULE_LINE.match(line):
            out.append(("", False))
            continue
        heading = bool(re.match(r"^\s*#{1,6}\s", line))
        line = _LEAD.sub("", line)
        line = _IMAGE.sub(r"\1", line)
        line = _LINK.sub(r"\1", line)
        for _ in range(2):
            line = _EMPH.sub(r"\2", line)
        line = line.replace("`", "").replace("**", "")
        # a prompt flattened onto one line keeps its '###' / '---' separators inline
        line = _INLINE_BREAK.split(" " + line.strip() + " ", 1)[0].strip()
        out.append((line, heading))
    return out


def clean_markdown(text):
    return "\n".join(t for t, _ in _lines(text)).strip()


def _blocks(text):
    """Non-empty lines, skipping a short generic heading ('### Task') when real text follows it."""
    lines = [(t, h) for t, h in _lines(text) if t]
    if len(lines) > 1 and lines[0][1] and len(lines[0][0].split()) < 3:
        lines = lines[1:]
    return [t for t, _ in lines]


def _first_sentence(s):
    for m in _SENTENCE_END.finditer(s):
        head = s[:m.start() + 1]
        if _ABBREV.search(head):
            continue
        if len(head.strip()) >= 8:
            return head.strip()
    return s.strip()


def _trim(s, limit):
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    paren = cut.rfind(" (")
    if paren >= limit * 0.5 and ")" not in cut[paren:]:
        cut = cut[:paren]
    else:
        space = cut.rfind(" ")
        if space >= limit * 0.5:
            cut = cut[:space]
    return cut.rstrip(" ,;:-–—(") + "…"


def _tidy(s, cap=True):
    s = re.sub(r"\s+", " ", s).strip().strip(" .,;:")
    if s.startswith("(") and s.endswith(")") and s.count("(") == 1:
        s = s[1:-1].strip()
    return s[:1].upper() + s[1:] if s and cap else s


def make_title(text, limit=TITLE_MAX):
    """First sentence or clause of the prompt, markdown removed, cut at a word boundary."""
    blocks = _blocks(text)
    title = _tidy(_first_sentence(blocks[0])) if blocks else ""
    if len(title) > limit:
        parts = [p for p in _CLAUSE.split(title) if p.strip()]
        if parts and 16 <= len(parts[0]) <= limit:
            title = _tidy(parts[0])
    return _trim(title, limit)


def make_summary(text, title=None, limit=SUMMARY_MAX):
    """One muted line after the title, from the same paragraph or the next; '' when there is none."""
    blocks = _blocks(text)
    title = (title if title is not None else make_title(text)).rstrip("…").strip()
    if not blocks or not title:
        return ""
    first, key = blocks[0], title.lower()
    i = first.lower().find(key)
    rest = first[i + len(key):] if i >= 0 else ""
    rest = rest.lstrip(" .,;:—–-").strip()
    if len(rest) < 12 and len(blocks) > 1:
        rest = blocks[1]
    if len(rest) < 12:
        return ""
    return _trim(_tidy(rest, cap=False), limit)
