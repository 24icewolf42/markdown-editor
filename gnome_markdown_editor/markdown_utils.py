"""Pure-text Markdown helpers (no GTK), unit-tested in tests/."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
EXPLICIT_ID_RE = re.compile(r"\s*\{[^}]*#([\w-]+)[^}]*\}\s*$")
LIST_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<quote>(?:>[ \t]?)*)"
    r"(?P<marker>[-*+]|\d{1,9}[.)])"
    r"(?P<space>[ \t]+)"
    r"(?P<task>\[[ xX]\][ \t]+)?"
    r"(?P<rest>.*)$"
)
QUOTE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<quote>(?:>[ \t]?)+)(?P<rest>.*)$")
FRONT_MATTER_RE = re.compile(r"\A(---|\+\+\+)[ \t]*\r?\n.*?\r?\n\1[ \t]*(?:\r?\n|\Z)", re.S)


# --------------------------------------------------------------------------
# Slugs (GitHub / Hugo "github" heading ids, matching github-slugger)


def _strip_inline_markup(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)  # reference links
    text = re.sub(r"<[^>]+>", "", text)  # inline html
    return text


CODE_SPAN_RE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")


def plain_heading_text(text: str) -> str:
    """Heading text as rendered (markup removed), used for slugs and outline."""
    text = EXPLICIT_ID_RE.sub("", text)
    # Code spans are literal: park them while markup is stripped.
    spans: list[str] = []

    def park(m: re.Match) -> str:
        spans.append(m.group(2).strip() if m.group(2).strip() else m.group(2))
        return f"\x00{len(spans) - 1}\x00"

    text = CODE_SPAN_RE.sub(park, text)
    text = _strip_inline_markup(text)
    text = re.sub(r"(\*\*|\*|~~|==)(?=\S)(.+?)(?<=\S)\1", r"\2", text)
    # Underscore emphasis only at word boundaries (snake_case stays intact).
    text = re.sub(r"(?<![\w])(__|_)(?=\S)(.+?)(?<=\S)\1(?![\w])", r"\2", text)
    text = re.sub(r"\\(.)", r"\1", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text.strip()


def _slug_char_ok(ch: str) -> bool:
    if ch in " -_":
        return True
    cat = unicodedata.category(ch)
    return cat[0] in "LNM"


def slugify(text: str) -> str:
    """github-slugger's algorithm: lower-case, drop punctuation, spaces to '-'."""
    text = plain_heading_text(text).replace("`", "")
    out = "".join(ch for ch in text.lower() if _slug_char_ok(ch))
    return out.replace(" ", "-")


class Slugger:
    def __init__(self) -> None:
        self.seen: dict[str, int] = {}

    def slug(self, text: str, *, raw: bool = False) -> str:
        base = text if raw else slugify(text)
        slug = base
        if slug in self.seen:
            n = self.seen[base]
            while True:
                n += 1
                slug = f"{base}-{n}"
                if slug not in self.seen:
                    break
            self.seen[base] = n
        self.seen.setdefault(slug, 0)
        return slug


# --------------------------------------------------------------------------
# Structure


@dataclass
class Heading:
    level: int
    title: str
    line: int  # 0-based
    anchor: str


def front_matter_lines(text: str) -> int:
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return 0
    return m.group(0).count("\n") + (0 if m.group(0).endswith("\n") else 1)


def code_line_mask(lines: list[str]) -> list[bool]:
    """True for lines inside a fenced code block (fence lines included)."""
    mask = [False] * len(lines)
    fence: str | None = None
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                mask[i] = True
        else:
            mask[i] = True
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = None
    return mask


def parse_headings(text: str) -> list[Heading]:
    lines = text.split("\n")
    skip = front_matter_lines(text)
    in_code = code_line_mask(lines)
    slugger = Slugger()
    headings: list[Heading] = []
    prev_para = False
    for i, line in enumerate(lines):
        if i < skip or in_code[i]:
            prev_para = False
            continue
        m = ATX_RE.match(line)
        if m:
            raw = m.group(2) or ""
            idm = EXPLICIT_ID_RE.search(raw)
            anchor = slugger.slug(idm.group(1), raw=True) if idm else slugger.slug(raw)
            headings.append(Heading(len(m.group(1)), plain_heading_text(raw), i, anchor))
            prev_para = False
            continue
        sm = SETEXT_RE.match(line)
        if sm and prev_para and i > 0 and lines[i - 1].strip():
            prev = lines[i - 1]
            # A '---' after a list item or table row is not a heading underline.
            if not LIST_RE.match(prev) and not prev.lstrip().startswith(("|", ">")):
                level = 1 if sm.group(1)[0] == "=" else 2
                headings.append(Heading(level, plain_heading_text(prev.strip()), i - 1, slugger.slug(prev.strip())))
                prev_para = False
                continue
        stripped = line.strip()
        prev_para = bool(stripped) and not stripped.startswith(("<", "|"))
    return headings


def table_of_contents(headings: list[Heading], min_level: int = 2, max_level: int = 3) -> str:
    items = [h for h in headings if min_level <= h.level <= max_level]
    if not items:
        return ""
    base = min(h.level for h in items)
    return "\n".join(f"{'  ' * (h.level - base)}- [{h.title}](#{h.anchor})" for h in items) + "\n"


# --------------------------------------------------------------------------
# Lists


def list_continuation(line: str) -> tuple[str | None, bool]:
    """What pressing Enter at the end of ``line`` should insert.

    Returns (prefix, end_list). ``prefix`` is the text to put at the start of
    the new line, or None if the line is not a list item or quote. When
    ``end_list`` is True the current item is empty and its marker should be
    removed instead of continuing the list.
    """
    m = LIST_RE.match(line)
    if m:
        marker = m.group("marker")
        if marker[-1] in ".)" and marker[:-1].isdigit():
            marker = f"{int(marker[:-1]) + 1}{marker[-1]}"
        task = "[ ] " if m.group("task") else ""
        prefix = f"{m.group('indent')}{m.group('quote')}{marker}{m.group('space')}{task}"
        empty = not m.group("rest").strip()
        return prefix, empty
    q = QUOTE_RE.match(line)
    if q:
        return f"{q.group('indent')}{q.group('quote')}", not q.group("rest").strip()
    return None, False


def toggle_task(line: str) -> str | None:
    m = re.match(r"^([ \t]*(?:>[ \t]?)*(?:[-*+]|\d{1,9}[.)])[ \t]+\[)([ xX])(\].*)$", line)
    if not m:
        return None
    return m.group(1) + (" " if m.group(2) in "xX" else "x") + m.group(3)


# --------------------------------------------------------------------------
# Tables


def _display_width(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return w


def split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells: list[str] = []
    cur = []
    in_code = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            cur.append(s[i : i + 2])
            i += 2
            continue
        if ch == "`":
            j = i
            while j < len(s) and s[j] == "`":
                j += 1
            run = j - i
            if in_code == 0:
                in_code = run
            elif in_code == run:
                in_code = 0
            cur.append(s[i:j])
            i = j
            continue
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


DELIM_CELL_RE = re.compile(r"^:?-+:?$")


def is_table_line(line: str) -> bool:
    return "|" in line and bool(line.strip())


def format_table(lines: list[str]) -> list[str] | None:
    """Align the pipes of a GFM table. Returns None if it is not a table."""
    if len(lines) < 2:
        return None
    rows = [split_table_row(l) for l in lines]
    if not all(DELIM_CELL_RE.match(c.replace(" ", "")) for c in rows[1]):
        return None
    ncols = max(len(r) for r in rows)
    aligns = []
    for c in rows[1] + [""] * (ncols - len(rows[1])):
        c = c.replace(" ", "")
        if c.startswith(":") and c.endswith(":") and len(c) > 1:
            aligns.append("c")
        elif c.endswith(":"):
            aligns.append("r")
        elif c.startswith(":"):
            aligns.append("l:")
        else:
            aligns.append("l")
    for r in rows:
        r.extend([""] * (ncols - len(r)))
    widths = [3] * ncols
    for ri, r in enumerate(rows):
        if ri == 1:
            continue
        for ci, c in enumerate(r):
            widths[ci] = max(widths[ci], _display_width(c))
    out = []
    for ri, r in enumerate(rows):
        cells = []
        for ci, c in enumerate(r):
            w = widths[ci]
            if ri == 1:
                a = aligns[ci]
                if a == "c":
                    cells.append(":" + "-" * (w - 2) + ":")
                elif a == "r":
                    cells.append("-" * (w - 1) + ":")
                elif a == "l:":
                    cells.append(":" + "-" * (w - 1))
                else:
                    cells.append("-" * w)
            else:
                pad = w - _display_width(c)
                a = aligns[ci]
                if a == "r":
                    cells.append(" " * pad + c)
                elif a == "c":
                    cells.append(" " * (pad // 2) + c + " " * (pad - pad // 2))
                else:
                    cells.append(c + " " * pad)
        out.append("| " + " | ".join(cells) + " |")
    return out


# --------------------------------------------------------------------------
# Statistics


WORD_RE = re.compile(r"\w[\w’'-]*")


def word_count(text: str) -> int:
    """Words of prose: runs starting with a letter or digit, so Markdown
    syntax (#, |, ---, ```) does not count."""
    m = FRONT_MATTER_RE.match(text)
    return sum(1 for _ in WORD_RE.finditer(text, m.end() if m else 0))
