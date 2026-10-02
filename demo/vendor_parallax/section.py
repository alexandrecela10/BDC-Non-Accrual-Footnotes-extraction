# Copied from ~/Documents/Parallax/src/parallax/soi/section.py (Parallax repo, commit 0ab5eaa),
# by the same author, for the BDC Footnotes demo. Logic unchanged unless a line says
# "DEMO CHANGE". Credit: Parallax M2 Schedule of Investments parser.
"""Find the Consolidated Schedule of Investments sections in one filing (M2, step 1).

Idea reused from ~/Documents/BDC Footnotes/parsers/html_parser.py (find_schedule_sections,
_extract_date, _find_section_end): the schedule header repeats on every page, the "as of"
date sits in the header or in the next few text nodes, and a 10-K (and, here, every 10-Q
too) carries two dated sections. We walk the document once with lxml, in order, and emit
blocks: text outside tables, and tables. Every table is assigned the date of the nearest
preceding schedule header (FM-005: the caller keeps only the date equal to period_end).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from lxml import html as LH

SCHEDULE_HEADER_RE = re.compile(r"(consolidated\s+)?schedules?\s+of\s+investments?", re.I)
NOTES_TO_RE = re.compile(r"^\s*notes?\s+to\s+(the\s+)?(consolidated\s+)?(schedules?|financial)", re.I)
END_MARKER_RE = re.compile(
    r"(see\s+(accompanying\s+)?notes\s+to|the\s+accompanying\s+notes\s+are\s+an\s+integral\s+part)", re.I)
HARD_END_RE = re.compile(r"^\s*notes\s+to\s+(the\s+)?consolidated\s+financial\s+statements", re.I)
MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december")
DATE_RE = re.compile(rf"(?:as\s+of\s+)?\b({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I)
UNITS_RE = re.compile(r"in\s+(thousands|millions)", re.I)
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS.split("|"))}
AUDIT_WORDS = ("we have audited", "audited the accompanying", "including the consolidated schedule",
               "our audits", "in our opinion")


def parse_month_date(text: str) -> date | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTH_NUM[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None


@dataclass
class Block:
    kind: str                 # "text" | "table"
    text: str = ""            # for text blocks
    table: object = None      # lxml element for table blocks
    in_link: bool = False     # text inside <a> (table of contents)
    index: int = -1           # table index in document order (tables only)


@dataclass
class Section:
    as_of: date | None
    header_text: str
    start: int                # block index of the first header
    end: int                  # block index (exclusive) where the first range stops
    units_multiplier: float | None = None
    table_blocks: list[int] = field(default_factory=list)
    ranges: list[tuple[int, int]] = field(default_factory=list)   # all (start, end) ranges with this date

    def text_block_indices(self) -> list[int]:
        return [j for a, b in self.ranges for j in range(a, b)]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def iter_blocks(doc) -> list[Block]:
    """Document-order blocks: text outside tables (element .text and .tail) and tables."""
    blocks: list[Block] = []
    ti = 0
    # Precompute table membership without repeated xpath calls: walk the tree recursively.
    def walk(el, in_table: bool, in_link: bool):
        nonlocal ti
        tag = el.tag if isinstance(el.tag, str) else None
        if tag in ("script", "style"):
            return
        if tag == "table" and not in_table:
            blocks.append(Block(kind="table", table=el, index=ti))
            ti += 1
            # tail of a table still belongs to the outer flow
            t = _clean(el.tail)
            if t:
                blocks.append(Block(kind="text", text=t, in_link=in_link))
            return
        link = in_link or tag == "a"
        if not in_table:
            t = _clean(el.text)
            if t:
                blocks.append(Block(kind="text", text=t, in_link=link))
        for child in el:
            walk(child, in_table, link)
        if not in_table:
            t = _clean(el.tail)
            if t:
                blocks.append(Block(kind="text", text=t, in_link=in_link))
    walk(doc, False, False)
    return blocks


def header_text_at(blocks: list[Block], i: int) -> str | None:
    """The header text at block i, joining split text runs ("Consolidated Schedule of Inve" +
    "stments as of September 30, 2024", TSLX 2024) across up to 4 consecutive text blocks."""
    b = blocks[i]
    if b.kind != "text" or b.in_link:
        return None
    if is_schedule_header(b):
        return b.text
    if len(b.text) > 60 or "chedule" not in b.text.lower() and "onsolidated" not in b.text.lower():
        return None
    parts = [b.text]
    for j in range(i + 1, min(len(blocks), i + 4)):
        nb = blocks[j]
        if nb.kind != "text" or nb.in_link:
            break
        parts.append(nb.text)
        for joined in ("".join(parts), " ".join(parts)):
            if len(joined) <= 140 and is_schedule_header(Block(kind="text", text=joined)):
                return joined
    return None


def is_schedule_header(b: Block) -> bool:
    if b.kind != "text" or b.in_link:
        return False
    t = b.text
    if len(t) > 140 or not SCHEDULE_HEADER_RE.search(t):
        return False
    if NOTES_TO_RE.match(t):
        return False
    if re.search(r"advances\s+to\s+affiliates|changes\s+in", t, re.I):
        return False   # Schedule 12-14 (affiliates) and "Schedules of Changes" are not the SOI
    low = t.lower()
    if any(w in low for w in AUDIT_WORDS):
        return False
    # Prose that mentions the schedule ("...the consolidated schedule of investments as of...")
    # is longer than a heading and starts lower-case or mid-sentence.
    if len(t) > 90 and not low.startswith(("consolidated", "schedule", "unaudited")):
        return False
    return True


def header_date(blocks: list[Block], i: int, lookahead: int = 8, text: str | None = None) -> date | None:
    """Date in the header text, else in the next few text blocks (before the next table).
    A "(continued)" header without its own date inherits (returns None here)."""
    text = text if text is not None else blocks[i].text
    d = parse_month_date(text)
    if d:
        return d
    seen = 0
    for j in range(i + 1, min(len(blocks), i + 1 + 40)):
        b = blocks[j]
        if b.kind == "table":
            # PSEC puts the page header inside the table: look at its first rows
            txt = _clean(b.table.text_content())[:400]
            return parse_month_date(txt)
        if not b.text:
            continue
        d = parse_month_date(b.text)
        if d and len(b.text) < 80:
            return d
        seen += 1
        if seen >= lookahead:
            break
    return None


def nearby_units(blocks: list[Block], i: int, back: int = 6, fwd: int = 6) -> float | None:
    """Units multiplier from the text around a table (or a header): thousands/millions."""
    lo, hi = max(0, i - back), min(len(blocks), i + fwd + 1)
    for j in list(range(i, lo - 1, -1)) + list(range(i + 1, hi)):
        b = blocks[j]
        txt = b.text if b.kind == "text" else _clean(b.table.text_content())[:300]
        m = UNITS_RE.search(txt)
        if m:
            return 1e3 if m.group(1).lower() == "thousands" else 1e6
    return None


def find_sections(blocks: list[Block]) -> list[Section]:
    """Group schedule headers by date. A section runs from its first header to the next
    header with a different date, or a hard end (Notes to Consolidated Financial Statements).
    Tables inside the range are candidates; the caller filters by header signature."""
    headers = []
    for i in range(len(blocks)):
        ht = header_text_at(blocks, i)
        if ht is not None:
            headers.append((i, ht, header_date(blocks, i, text=ht)))
    if not headers:
        return []
    # Headers without a date inherit the previous header's date (continuation pages).
    filled = []
    last = None
    for i, ht, d in headers:
        if d is None:
            d = last
        last = d
        filled.append((i, ht, d))
    # Contiguous runs of the same date form a range; ranges of the same date merge into one
    # section (PSEC's endnote pages come after the other date's pages).
    runs: list[tuple[date | None, str, int]] = []
    for i, ht, d in filled:
        if runs and runs[-1][0] == d:
            continue
        runs.append((d, ht, i))
    ranges: list[tuple[date | None, str, int, int]] = []
    for k, (d, ht, start) in enumerate(runs):
        nxt = runs[k + 1][2] if k + 1 < len(runs) else len(blocks)
        end = nxt
        for j in range(start + 1, nxt):
            b = blocks[j]
            if b.kind == "text" and not b.in_link and HARD_END_RE.match(b.text) and len(b.text) < 120:
                end = j
                break
        ranges.append((d, ht, start, end))
    sections: list[Section] = []
    by_date: dict = {}
    for d, ht, start, end in ranges:
        key = d.isoformat() if d else f"none-{start}"
        if key in by_date:
            by_date[key].ranges.append((start, end))
            continue
        s = Section(as_of=d, header_text=ht, start=start, end=end, ranges=[(start, end)])
        by_date[key] = s
        sections.append(s)
    for s in sections:
        s.table_blocks = [j for a, b in s.ranges for j in range(a, b) if blocks[j].kind == "table"]
        s.units_multiplier = nearby_units(blocks, s.start, back=0, fwd=8)
    return sections


def load_blocks(path) -> list[Block]:
    with open(path, "rb") as fh:
        doc = LH.fromstring(fh.read())
    return iter_blocks(doc)
