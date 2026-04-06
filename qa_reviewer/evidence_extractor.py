"""
Evidence Extractor — pulls focused text windows from parsed HTML for LLM review.

The LLM can't read 10MB HTML files. Instead, we extract small, targeted
"evidence windows" that give the LLM just enough context to verify:

1. Section boundary evidence: ~500 chars around start/end of the identified section
2. Footnote area evidence: raw text of the footnote definition area (~3000 chars)
3. Per-footnote evidence: source text for each extracted footnote

These windows are what the LLM reviewer reads to make its judgment.
"""

import re
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger(__name__)


@dataclass
class SectionEvidence:
    """
    Evidence package for one schedule-of-investments date sub-section.
    Contains all the text windows the LLM needs to verify the extraction.
    """
    # Path to the source HTML file (for navigating to source)
    html_path: str = ""
    # Context around the section start (first ~500 chars of the section)
    section_start_text: str = ""
    # Context around the section end (last ~500 chars of the section)
    section_end_text: str = ""
    # Raw text of footnote DEFINITIONS only (no table data)
    footnote_area_text: str = ""
    # The full list of extracted marker IDs for completeness check
    extracted_markers: List[str] = field(default_factory=list)
    # Per-footnote: marker -> extracted definition text
    extracted_footnotes: Dict[str, str] = field(default_factory=dict)


def extract_evidence(
    start_elem,
    end_elem,
    soup: BeautifulSoup,
    extracted_footnotes: Dict[str, str],
    html_path: str = "",
) -> SectionEvidence:
    """
    Build an evidence package from the parsed HTML and extraction results.

    We collect text nodes between start_elem and end_elem, then slice
    them into focused windows for the LLM to review.

    Args:
        start_elem: NavigableString marking the section start
        end_elem: NavigableString marking the section end
        soup: The full parsed HTML document
        extracted_footnotes: The footnotes our deterministic pipeline extracted
        html_path: Path to the source HTML file for traceability

    Returns:
        SectionEvidence with all text windows populated
    """
    evidence = SectionEvidence()
    evidence.html_path = html_path
    evidence.extracted_markers = list(extracted_footnotes.keys())
    evidence.extracted_footnotes = dict(extracted_footnotes)

    # Collect all text nodes in the section
    text_nodes = _collect_section_text(start_elem, end_elem)

    if not text_nodes:
        logger.warning("No text nodes found for evidence extraction")
        return evidence

    # Build the full section text (stripped, one piece per node)
    all_texts = [n.strip() for n in text_nodes if n.strip()]

    # 1) Section start evidence — first ~500 chars
    evidence.section_start_text = _build_window(all_texts, from_start=True, max_chars=500)

    # 2) Section end evidence — last ~500 chars
    evidence.section_end_text = _build_window(all_texts, from_start=False, max_chars=500)

    # 3) Footnote area — ONLY the footnote definitions, no table data.
    # We scan backward to find the first footnote marker, then take
    # everything from there to the end of the section.
    evidence.footnote_area_text = _build_footnote_area(all_texts)

    return evidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_section_text(start_elem, end_elem) -> List[NavigableString]:
    """Walk forward from start_elem to end_elem collecting text nodes."""
    nodes = [start_elem]
    for node in start_elem.find_all_next(string=True):
        nodes.append(node)
        if end_elem is not None and node is end_elem:
            break
    return nodes


def _build_footnote_area(texts: List[str]) -> str:
    """
    Extract ONLY the footnote definition area — no investment table data.
    
    Strategy: scan backward from the end of the section to find the
    earliest footnote definition marker. Footnote definitions are
    always at the bottom of each page, after the investment data rows.
    
    We look for patterns like:
    - "(1) Some definition text..." (inline definition)
    - "(1)" as standalone followed by definition text
    - "(A) Some definition text..."
    - "* Refer to..." or "** Refer to..."
    
    Once we find the earliest definition marker cluster, we include
    everything from there to the end.
    """
    if not texts:
        return ""
    
    # Regex to detect footnote definition markers
    marker_re = re.compile(
        r"^\s*\((\d+|[A-Za-z])\)\s*$"      # standalone: "(1)"
        r"|^\s*\((\d+|[A-Za-z])\)\s+.{8,}"  # inline: "(1) Some text..."
        r"|^\s*\*{1,3}\s+.{8,}"             # star: "* Refer to..."
    )
    
    # Scan forward to find the FIRST footnote definition marker.
    # We look for clusters: a marker followed by substantial text.
    first_def_idx = None
    for i, t in enumerate(texts):
        if marker_re.match(t):
            first_def_idx = i
            break
        # Also check: standalone marker where next text is a definition
        standalone = re.match(r"^\s*\((\d+|[A-Za-z])\)\s*$", t)
        if standalone and i + 1 < len(texts):
            next_t = texts[i + 1]
            # Next text should be substantial (not a number or short data)
            if len(next_t) >= 15 and not re.match(r'^[\d,\.\-\+/\s%$SLNA]+$', next_t):
                first_def_idx = i
                break
    
    if first_def_idx is None:
        # Fallback: take last 15000 chars
        return _build_window(texts, from_start=False, max_chars=15000)
    
    # Take everything from the first definition to the end
    footnote_texts = texts[first_def_idx:]
    result = " | ".join(footnote_texts)
    
    # Cap at 30000 chars to stay within reasonable LLM input
    if len(result) > 30000:
        result = result[:30000]
    
    return result


def _build_window(texts: List[str], from_start: bool, max_chars: int) -> str:
    """
    Build a text window of approximately max_chars from either the start
    or end of the text list.

    Joins text pieces with ' | ' as a delimiter so the LLM can see
    element boundaries clearly.
    """
    if not texts:
        return ""

    if from_start:
        # Take from the beginning
        window_parts = []
        total = 0
        for t in texts:
            if total + len(t) > max_chars:
                break
            window_parts.append(t)
            total += len(t)
        return " | ".join(window_parts)
    else:
        # Take from the end
        window_parts = []
        total = 0
        for t in reversed(texts):
            if total + len(t) > max_chars:
                break
            window_parts.insert(0, t)
            total += len(t)
        return " | ".join(window_parts)
