"""
HTML parser for SEC filing documents.

Responsibilities:
1. Discover all filing HTML files on disk
2. Load and parse HTML with BeautifulSoup
3. Locate the "Consolidated Schedule of Investments" section boundaries
4. Extract the "as of" date for each schedule sub-section

The section boundary logic is the most critical part:
- START: first text node matching the target section name (not in TOC)
- END: "See accompanying notes..." / "The accompanying notes are an integral part..."
        or next different major section header
- (Continued) pages are INSIDE the section, not boundaries
"""

import os
import re
import logging
import warnings
from typing import List, Tuple, Optional
from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
from models import FilingMetadata

# lxml emits this warning on SEC filings that mix HTML and XML (XBRL).
# Harmless — we want lxml for its 3-5x speed advantage on large files.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns used throughout
# ---------------------------------------------------------------------------

# Matches the schedule header text (with variations)
# Examples: "Consolidated Schedule of Investments",
#           "Consolidated Schedules of Investments",
#           "Consolidated Schedule of Investments (Unaudited)"
SCHEDULE_HEADER_RE = re.compile(
    r"Consolidated\s+Schedule[s]?\s+of\s+Investments?",
    re.IGNORECASE,
)

# Matches "(Continued)" suffix — these pages are still part of the section
CONTINUED_RE = re.compile(r"\(Continued\)", re.IGNORECASE)

# Matches the end-of-section markers
END_MARKER_RE = re.compile(
    r"(See\s+accompanying\s+notes\s+to\s+the\s+consolidated|"
    r"The\s+accompanying\s+notes\s+are\s+an\s+integral\s+part)",
    re.IGNORECASE,
)

# Matches a DIFFERENT major section header (signals end of schedule section)
# These are sections that come AFTER the schedule of investments
OTHER_SECTION_RE = re.compile(
    r"^(Consolidated\s+Statement[s]?\s+of\s+"           # e.g. "Consolidated Statements of Operations"
    r"|Notes\s+to\s+Consolidated\s+Financial\s+Statements"  # The big "Notes" section we don't want
    r"|Consolidated\s+Schedule[s]?\s+of\s+Changes)",     # e.g. "Consolidated Schedules of Changes in..."
    re.IGNORECASE,
)

# Matches "As of [date]" or just a date like "December 31, 2025"
AS_OF_DATE_RE = re.compile(
    r"(?:As\s+of\s+)?(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)

# Quarter-end dates only (fiscal quarter ends)
QUARTER_END_DATES = {
    ("march", "31"), ("june", "30"), ("september", "30"), ("december", "31")
}


def _normalize_date(date_str: str) -> str:
    """
    Normalize a date string to a canonical format for grouping.
    
    Handles variations like:
    - "AS OF DECEMBER 31, 2022" -> "december 31 2022"
    - "December 31, 2022" -> "december 31 2022"
    - "DECEMBER 31 2022" -> "december 31 2022"
    
    This ensures headers with the same date but different formatting
    are grouped together as one section.
    """
    if not date_str or date_str == "UNKNOWN":
        return date_str
    
    # Lowercase, remove "as of", remove commas, collapse whitespace
    normalized = date_str.lower()
    normalized = re.sub(r"as\s+of\s+", "", normalized)
    normalized = normalized.replace(",", "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    
    return normalized


def _is_quarter_end(normalized_date: str) -> bool:
    """
    Check if a normalized date string is a quarter-end date.
    
    Quarter-ends are: March 31, June 30, September 30, December 31
    
    We only extract these dates because:
    1. BDCs report quarterly (10-Q) and annually (10-K)
    2. Mid-quarter dates are usually duplicates or errors
    3. Reduces noise from interim schedules
    """
    if not normalized_date or normalized_date == "unknown":
        return False
    
    for month, day in QUARTER_END_DATES:
        if month in normalized_date and day in normalized_date:
            return True
    
    return False


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _scan_cik_tree(root_path: str, filings: List[FilingMetadata], skip_dirs: set):
    """
    Scan a directory that contains CIK folders and collect filings.
    
    Helper used by discover_filings to scan multiple root locations
    (e.g. base_path/ and base_path/sec-edgar-filings/).
    
    Args:
        root_path: Directory containing CIK sub-folders (e.g. "0001655888")
        filings:   List to append discovered FilingMetadata into
        skip_dirs: Set of directory names to ignore (e.g. logs, sec-edgar-filings)
    """
    for cik in sorted(os.listdir(root_path)):
        cik_path = os.path.join(root_path, cik)
        if not os.path.isdir(cik_path) or cik.startswith(".") or cik in skip_dirs:
            continue
        
        for filing_type in sorted(os.listdir(cik_path)):
            type_path = os.path.join(cik_path, filing_type)
            if not os.path.isdir(type_path):
                continue
            
            for accession in sorted(os.listdir(type_path)):
                acc_path = os.path.join(type_path, accession)
                if not os.path.isdir(acc_path):
                    continue
                
                # Look for primary-document.html
                html_file = os.path.join(acc_path, "primary-document.html")
                if os.path.exists(html_file):
                    filings.append(FilingMetadata(
                        cik=cik,
                        filing_type=filing_type,
                        accession=accession,
                        html_path=html_file,
                    ))
                else:
                    logger.warning(f"No primary-document.html in {acc_path}")


def discover_filings(base_path: str) -> List[FilingMetadata]:
    """
    Walk the directory tree and find all filing HTML files.
    
    Scans two locations:
      1. base_path/{CIK}/{FILING_TYPE}/{ACCESSION}/primary-document.html
      2. base_path/sec-edgar-filings/{CIK}/{FILING_TYPE}/{ACCESSION}/primary-document.html
    
    This way the pipeline works whether or not the downloader has
    reorganised files out of the sec-edgar-filings/ staging folder.
    
    Returns a list of FilingMetadata objects, one per filing.
    """
    filings = []
    
    if not os.path.exists(base_path):
        logger.error(f"Base path does not exist: {base_path}")
        return filings
    
    # Directories to skip when scanning the root level
    skip_dirs = {"logs", "sec-edgar-filings"}
    
    # Scan 1: CIK folders directly under base_path (already reorganised)
    _scan_cik_tree(base_path, filings, skip_dirs)
    
    # Scan 2: CIK folders under sec-edgar-filings/ (downloader staging area)
    staging_path = os.path.join(base_path, "sec-edgar-filings")
    if os.path.isdir(staging_path):
        before = len(filings)
        _scan_cik_tree(staging_path, filings, skip_dirs=set())
        logger.info(f"Found {len(filings) - before} additional filings in sec-edgar-filings/")
    
    # De-duplicate by accession number (in case a CIK exists in both locations)
    seen = set()
    unique = []
    for f in filings:
        if f.accession not in seen:
            seen.add(f.accession)
            unique.append(f)
    filings = unique
    
    logger.info(f"Discovered {len(filings)} filings")
    return filings


# ---------------------------------------------------------------------------
# HTML loading
# ---------------------------------------------------------------------------

def load_html(path: str) -> BeautifulSoup:
    """
    Read an HTML file and return a BeautifulSoup parse tree.
    
    We use 'lxml' for speed — it's a C-based parser that is 3-5x faster
    than Python's built-in html.parser. Critical for large SEC filings
    (some are 60MB+). XBRL inline tags are still preserved as plain text.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return BeautifulSoup(f.read(), "lxml")


# ---------------------------------------------------------------------------
# Section boundary detection
# ---------------------------------------------------------------------------

def _is_toc_element(element) -> bool:
    """
    Check if a text node is inside a Table of Contents link.
    
    TOC entries are usually wrapped in <a> tags. We skip these because
    they're just links to the actual section, not the section itself.
    """
    for parent in element.parents:
        if parent.name == "a":
            return True
    return False


def _is_audit_mention(text: str) -> bool:
    """
    Check if text is part of an auditor's report mentioning the schedule.
    
    Auditors often say things like "we have audited the consolidated 
    schedules of investments" — we don't want to start our section there.
    """
    lower = text.lower()
    return any(kw in lower for kw in [
        "we have audited",
        "audited the accompanying",
        "including the consolidated schedule",
    ])


def _is_different_section(text: str) -> bool:
    """
    Check if a text node is a header for a DIFFERENT major section.
    
    This signals the END of our schedule of investments section.
    For example: "Consolidated Statements of Operations" or
    "Notes to Consolidated Financial Statements".
    
    We must NOT match "Consolidated Schedule of Investments (Continued)"
    since that's still part of our section.
    """
    text_clean = text.strip()
    
    # If it still says "Schedule of Investments", it's our section
    if SCHEDULE_HEADER_RE.search(text_clean):
        return False
    
    # Check against the other-section patterns
    return bool(OTHER_SECTION_RE.search(text_clean))


def find_schedule_sections(soup: BeautifulSoup) -> List[Tuple[Tag, Tag, str]]:
    """
    Find all "Consolidated Schedule of Investments" date sub-sections.
    
    Returns a list of tuples: (start_text_node, end_text_node, as_at_date_string)
    
    How it works:
    1. Find all text nodes matching the schedule header pattern
    2. Filter out TOC links and audit mentions
    3. Extract the date for each header
    4. Group headers by date — same date = same section (handles repeated
       page headers and "(Continued)" pages)
    5. For each unique date group: start = first header, end = end-marker
       found AFTER the last header of that group
    
    A 10-K filing typically has 2 sub-sections (current year + prior year).
    A 10-Q typically has 1 sub-section.
    """
    sections = []
    
    # Step 1: Find ALL text nodes matching the schedule header
    all_matches = soup.find_all(string=SCHEDULE_HEADER_RE)
    
    # Step 2: Filter out TOC links, audit mentions, and non-header text
    body_headers = []
    for match in all_matches:
        if _is_toc_element(match):
            continue
        full_text = match.strip()
        if _is_audit_mention(full_text):
            continue
        # Skip "Notes to Consolidated Schedule of Investments:" — this is a
        # mini sub-section header WITHIN the schedule, not a section start
        if re.match(r"Notes\s+to\s+Consolidated", full_text, re.IGNORECASE):
            continue
        # Skip long body text that happens to contain the schedule name
        # (e.g., "Under the 1940 Act, the Company is required to separately
        # identify investments..." or similar prose)
        if len(full_text) > 120:
            continue
        body_headers.append(match)
    
    if not body_headers:
        logger.warning("No schedule of investments headers found in document body")
        return sections
    
    logger.info(f"Found {len(body_headers)} schedule headers in document body")
    
    # Pre-build text node index ONCE so _extract_date and _find_section_end
    # can iterate the pre-built list instead of walking the DOM repeatedly.
    # For a 60 MB file with 200K+ text nodes, this turns O(n) per call into
    # O(1) lookup + small slice — the single biggest performance win.
    all_nodes = list(soup.find_all(string=True))
    id_to_pos = {id(n): i for i, n in enumerate(all_nodes)}

    # Step 3 & 4: Extract date for each header, group by NORMALIZED date
    # Headers with the same date are part of the same section (repeated page headers)
    # We normalize dates to handle "AS OF DECEMBER 31, 2022" vs "December 31, 2022"
    date_groups = []  # List of (date_str, normalized_date, [header_nodes])
    seen_dates = {}   # normalized_date -> index in date_groups
    
    for header in body_headers:
        date_str = _extract_date(header, all_nodes, id_to_pos)
        normalized = _normalize_date(date_str)
        
        # Skip non-quarter-end dates (only keep March 31, June 30, Sept 30, Dec 31)
        if not _is_quarter_end(normalized):
            continue
        
        if normalized in seen_dates:
            # Same date — add to existing group
            date_groups[seen_dates[normalized]][2].append(header)
        else:
            # New date — start a new group
            seen_dates[normalized] = len(date_groups)
            date_groups.append((date_str, normalized, [header]))
    
    logger.info(f"Grouped into {len(date_groups)} unique date sub-section(s) (quarter-ends only)")

    # Step 5: For each date group, find start and end boundaries
    for i, (date_str, normalized, headers) in enumerate(date_groups):
        # Start = first header text node of this date group
        start_node = headers[0]
        # End = search forward from the LAST header of this group
        last_node = headers[-1]
        
        end_node = _find_section_end(last_node, all_nodes, id_to_pos)
        
        sections.append((start_node, end_node, date_str))
        logger.info(f"  Section {i+1}: date='{date_str}', pages={len(headers)}, has_end={end_node is not None}")
    
    return sections


def _find_section_end(last_header_node, all_nodes=None, id_to_pos=None):
    """
    Find the end boundary of a schedule section by walking forward from
    the last header of a date group.
    
    We stop at:
    - "See accompanying notes..." / "The accompanying notes are an integral part..."
    - A different major section header (e.g., "Consolidated Statements of Operations")
    
    We do NOT stop at:
    - Another "Consolidated Schedule of Investments" header (same section, page repeat)
    - "Notes to Consolidated Schedule of Investments:" (mini sub-section within schedule)
    
    Fast path: if all_nodes/id_to_pos are provided, iterate the pre-built
    list from the header's position instead of walking the DOM.
    
    Returns the end text node, or None if not found.
    """
    # Choose the iterator: pre-built list (fast) or DOM walk (fallback)
    if all_nodes is not None and id_to_pos is not None:
        start_pos = id_to_pos.get(id(last_header_node))
        if start_pos is not None:
            iterator = all_nodes[start_pos + 1:]
        else:
            iterator = last_header_node.find_all_next(string=True)
    else:
        iterator = last_header_node.find_all_next(string=True)

    for elem in iterator:
        text = elem.strip()
        if not text:
            continue
        
        # Check for end-of-section markers
        if END_MARKER_RE.search(text):
            return elem
        
        # Check for a different major section header
        if _is_different_section(text) and len(text) < 150:
            return elem
    
    return None


def _extract_date(header_text_node, all_nodes=None, id_to_pos=None) -> str:
    """
    Extract the "as of" date from the schedule header or nearby elements.
    
    The date can appear in three places:
    1. Inside the header text itself: "Consolidated Schedule of Investments as of December 31, 2025"
    2. In a sibling/child element right below: <span>As of December 31, 2025</span>
    3. In the same parent container: <span>December 31, 2024</span>
    
    Fast path: if all_nodes/id_to_pos provided, iterate the pre-built list
    instead of walking the DOM (avoids O(n) per header).
    
    Returns the raw date string (e.g., "December 31, 2025") or "UNKNOWN".
    """
    # Try 1: Check the header text itself
    header_text = header_text_node.strip()
    date_match = AS_OF_DATE_RE.search(header_text)
    if date_match:
        return date_match.group(0).replace("As of ", "").replace("as of ", "")
    
    # Try 2: Check the next few text nodes after the header
    # Use pre-built list if available (fast), otherwise fall back to DOM walk
    if all_nodes is not None and id_to_pos is not None:
        start_pos = id_to_pos.get(id(header_text_node))
        if start_pos is not None:
            iterator = all_nodes[start_pos + 1 : start_pos + 50]
        else:
            iterator = header_text_node.find_all_next(string=True)
    else:
        iterator = header_text_node.find_all_next(string=True)

    count = 0
    for elem in iterator:
        text = elem.strip()
        if not text or len(text) < 3:
            continue
        
        date_match = AS_OF_DATE_RE.search(text)
        if date_match:
            return date_match.group(0).replace("As of ", "").replace("as of ", "")
        
        count += 1
        # Only look at the next 5 meaningful text nodes
        if count > 5:
            break
    
    logger.warning(f"Could not extract date near: '{header_text[:80]}'")
    return "UNKNOWN"


def get_elements_between(start_elem, end_elem, soup: BeautifulSoup) -> List:
    """
    Collect all text nodes between start_elem and end_elem in document order.
    
    This gives us the "slice" of the HTML document that belongs to one
    schedule-of-investments date sub-section. We'll search within this
    slice for footnote definitions.
    
    Returns a list of NavigableString objects (text nodes).
    """
    text_nodes = []
    
    # If no end element, collect until end of document (fallback)
    if end_elem is None:
        for elem in start_elem.find_all_next(string=True):
            text_nodes.append(elem)
        return text_nodes
    
    # Collect all text nodes between start and end
    # We use the document position to determine ordering
    collecting = False
    for elem in soup.find_all(string=True):
        if elem is start_elem or (hasattr(start_elem, 'string') and elem is start_elem.string):
            collecting = True
        
        if collecting:
            text_nodes.append(elem)
        
        if elem.parent is end_elem or elem is end_elem:
            break
        # Also check if elem itself is inside end_elem
        if end_elem in getattr(elem, 'parents', []):
            break
    
    return text_nodes
