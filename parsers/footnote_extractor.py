"""
Footnote extractor for SEC filing schedule-of-investments sections.

Responsibilities:
1. Within a bounded section (start_elem → end_elem), scan ALL text nodes
   to find footnote definitions (markers followed by definition text)
2. Distinguish definition markers from inline references in the data table
3. Collect text for each footnote across multiple pages within the section
4. Return an ordered dict of marker → definition text

Key insight: Footnote definitions can be spread across MULTIPLE pages
within one schedule section. Each page may have its own cluster of
footnotes at the bottom. We must scan the entire section, not just
look for one "block."

How we tell definitions from inline references:
- Inline ref: "(1)" appears as part of data like "Company(1)(25)" — no
  substantial text follows the marker itself
- Definition: "(1)" is standalone, followed by >20 chars of explanatory text
"""

import os
import re
import json
import logging
from typing import Dict, List, Optional, Tuple
from collections import OrderedDict
from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

# Path to fixer memory — contains patterns learned from previous runs.
# The fixer agent writes page header patterns here; the deterministic
# extractor reads them on the next run so the same contamination
# is prevented at source (no need for the fixer to fix it again).
FIXER_MEMORY_FILE = os.path.join("output", "fixer_memory.json")


def _load_learned_header_patterns() -> List[str]:
    """
    Load page header patterns that the fixer agent learned from previous runs.
    
    These are company names and headers like "Owl Rock Capital Corporation"
    that leaked into footnote text as contamination. The fixer stripped them
    and saved the pattern. Now the deterministic extractor can skip them
    automatically — closing the feedback loop.
    
    Returns an empty list if no memory file exists yet.
    """
    if not os.path.exists(FIXER_MEMORY_FILE):
        return []
    try:
        with open(FIXER_MEMORY_FILE, "r") as f:
            memory = json.load(f)
        patterns = memory.get("page_header_patterns", [])
        if patterns:
            logger.debug(f"Loaded {len(patterns)} learned header patterns from fixer memory")
        return patterns
    except (json.JSONDecodeError, IOError):
        return []


# Load learned patterns once at module import time
_LEARNED_HEADER_PATTERNS = _load_learned_header_patterns()

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Standalone marker: "(1)", "(A)", "(a)" as entire element content
MARKER_STANDALONE_RE = re.compile(r"^\s*\((\d+|[A-Za-z])\)\s*$")

# Inline marker: "(1) Some definition text..."
MARKER_INLINE_RE = re.compile(r"^\s*\((\d+|[A-Za-z])\)\s+(.+)", re.DOTALL)

# Star/asterisk footnotes: "* Refer to...", "** Refer to..."
STAR_MARKER_RE = re.compile(r"^\s*(\*{1,3})\s+(.*)", re.DOTALL)

# Frequency markers that look like footnotes but aren't:
# (M) = monthly, (Q) = quarterly, (S) = semi-annual, (Y) = yearly
# These appear in investment data columns near rate/payment info
FREQUENCY_LETTERS = frozenset({'M', 'Q', 'S', 'Y'})

# Page numbers like "F-4", "F-66" — used to skip page breaks between
# footnote clusters, NOT to stop extraction
PAGE_NUMBER_RE = re.compile(r"^[F\d]+-?\d+$")

# Patterns that indicate we've entered a DIFFERENT section entirely
SECTION_BREAK_RE = re.compile(
    r"^(Consolidated\s+Statement[s]?\s+of\s+"
    r"|Notes\s+to\s+Consolidated\s+Financial\s+Statements"
    r"|Consolidated\s+Schedule[s]?\s+of\s+Changes)",
    re.IGNORECASE,
)

# End-of-section markers (only used as hard stops)
END_MARKER_RE = re.compile(
    r"(See\s+accompanying\s+notes\s+to\s+the\s+consolidated|"
    r"The\s+accompanying\s+notes\s+are\s+an\s+integral\s+part)",
    re.IGNORECASE,
)

# Schedule header (used to detect page repeats within the section)
SCHEDULE_HEADER_RE = re.compile(
    r"Consolidated\s+Schedule[s]?\s+of\s+Investments?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _is_frequency_marker(marker: str, text_nodes: list = None, idx: int = None, 
                         has_definition: bool = False) -> bool:
    """
    Check if a single-letter marker is actually a frequency indicator.
    
    (M), (Q), (S), (Y) are used in investment tables to mean
    monthly/quarterly/semi-annual/yearly — NOT footnote markers.
    
    Strategy:
    1. If the marker is followed by substantial definition text (>50 chars),
       it's a REAL footnote, not a frequency indicator.
    2. Only filter out (M)/(Q)/(S)/(Y) if they appear in table context
       without definition text.
    
    Args:
        marker: The marker letter (e.g., "M", "Q")
        text_nodes: List of text nodes for context
        idx: Index of the marker in text_nodes
        has_definition: True if this marker is followed by substantial text (>50 chars)
    """
    if marker.upper() not in FREQUENCY_LETTERS:
        return False
    
    # If this marker has a substantial definition (>50 chars), it's a real footnote
    if has_definition:
        return False
    
    # If we have context, check surrounding nodes for frequency keywords
    if text_nodes is not None and idx is not None:
        # Look at nearby text (±3 nodes) for frequency-related words
        nearby = ""
        for j in range(max(0, idx - 3), min(len(text_nodes), idx + 4)):
            nearby += " " + text_nodes[j].strip()
        nearby_lower = nearby.lower()
        freq_words = ['monthly', 'quarterly', 'semi-annual', 'semi annual',
                      'yearly', 'annually', 'sofr', 'libor', 'pik', 'cash',
                      'first lien', 'second lien', 'revolver', 'term loan']
        if any(w in nearby_lower for w in freq_words):
            return True
    
    return False


def _is_footnote_marker(text: str) -> Optional[str]:
    """
    Check if text is a standalone footnote marker like "(1)" or "(A)".
    Returns the marker value (e.g., "1", "A") or None.
    """
    match = MARKER_STANDALONE_RE.match(text)
    return match.group(1) if match else None


def _is_inline_footnote(text: str) -> Optional[Tuple[str, str]]:
    """
    Check if text starts with a marker followed by definition text.
    Example: "(1) Certain portfolio company investments..."
    Returns (marker, remaining_text) or None.
    """
    match = MARKER_INLINE_RE.match(text)
    if match:
        return match.group(1), match.group(2).strip()
    return None


def _get_definition_text_after_marker(text_nodes: list, marker_idx: int) -> Optional[str]:
    """
    Look at the next few text nodes after a standalone marker to see
    if they contain definition text (>20 chars, not another marker,
    not a page number, not a schedule header repeat).
    
    This is how we distinguish:
    - Definition marker (1) followed by "Certain portfolio company..." → YES
    - Data reference (1) followed by "First lien" or numbers → NO
    
    Returns the first substantial text found, or None.
    """
    for j in range(marker_idx + 1, min(marker_idx + 5, len(text_nodes))):
        next_text = text_nodes[j].strip()
        if not next_text:
            continue
        
        # Skip if it's another marker
        if _is_footnote_marker(next_text):
            return None
        
        # Skip page numbers
        if PAGE_NUMBER_RE.match(next_text):
            continue
        
        # Skip schedule header repeats
        if SCHEDULE_HEADER_RE.search(next_text):
            continue
        
        # Reject pure numeric/financial data that looks like table values
        # e.g., "42,404", "12/2029", "S+ 4.25", "N/A"
        if re.match(r'^[\d,\.\-\+/\s%$SLNA]+$', next_text):
            return None
        
        # If text is >=8 chars and not pure data, it's a definition.
        # We use 8 to catch short ones like "Reserved." (9 chars)
        if len(next_text) >= 8:
            return next_text
        
        # Short text that's not a marker — likely data, not a definition
        return None
    
    return None


def _is_skippable_text(text: str) -> bool:
    """
    Check if text is a page number, repeated header, date, or other
    non-definition text that appears between footnote clusters.
    
    We skip these when collecting definition text for a footnote,
    so page breaks don't fragment the definitions.
    
    Also checks patterns learned by the fixer agent from previous runs.
    This closes the feedback loop: fixer finds contamination → saves
    the pattern → next run, the extractor skips it automatically.
    """
    if PAGE_NUMBER_RE.match(text):
        return True
    if SCHEDULE_HEADER_RE.search(text):
        return True
    # "As of December 31, 2025" repeated on page headers — but only
    # if it's short (<60 chars). Longer "As of..." text is likely a
    # footnote definition (e.g., "As of December 31, 2020, the net
    # estimated unrealized loss for U.S. federal income tax purposes...")
    if re.match(r"^As\s+of\s+", text, re.IGNORECASE) and len(text) < 60:
        return True
    # "(Amounts in thousands...)" repeated on page headers
    if re.match(r"^\(Amounts\s+in\s+", text, re.IGNORECASE):
        return True
    # Auto-promoted patterns: company names and headers that the fixer
    # agent previously had to strip from contaminated footnotes.
    # If the text matches a learned pattern exactly (or is just that
    # pattern with whitespace), skip it at source.
    stripped = text.strip()
    for pattern in _LEARNED_HEADER_PATTERNS:
        if stripped == pattern or stripped == f"F- {pattern}":
            return True
    return False


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def build_text_node_index(soup: BeautifulSoup):
    """
    Pre-collect ALL text nodes in document order, plus an id→position map.

    This is called **once per filing** so that every later section-slice
    is just two O(1) dict lookups + one list slice — no repeated DOM walks.

    Returns:
        all_nodes:  list[NavigableString] in document order
        id_to_pos:  dict mapping id(node) → index in all_nodes
    """
    all_nodes = list(soup.find_all(string=True))
    id_to_pos = {id(n): i for i, n in enumerate(all_nodes)}
    return all_nodes, id_to_pos


def extract_footnotes_from_section(
    start_elem,
    end_elem,
    soup: BeautifulSoup,
    all_nodes=None,
    id_to_pos=None,
) -> Dict[str, str]:
    """
    Extract all footnote definitions from within a schedule section.
    
    New approach (handles multi-page footnotes):
    1. Collect all text nodes between start and end
    2. Scan ALL nodes — when we find a standalone marker followed by
       substantial text, it's a definition
    3. Collect the definition text until the next definition marker,
       page break, or section end
    4. Return ordered dict: {"1": "definition...", "2": "definition..."}
    
    This handles footnotes spread across multiple pages within one section.

    Performance: if all_nodes / id_to_pos are supplied (from
    build_text_node_index), we slice by position — O(1) lookup.
    Otherwise we fall back to the old DOM-walk method.
    """
    # Collect all text nodes in the section
    text_nodes = _collect_text_nodes(
        start_elem, end_elem, soup, all_nodes, id_to_pos
    )
    
    if not text_nodes:
        logger.warning("No text nodes found in section")
        return OrderedDict()
    
    logger.debug(f"Section has {len(text_nodes)} text nodes")
    
    # Find all definition markers and their positions
    definition_positions = _find_all_definition_markers(text_nodes)
    
    if not definition_positions:
        logger.warning("No footnote definitions found in section")
        return OrderedDict()
    
    # Parse the actual text for each definition
    footnotes = _collect_definition_texts(text_nodes, definition_positions)
    
    logger.info(f"Extracted {len(footnotes)} footnotes")
    return footnotes


def _collect_text_nodes(
    start_elem, end_elem, soup, all_nodes=None, id_to_pos=None
) -> list:
    """
    Collect text nodes between start and end elements in document order.

    Fast path (all_nodes + id_to_pos provided):
        Two O(1) dict lookups to find start/end positions, then a list
        slice. No DOM traversal at all — this is what makes 60 MB files
        processable in seconds instead of minutes.

    Slow fallback (no index provided):
        Walk forward from start_elem via find_all_next. Still correct
        but O(n) on the full DOM size.
    """
    # ── Fast path: slice from pre-built index ──────────────────────
    if all_nodes is not None and id_to_pos is not None:
        start_pos = id_to_pos.get(id(start_elem))
        if start_pos is None:
            # start_elem not in index — fall through to slow path
            pass
        else:
            if end_elem is not None:
                end_pos = id_to_pos.get(id(end_elem))
                if end_pos is not None:
                    return all_nodes[start_pos : end_pos + 1]
            # No end → take everything from start to end of doc
            return all_nodes[start_pos:]

    # ── Slow fallback: walk the DOM ───────────────────────────────
    text_nodes = [start_elem]
    for node in start_elem.find_all_next(string=True):
        # Stop at end element
        if end_elem is not None and node is end_elem:
            text_nodes.append(node)
            break
        # Safety: stop at a different major section
        text = node.strip()
        if text and SECTION_BREAK_RE.search(text) and len(text) < 150:
            if not SCHEDULE_HEADER_RE.search(text):
                break
        text_nodes.append(node)
    return text_nodes


def _find_all_definition_markers(text_nodes: List[NavigableString]) -> List[Tuple[int, str]]:
    """
    Scan all text nodes to find footnote DEFINITION markers.
    
    A definition marker is a standalone "(1)" or "(A)" that is followed
    by substantial text (>20 chars). This distinguishes it from inline
    references in the investment data table.
    
    Also detects inline definitions like "(1) Some definition text..."
    
    Returns list of (index, marker_value) tuples.
    """
    definitions = []
    
    for i, node in enumerate(text_nodes):
        text = node.strip()
        if not text:
            continue
        
        # Check standalone marker followed by definition text
        marker = _is_footnote_marker(text)
        if marker:
            def_text = _get_definition_text_after_marker(text_nodes, i)
            if def_text is not None:
                # Skip frequency markers ONLY if they don't have substantial definition text
                has_def = len(def_text) > 50
                if _is_frequency_marker(marker, text_nodes, i, has_definition=has_def):
                    continue
                definitions.append((i, marker))
            continue
        
        # Check inline definition: "(1) Some definition text..."
        inline = _is_inline_footnote(text)
        if inline and len(inline[1]) >= 8:
            # Skip frequency markers ONLY if they don't have substantial definition text
            has_def = len(inline[1]) > 50
            if _is_frequency_marker(inline[0], text_nodes, i, has_definition=has_def):
                continue
            definitions.append((i, inline[0]))
            continue
        
        # Check star markers: "* Refer to...", "** Refer to..."
        star_match = STAR_MARKER_RE.match(text)
        if star_match and len(star_match.group(2)) >= 8:
            definitions.append((i, star_match.group(1)))
    
    logger.debug(f"Found {len(definitions)} definition markers")
    return definitions


def _collect_definition_texts(
    text_nodes: List[NavigableString],
    definition_positions: List[Tuple[int, str]],
) -> Dict[str, str]:
    """
    For each definition marker, collect all text until the next definition
    marker, end-of-section, or section break.
    
    Handles:
    - Standalone markers: (1) in one element, text in next elements
    - Inline markers: "(1) Some text..." all in one element
    - XBRL tags: ix:footnote, ix:nonfraction treated as text
    - Page breaks between footnote clusters (skipped)
    - Star markers (*, **) treated as special footnotes
    
    If a marker appears multiple times (e.g., on different pages with same
    definition), we keep only the first occurrence.
    """
    footnotes = OrderedDict()
    star_counter = 0
    
    for pos_idx, (node_idx, marker) in enumerate(definition_positions):
        # Determine where to stop collecting text:
        # Either the next definition marker, or end of text_nodes
        if pos_idx + 1 < len(definition_positions):
            next_def_idx = definition_positions[pos_idx + 1][0]
        else:
            next_def_idx = len(text_nodes)
        
        # Collect text parts for this footnote
        text_parts = []
        node_text = text_nodes[node_idx].strip()
        
        # Handle inline definition: "(1) Some text..."
        inline = _is_inline_footnote(node_text)
        if inline:
            text_parts.append(inline[1])
            start_collecting = node_idx + 1
        else:
            # Handle star markers: "* Refer to..." or "** Refer to..."
            star_match = STAR_MARKER_RE.match(node_text)
            if star_match:
                text_parts.append(star_match.group(2))
                start_collecting = node_idx + 1
            else:
                # Standalone marker — text starts in the next node
                start_collecting = node_idx + 1
        
        # Walk forward collecting text until the next definition marker
        for j in range(start_collecting, next_def_idx):
            text = text_nodes[j].strip()
            if not text:
                continue
            
            # Skip page numbers and repeated headers between clusters
            if _is_skippable_text(text):
                continue
            
            # Skip standalone markers (these are inline refs in the data
            # that happen to be between our definition markers)
            if _is_footnote_marker(text):
                continue
            
            # Stop at hard end-of-section markers
            if END_MARKER_RE.search(text):
                break
            
            text_parts.append(text)
        
        # Join and store — keep the LAST occurrence of each marker.
        # Why last? Footnote definitions are at the bottom of schedule pages.
        # Higher-numbered markers like (33) may also appear as inline
        # references in data rows earlier in the section. The real
        # definition is the last one (closest to the end of the section).
        definition_text = _join_text(text_parts)
        if definition_text:
            footnotes[marker] = definition_text
    
    return footnotes


def _join_text(parts: List[str]) -> str:
    """
    Join text parts into a single clean string.
    
    Handles fragments from XBRL tags (e.g., "70" from ix:nonfraction)
    by joining with spaces and cleaning up.
    """
    if not parts:
        return ""
    
    result = " ".join(p.strip() for p in parts if p.strip())
    # Remove double+ spaces
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip()


# ---------------------------------------------------------------------------
# Post-extraction analysis utilities
# ---------------------------------------------------------------------------

def detect_marker_type(footnotes: Dict[str, str]) -> str:
    """
    Classify the footnote marker style used in this section.
    
    Returns:
        'numeric'  — all markers are numbers (1, 2, 3...)
        'alpha'    — all markers are letters (A, B, C...)
        'star'     — only star markers (*, **)
        'mixed'    — combination of types
        ''         — no footnotes
    """
    if not footnotes:
        return ""
    
    markers = list(footnotes.keys())
    has_numeric = any(m.isdigit() for m in markers)
    has_alpha = any(m.isalpha() for m in markers)
    has_star = any(m.startswith("*") for m in markers)
    
    types = sum([has_numeric, has_alpha, has_star])
    if types == 0:
        return ""
    if types > 1:
        return "mixed"
    if has_numeric:
        return "numeric"
    if has_alpha:
        return "alpha"
    return "star"


def check_first_marker_valid(footnotes: Dict[str, str]) -> bool:
    """
    Check if the first extracted footnote starts at the beginning of the
    expected sequence. If the first marker is (22) instead of (1), the
    machine likely missed earlier footnotes.
    
    Valid first markers: '1', 'A', 'a', '*'
    Returns True if valid, False if suspicious.
    """
    if not footnotes:
        return True  # No footnotes = nothing to validate
    
    first_marker = list(footnotes.keys())[0]
    
    # Star markers are always valid as "first" (they're supplementary)
    if first_marker.startswith("*"):
        return True
    
    # Valid starting markers for numeric and alpha styles
    valid_starts = {'1', 'A', 'a'}
    return first_marker in valid_starts
