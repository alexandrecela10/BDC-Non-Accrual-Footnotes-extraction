"""Footnote definitions for the parsed schedule, using the BDC Footnotes repo's own code.

Primary path (repo code, unchanged):
  parsers.html_parser.find_schedule_sections  -> dated "Consolidated Schedule of Investments" ranges
  parsers.footnote_extractor.extract_footnotes_from_section -> {marker: definition text}

Fallback path, used only when the primary path finds nothing for the parsed date (the repo's
header regex requires the word "Consolidated", so a filer that prints "Schedule of
Investments" alone is missed): the same repo marker rules (_find_all_definition_markers,
_collect_definition_texts) run over the text of the section range found by the table parser.

Tags reuse parsers.non_accrual_tagger.classify_text for non-accrual and default. The other
tags are keyword rules written for the demo. Every tag is a keyword match on the footnote
text, not a confirmed designation.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bs4 import BeautifulSoup  # noqa: E402
import parsers.footnote_extractor as _fx  # noqa: E402
from parsers.html_parser import find_schedule_sections  # noqa: E402
from parsers.footnote_extractor import (  # noqa: E402
    build_text_node_index, extract_footnotes_from_section,
    _find_all_definition_markers, _collect_definition_texts,
)
try:
    from parsers.non_accrual_tagger import classify_text  # noqa: E402
    TAGGER_SOURCE = "parsers/non_accrual_tagger.py"
except ImportError:   # untracked in git: absent on a fresh clone
    from non_accrual_rules import classify_text  # noqa: E402
    TAGGER_SOURCE = "demo/non_accrual_rules.py (copy)"
from vendor_parallax.section import parse_month_date  # noqa: E402

# The repo extractor loads "learned" page-header patterns from output/fixer_memory.json if the
# working directory holds one. That file is gitignored, so the hosted demo never has it.
# Clear the list in memory so a local run and the hosted run behave the same.
_fx._LEARNED_HEADER_PATTERNS = []

TAG_RULES = {
    "pik": re.compile(r"\bPIK\b|payment[\s-]in[\s-]kind", re.I),
    "restricted": re.compile(r"restricted\s+securit|restricted\s+as\s+to\s+resale|not\s+registered\s+under\s+the\s+securities\s+act|"
                             r"exempt\s+from\s+registration|rule\s+144a", re.I),
    "non_qualifying": re.compile(r"non[\s-]?qualifying|qualifying\s+assets|section\s+55\(a\)", re.I),
    # "co-investment made with the Company's affiliates" is not an affiliate investment, so the
    # rule needs the 1940 Act wording ("deemed to control", "affiliated person", "controlled affiliate").
    "affiliate_or_control": re.compile(r"deemed\s+(?:to\s+be\s+)?(?:an?\s+)?[\"“]?(?:control|affiliat)|affiliated\s+person|"
                                       r"(?:non-?)?controlled\s+affiliate|control\s+investment", re.I),
    "level_3": re.compile(r"level\s+3|significant\s+unobservable", re.I),
    "unfunded_or_revolver": re.compile(r"unfunded|delayed[\s-]draw|revolv", re.I),
    "non_income_producing": re.compile(r"non[\s-]?income[\s-]producing", re.I),
    "pledged_collateral": re.compile(r"pledged|as\s+collateral|held\s+(?:by|through|in)\s+.{0,60}(?:spv|subsidiar|financing)", re.I),
}
TAG_NAMES = ["non_accrual"] + list(TAG_RULES)


def tag_footnote(text: str) -> list[str]:
    """Keyword tags for one footnote text. Non-accrual and default use the repo's tagger."""
    tags = ["non_accrual"] if classify_text(text) else []
    tags += [name for name, rx in TAG_RULES.items() if rx.search(text or "")]
    return tags


def strip_page_header(text: str, filer_name: str) -> str:
    """Remove the filer's running page header ("<Filer name> (Unaudited)") that the repo
    extractor can pick up when a footnote spans a page break. The repo fixes this with
    patterns learned in output/fixer_memory.json; the demo uses the EDGAR filer name instead."""
    if not filer_name or not text:
        return text
    rx = re.compile(r"\s*\b" + re.escape(filer_name.strip()) + r"\b\s*(?:and\s+subsidiaries\s*)?(?:\(unaudited\))?", re.I)
    return re.sub(r"\s{2,}", " ", rx.sub(" ", text)).strip()


# When the schedule's closing line ("See accompanying notes...") is missing, the repo extractor
# keeps collecting the last footnote into the next part of the filing. Cut at the first sign of it.
RUNAWAY_RE = re.compile(r"\s(?:see\s+accompanying\s+notes|table\s+of\s+contents|item\s+\d+[a-z]?\.\s|notes?\s+to\s+(?:the\s+)?consolidated\s+financial"
                        r"|note\s+(?:1|a)\b[.\s—–-])", re.I)
MAX_FOOTNOTE_CHARS = 2500


def trim_runaway(text: str) -> tuple[str, bool]:
    """Returns (text, trimmed?)."""
    text = text or ""
    cut = False
    m = RUNAWAY_RE.search(text)
    if m:
        text, cut = text[:m.start()].strip(), True
    if len(text) > MAX_FOOTNOTE_CHARS:
        text, cut = text[:MAX_FOOTNOTE_CHARS].rstrip() + " [truncated]", True
    return text, cut


def _date_of(date_str: str) -> date | None:
    return parse_month_date(date_str or "")


def extract_primary(raw: bytes, as_of: date | None) -> tuple[dict, list[str]]:
    """Repo path. Returns ({marker: text}, [dates the repo found]).

    One demo guard on top of the repo code: a section's end is clipped to the start of the
    next dated section. Without it, a section with no closing line runs into the prior-period
    schedule, and the repo's keep-the-last-definition rule returns the prior period's footnotes."""
    soup = BeautifulSoup(raw, "lxml")
    sections = find_schedule_sections(soup)
    dates = [s[2] for s in sections]
    if not sections:
        return {}, dates
    all_nodes, id_to_pos = build_text_node_index(soup)
    starts = sorted(id_to_pos[id(s[0])] for s in sections if id(s[0]) in id_to_pos)
    chosen = [s for s in sections if as_of and _date_of(s[2]) == as_of] or ([] if as_of else sections[:1])
    footnotes: dict = {}
    for start, end, _ in chosen:
        sp = id_to_pos.get(id(start))
        nxt = next((p for p in starts if sp is not None and p > sp), None)
        ep = id_to_pos.get(id(end)) if end is not None else None
        if nxt is not None and (ep is None or ep >= nxt):
            end = all_nodes[nxt - 1]
        for k, v in extract_footnotes_from_section(start, end, soup, all_nodes, id_to_pos).items():
            footnotes.setdefault(k, v)
    return footnotes, dates


def section_strings(blocks, section_ranges) -> list[str]:
    """Plain strings in document order for a section range: text blocks, then each table cell."""
    out: list[str] = []
    for a, b in section_ranges:
        for j in range(a, b):
            blk = blocks[j]
            if blk.kind == "text":
                if not blk.in_link:
                    out.append(blk.text)
            else:
                for td in blk.table.iter("td", "th"):
                    t = " ".join(td.text_content().split())
                    if t:
                        out.append(t)
    return out


def extract_fallback(strings: list[str]) -> dict:
    """Repo marker rules on plain strings (they only call .strip() on each node)."""
    positions = _find_all_definition_markers(strings)
    return dict(_collect_definition_texts(strings, positions)) if positions else {}
