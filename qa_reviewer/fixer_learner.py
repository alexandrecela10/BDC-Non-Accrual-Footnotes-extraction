"""
Fixer Learner — observes QA failures and logs patterns WITHOUT modifying output.

This is a "learning-only" mode that:
1. Receives QA issues (missing markers, wrong text, contamination, etc.)
2. Analyzes the patterns in these mistakes
3. Logs learnings to a persistent file (output/fixer_learnings.json)
4. Does NOT modify the extracted footnotes

The learnings can later be used to:
- Improve the deterministic extractor rules
- Train better patterns for the fixer
- Identify systematic extraction failures

Runs in parallel with QA review — doesn't block or slow down the pipeline.
"""

import os
import re
import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger(__name__)

# Path to learnings file — accumulates patterns across runs
LEARNINGS_FILE = os.path.join("output", "fixer_learnings.json")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class LearningEntry:
    """One learning from a QA failure."""
    cik: str
    file_code: str
    as_at_date: str
    issue_type: str           # missing_marker, wrong_text, contaminated, truncated
    marker: str               # which marker had the issue
    detail: str               # description of the issue
    pattern: str              # extracted pattern (e.g., header text, marker format)
    suggested_fix: str        # what fix strategy would help
    timestamp: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass 
class LearningRequest:
    """Input to the learner for one section."""
    cik: str
    file_code: str
    as_at_date: str
    html_path: str
    # Issues from QAVerdict.accuracy_check.details["issues"]
    issues: List[Dict] = field(default_factory=list)
    # Missing markers from completeness_check
    missing_markers: List[str] = field(default_factory=list)
    # Current extracted footnotes (for context)
    extracted_footnotes: Dict[str, str] = field(default_factory=dict)
    # Raw footnote area text for pattern analysis
    footnote_area_text: str = ""


# ---------------------------------------------------------------------------
# Learnings storage
# ---------------------------------------------------------------------------

def _load_learnings() -> Dict:
    """
    Load accumulated learnings from disk.
    
    Structure:
    {
        "entries": [LearningEntry, ...],
        "patterns": {
            "page_headers": ["pattern1", ...],
            "missing_marker_contexts": ["context1", ...],
            "contamination_sources": ["source1", ...],
        },
        "stats": {
            "total_issues_seen": 100,
            "by_type": {"missing_marker": 30, "contaminated": 25, ...},
            "by_cik": {"0001655888": 15, ...},
        },
        "last_updated": "2026-03-15T21:00:00"
    }
    """
    if os.path.exists(LEARNINGS_FILE):
        try:
            with open(LEARNINGS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning(f"Could not load learnings from {LEARNINGS_FILE}, starting fresh")
    
    return {
        "entries": [],
        "patterns": {
            "page_headers": [],
            "missing_marker_contexts": [],
            "contamination_sources": [],
        },
        "stats": {
            "total_issues_seen": 0,
            "by_type": {},
            "by_cik": {},
        },
        "last_updated": "",
    }


def _save_learnings(learnings: Dict) -> None:
    """Save learnings to disk."""
    learnings["last_updated"] = datetime.utcnow().isoformat()
    os.makedirs(os.path.dirname(LEARNINGS_FILE), exist_ok=True)
    with open(LEARNINGS_FILE, "w") as f:
        json.dump(learnings, f, indent=2, ensure_ascii=False)
    logger.debug(f"Learnings saved to {LEARNINGS_FILE}")


# ---------------------------------------------------------------------------
# Pattern extraction helpers
# ---------------------------------------------------------------------------

def _extract_header_pattern(text: str) -> Optional[str]:
    """
    Extract page header patterns from contaminated footnote text.
    
    Looks for common SEC filing header formats:
    - "F-XX" page numbers
    - Company names in ALL CAPS or Title Case
    - "(Unaudited)" markers
    """
    patterns_found = []
    
    # F-XX page numbers
    f_match = re.search(r'F-\s*\d+', text)
    if f_match:
        patterns_found.append(f_match.group(0))
    
    # Company names (Title Case followed by common suffixes)
    company_match = re.search(
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*'
        r'(?:Corporation|Corp\.?|Capital|Partners|LLC|Inc\.?)',
        text
    )
    if company_match:
        patterns_found.append(company_match.group(0))
    
    # ALL CAPS headers (likely page headers)
    caps_match = re.search(r'\b([A-Z]{2,}(?:\s+[A-Z]{2,})+)\b', text)
    if caps_match and len(caps_match.group(0)) > 10:
        patterns_found.append(caps_match.group(0))
    
    return patterns_found[0] if patterns_found else None


def _extract_missing_marker_context(
    marker: str, 
    footnote_area_text: str
) -> Optional[str]:
    """
    Find context around a missing marker in the raw text.
    
    This helps understand WHY the marker was missed — was it:
    - In an unusual format?
    - Part of a table structure?
    - Hidden in XBRL tags?
    """
    # Look for the marker in various formats
    patterns = [
        rf'\({re.escape(marker)}\)',  # (1), (A)
        rf'\[{re.escape(marker)}\]',  # [1], [A]
        rf'(?<!\d){re.escape(marker)}(?!\d)',  # standalone number
    ]
    
    for pattern in patterns:
        match = re.search(pattern, footnote_area_text)
        if match:
            # Extract surrounding context (50 chars before/after)
            start = max(0, match.start() - 50)
            end = min(len(footnote_area_text), match.end() + 50)
            return footnote_area_text[start:end]
    
    return None


def _suggest_fix_strategy(issue_type: str, detail: str) -> str:
    """
    Suggest which fix strategy would address this issue.
    
    Maps issue types to fixer strategies:
    - contaminated → strip_page_headers
    - wrong_marker (*, **) → reclassify_sub_markers  
    - wrong_text → recover_missing_values
    - missing_marker → extract_missing / improve extractor regex
    - truncated → increase evidence window / multi-page handling
    """
    if issue_type == "contaminated":
        return "strip_page_headers"
    elif issue_type == "wrong_marker":
        if "*" in detail:
            return "reclassify_sub_markers"
        return "review_marker_detection"
    elif issue_type == "wrong_text":
        if "XBRL" in detail.lower() or "numeric" in detail.lower():
            return "recover_missing_values"
        return "improve_text_extraction"
    elif issue_type == "missing_marker":
        return "extract_missing / improve_extractor_regex"
    elif issue_type == "truncated":
        return "increase_evidence_window / multi_page_handling"
    else:
        return "manual_review"


# ---------------------------------------------------------------------------
# Main learning function
# ---------------------------------------------------------------------------

def learn_from_issues(request: LearningRequest) -> int:
    """
    Analyze QA issues and log learnings WITHOUT modifying output.
    
    This is the "learning-only" mode:
    1. Load existing learnings
    2. Analyze each issue for patterns
    3. Log new learnings
    4. Update statistics
    5. Save to disk
    
    Returns the number of new learnings recorded.
    """
    if not request.issues and not request.missing_markers:
        return 0
    
    learnings = _load_learnings()
    new_entries = []
    timestamp = datetime.utcnow().isoformat()
    
    # Process accuracy issues (contaminated, wrong_text, truncated, etc.)
    for issue in request.issues:
        issue_type = issue.get("issue_type", "unknown")
        marker = issue.get("marker", "?")
        detail = issue.get("detail", "")
        
        # Extract pattern based on issue type
        pattern = ""
        if issue_type == "contaminated":
            # Try to find the contaminating header
            fn_text = request.extracted_footnotes.get(marker, "")
            pattern = _extract_header_pattern(fn_text) or ""
            if pattern and pattern not in learnings["patterns"]["page_headers"]:
                learnings["patterns"]["page_headers"].append(pattern)
        
        entry = LearningEntry(
            cik=request.cik,
            file_code=request.file_code,
            as_at_date=request.as_at_date,
            issue_type=issue_type,
            marker=marker,
            detail=detail[:200],
            pattern=pattern,
            suggested_fix=_suggest_fix_strategy(issue_type, detail),
            timestamp=timestamp,
        )
        new_entries.append(entry)
        
        # Update stats
        learnings["stats"]["total_issues_seen"] += 1
        learnings["stats"]["by_type"][issue_type] = \
            learnings["stats"]["by_type"].get(issue_type, 0) + 1
        learnings["stats"]["by_cik"][request.cik] = \
            learnings["stats"]["by_cik"].get(request.cik, 0) + 1
    
    # Process missing markers
    for marker in request.missing_markers:
        # Find context around the missing marker
        context = _extract_missing_marker_context(marker, request.footnote_area_text)
        if context and context not in learnings["patterns"]["missing_marker_contexts"]:
            learnings["patterns"]["missing_marker_contexts"].append(context[:100])
        
        entry = LearningEntry(
            cik=request.cik,
            file_code=request.file_code,
            as_at_date=request.as_at_date,
            issue_type="missing_marker",
            marker=marker,
            detail=f"Marker ({marker}) not extracted by deterministic extractor",
            pattern=context[:100] if context else "",
            suggested_fix="extract_missing / improve_extractor_regex",
            timestamp=timestamp,
        )
        new_entries.append(entry)
        
        # Update stats
        learnings["stats"]["total_issues_seen"] += 1
        learnings["stats"]["by_type"]["missing_marker"] = \
            learnings["stats"]["by_type"].get("missing_marker", 0) + 1
        learnings["stats"]["by_cik"][request.cik] = \
            learnings["stats"]["by_cik"].get(request.cik, 0) + 1
    
    # Add new entries to learnings
    learnings["entries"].extend([e.to_dict() for e in new_entries])
    
    # Save updated learnings
    _save_learnings(learnings)
    
    if new_entries:
        logger.info(f"  Learner: recorded {len(new_entries)} new learnings")
        for entry in new_entries[:3]:  # Log first 3
            logger.info(f"    [{entry.issue_type}] ({entry.marker}): {entry.suggested_fix}")
        if len(new_entries) > 3:
            logger.info(f"    ... and {len(new_entries) - 3} more")
    
    return len(new_entries)


def get_learning_summary() -> Dict:
    """
    Get a summary of all learnings for reporting.
    
    Returns stats and top patterns that could improve the extractor.
    """
    learnings = _load_learnings()
    
    return {
        "total_issues": learnings["stats"]["total_issues_seen"],
        "by_type": learnings["stats"]["by_type"],
        "by_cik": learnings["stats"]["by_cik"],
        "unique_page_headers": len(learnings["patterns"]["page_headers"]),
        "page_headers": learnings["patterns"]["page_headers"][:10],
        "missing_marker_contexts": learnings["patterns"]["missing_marker_contexts"][:5],
        "last_updated": learnings["last_updated"],
    }
