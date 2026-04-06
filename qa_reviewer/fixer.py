"""
Fixer Agent — surgically repairs specific QA issues flagged by the reviewer.

Instead of re-extracting everything (like self_correct.py), this module
applies targeted fixes to individual footnotes based on the exact error type.

Fix strategies (in order of application):
1. strip_page_headers  — removes page header/footer contamination
2. reclassify_sub_markers — moves *, ** sub-footnotes into parent
3. recover_missing_values — uses Gemini to fill dropped XBRL numeric values

Truncated issues are NOT fixed here (deferred to future iteration).

The fixer maintains a persistent memory file (output/fixer_memory.json)
that accumulates patterns it has seen and fixes it has learned, so it
gets smarter over time across runs.
"""

import os
import re
import json
import time
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from collections import OrderedDict
from datetime import datetime

from google import genai

from qa_reviewer.tracing import get_langfuse

logger = logging.getLogger(__name__)

MODEL_ID = "gemini-2.5-flash"
CALL_DELAY_SECONDS = 0.5

# Path to persistent memory file — accumulates patterns across runs
MEMORY_FILE = os.path.join("output", "fixer_memory.json")


# ---------------------------------------------------------------------------
# Structured I/O contracts
# ---------------------------------------------------------------------------

@dataclass
class FixRequest:
    """
    Input to the fixer agent for one section.
    Contains the QA issues, the current (broken) footnotes, and raw evidence.
    """
    cik: str
    file_code: str
    as_at_date: str
    html_path: str
    # Issues from QAVerdict.accuracy_check.details["issues"]
    issues: List[Dict] = field(default_factory=list)
    # Missing markers from completeness_check
    missing_markers: List[str] = field(default_factory=list)
    # Current extracted footnotes (may have errors)
    extracted_footnotes: Dict[str, str] = field(default_factory=dict)
    # Raw footnote area text for LLM repair
    footnote_area_text: str = ""


@dataclass
class FixAction:
    """One fix applied to a specific footnote."""
    marker: str
    issue_type: str       # original issue type from QA
    fix_type: str         # strategy used: strip_page_headers, reclassify_sub_markers, etc.
    description: str      # human-readable explanation of what was fixed
    before_text: str = "" # text before fix (for audit trail)
    after_text: str = ""  # text after fix


@dataclass
class FixResult:
    """
    Output from the fixer agent for one section.
    Contains corrected footnotes and metrics.
    """
    corrected_footnotes: Dict[str, str] = field(default_factory=dict)
    fixes_applied: List[FixAction] = field(default_factory=list)
    skipped_issues: List[Dict] = field(default_factory=list)  # truncated issues we skip
    fix_rate: float = 0.0  # fixes_applied / fixable_issues

    def to_dict(self) -> Dict:
        return {
            "num_fixes": len(self.fixes_applied),
            "num_skipped": len(self.skipped_issues),
            "fix_rate": self.fix_rate,
            "fixes": [asdict(f) for f in self.fixes_applied],
            "skipped": self.skipped_issues,
        }


# ---------------------------------------------------------------------------
# Persistent memory — learns patterns across runs
# ---------------------------------------------------------------------------

def _load_memory() -> Dict:
    """
    Load the fixer's persistent memory from disk.
    
    Memory structure:
    {
        "page_header_patterns": ["F- Owl Rock Capital Corporation", ...],
        "known_fixes": [
            {"issue_type": "contaminated", "pattern": "...", "fix": "strip", "count": 5},
            ...
        ],
        "stats": {"total_issues_seen": 100, "total_fixed": 80},
        "last_updated": "2026-03-12T21:00:00"
    }
    """
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning(f"Could not load fixer memory from {MEMORY_FILE}, starting fresh")
    
    # Default empty memory
    return {
        "page_header_patterns": [],
        "known_fixes": [],
        "stats": {"total_issues_seen": 0, "total_fixed": 0},
        "last_updated": "",
    }


def _save_memory(memory: Dict) -> None:
    """Save updated memory to disk."""
    memory["last_updated"] = datetime.utcnow().isoformat()
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)
    logger.debug(f"Fixer memory saved to {MEMORY_FILE}")


def _update_memory_with_fix(memory: Dict, fix: FixAction) -> None:
    """
    Record a successful fix in memory so we can reuse the pattern later.
    
    For contamination fixes, we store the exact header pattern we stripped.
    For wrong_marker fixes, we store the reclassification rule.
    This lets the fixer get faster on future runs — it checks memory first
    before calling the LLM.
    """
    # Track page header patterns we've seen
    if fix.fix_type == "strip_page_headers" and fix.before_text != fix.after_text:
        # Extract what was stripped
        stripped = fix.before_text.replace(fix.after_text, "").strip()
        if stripped and stripped not in memory["page_header_patterns"]:
            memory["page_header_patterns"].append(stripped)
            logger.info(f"  Memory: learned new page header pattern: '{stripped[:60]}'")
    
    # Track all fixes as known patterns
    known = {
        "issue_type": fix.issue_type,
        "fix_type": fix.fix_type,
        "marker": fix.marker,
        "description": fix.description,
        "timestamp": datetime.utcnow().isoformat(),
    }
    
    # Check if we've seen this exact pattern before — increment count
    for existing in memory["known_fixes"]:
        if (existing["issue_type"] == fix.issue_type 
            and existing["fix_type"] == fix.fix_type
            and existing.get("description", "")[:50] == fix.description[:50]):
            existing["count"] = existing.get("count", 1) + 1
            return
    
    # New pattern — add it
    known["count"] = 1
    memory["known_fixes"].append(known)


# ---------------------------------------------------------------------------
# Fix Strategy 1: Strip page headers/footers from contaminated footnotes
# ---------------------------------------------------------------------------

# Common page header/footer patterns found in SEC filings
# These get inserted mid-footnote when footnotes span page breaks
PAGE_HEADER_PATTERNS = [
    # "F- Company Name" pattern (SEC page numbering)
    re.compile(r"\s*F-\s*\d*\s*"),
    # Company names that appear as headers (we build these dynamically too)
    re.compile(r"\s*(Owl Rock Capital Corporation|BlackRock TCP Capital Corp\.?)(\s*\(Unaudited\))?\s*"),
    # "TABLE OF CONTENTS" leaking in
    re.compile(r"\s*TABLE OF CONTENTS\s*"),
]


def _strip_page_headers(text: str, memory: Dict) -> Tuple[str, bool]:
    """
    Remove known page header/footer patterns from footnote text.
    
    Uses both hardcoded patterns AND learned patterns from memory.
    Returns (cleaned_text, was_modified).
    """
    original = text
    
    # Apply hardcoded patterns
    for pattern in PAGE_HEADER_PATTERNS:
        text = pattern.sub(" ", text)
    
    # Apply learned patterns from memory
    for header_pattern in memory.get("page_header_patterns", []):
        if header_pattern in text:
            text = text.replace(header_pattern, " ")
    
    # Clean up whitespace artifacts from stripping
    text = re.sub(r"\s{2,}", " ", text).strip()
    
    return text, text != original


# ---------------------------------------------------------------------------
# Fix Strategy 2: Reclassify sub-markers (*, **) into parent footnote
# ---------------------------------------------------------------------------

def _reclassify_sub_markers(
    footnotes: Dict[str, str],
    issues: List[Dict],
) -> Tuple[Dict[str, str], List[FixAction]]:
    """
    If QA flagged * or ** as wrong_marker (sub-footnote of a parent),
    remove them from the main footnotes dict and append their text
    to the parent footnote if identifiable.
    
    Returns (updated_footnotes, list_of_fixes_applied).
    """
    fixes = []
    updated = OrderedDict(footnotes)
    
    # Find wrong_marker issues for * and **
    sub_marker_issues = [
        i for i in issues
        if i.get("issue_type") == "wrong_marker"
        and i.get("marker", "").startswith("*")
    ]
    
    if not sub_marker_issues:
        return updated, fixes
    
    for issue in sub_marker_issues:
        marker = issue["marker"]
        if marker not in updated:
            continue
        
        sub_text = updated[marker]
        
        # Try to find the parent footnote that references this sub-marker.
        # Usually it's the footnote immediately before the * in the dict.
        keys = list(updated.keys())
        marker_idx = keys.index(marker) if marker in keys else -1
        
        # Walk backward to find the first non-star marker before this one
        parent_marker = None
        if marker_idx > 0:
            for k in range(marker_idx - 1, -1, -1):
                if not keys[k].startswith("*"):
                    parent_marker = keys[k]
                    break
        
        if parent_marker:
            # Append sub-footnote text to parent
            before_text = updated[parent_marker]
            updated[parent_marker] = f"{before_text} [{marker} {sub_text}]"
            # Remove the sub-marker entry
            del updated[marker]
            
            fixes.append(FixAction(
                marker=marker,
                issue_type="wrong_marker",
                fix_type="reclassify_sub_markers",
                description=f"Moved sub-footnote {marker} into parent footnote ({parent_marker})",
                before_text=sub_text,
                after_text=f"[merged into ({parent_marker})]",
            ))
        else:
            # Can't find parent — just remove the standalone sub-marker
            del updated[marker]
            fixes.append(FixAction(
                marker=marker,
                issue_type="wrong_marker",
                fix_type="reclassify_sub_markers",
                description=f"Removed orphan sub-footnote {marker} (no parent found)",
                before_text=sub_text,
                after_text="[removed]",
            ))
    
    return updated, fixes


# ---------------------------------------------------------------------------
# Fix Strategy 3: Recover missing XBRL values via Gemini
# ---------------------------------------------------------------------------

def _recover_missing_values(
    footnotes: Dict[str, str],
    issues: List[Dict],
    footnote_area_text: str,
    memory: Dict,
    trace=None,
) -> Tuple[Dict[str, str], List[FixAction]]:
    """
    For 'wrong_text' issues (usually dropped XBRL numeric values),
    ask Gemini to find the correct values from the raw evidence.
    
    Batches all wrong_text issues into a single LLM call for efficiency.
    """
    wrong_text_issues = [
        i for i in issues if i.get("issue_type") == "wrong_text"
    ]
    
    if not wrong_text_issues:
        return footnotes, []
    
    # Build a focused prompt with just the problematic footnotes
    issue_descriptions = []
    for issue in wrong_text_issues:
        marker = issue.get("marker", "?")
        detail = issue.get("detail", "")
        current_text = footnotes.get(marker, "")
        issue_descriptions.append({
            "marker": marker,
            "current_text": current_text[:500],
            "issue": detail,
        })
    
    prompt = f"""You are a financial document extraction specialist. 

I have extracted footnotes from a Consolidated Schedule of Investments, but some numeric values were dropped during extraction (they were inside XBRL tags in the HTML).

Here are the problematic footnotes and what's wrong with each:

{json.dumps(issue_descriptions, indent=2)}

Here is the raw text from the footnote area of the document:

---RAW TEXT---
{footnote_area_text[:8000]}
---END---

For each problematic footnote, find the correct complete text from the raw evidence. 
Return ONLY this JSON:
{{"fixes": [{{"marker": "X", "corrected_text": "the full correct footnote text"}}]}}"""

    try:
        client = _get_client()
        time.sleep(CALL_DELAY_SECONDS)
        
        # Langfuse tracing
        generation = None
        if trace is not None:
            generation = trace.start_generation(
                name="fixer_recover_values",
                model=MODEL_ID,
                input=prompt,
                metadata={"num_issues": len(wrong_text_issues)},
            )
        
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
            config={"temperature": 0.0, "max_output_tokens": 4096},
        )
        
        text = response.text.strip()
        if generation is not None:
            generation.update(output=text)
            generation.end()
        
        # Parse response
        result = _parse_json(text)
        llm_fixes = result.get("fixes", [])
        
        fixes = []
        updated = OrderedDict(footnotes)
        for fix in llm_fixes:
            marker = str(fix.get("marker", ""))
            corrected = fix.get("corrected_text", "").strip()
            if marker and corrected and marker in updated:
                before = updated[marker]
                updated[marker] = corrected
                fixes.append(FixAction(
                    marker=marker,
                    issue_type="wrong_text",
                    fix_type="recover_missing_values",
                    description=f"Recovered missing values in footnote ({marker})",
                    before_text=before[:200],
                    after_text=corrected[:200],
                ))
        
        return updated, fixes
        
    except Exception as e:
        logger.error(f"  Fixer LLM call failed: {e}")
        return footnotes, []


# ---------------------------------------------------------------------------
# Fix Strategy 4: Extract missing markers via Gemini
# ---------------------------------------------------------------------------

def _extract_missing_markers(
    footnotes: Dict[str, str],
    missing_markers: List[str],
    footnote_area_text: str,
    memory: Dict,
    trace=None,
) -> Tuple[Dict[str, str], List[FixAction]]:
    """
    For markers that the machine missed entirely, ask Gemini to find
    them in the raw evidence and return their definition text.
    """
    if not missing_markers:
        return footnotes, []
    
    prompt = f"""You are a financial document extraction specialist.

I extracted footnotes from a Consolidated Schedule of Investments, but I missed these markers: {missing_markers}

Here is the raw text from the footnote area:

---RAW TEXT---
{footnote_area_text[:8000]}
---END---

Find the definition text for EACH missing marker. The markers are formatted like ({missing_markers[0]}) in the text.

Return ONLY this JSON:
{{"found": [{{"marker": "{missing_markers[0]}", "text": "the full definition text..."}}]}}"""

    try:
        client = _get_client()
        time.sleep(CALL_DELAY_SECONDS)
        
        generation = None
        if trace is not None:
            generation = trace.start_generation(
                name="fixer_extract_missing",
                model=MODEL_ID,
                input=prompt,
                metadata={"missing_markers": missing_markers},
            )
        
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
            config={"temperature": 0.0, "max_output_tokens": 4096},
        )
        
        text = response.text.strip()
        if generation is not None:
            generation.update(output=text)
            generation.end()
        
        result = _parse_json(text)
        found = result.get("found", [])
        
        fixes = []
        updated = OrderedDict(footnotes)
        for item in found:
            marker = str(item.get("marker", ""))
            fn_text = item.get("text", "").strip()
            if marker and fn_text and marker not in updated:
                # Insert at the right position (sorted)
                updated[marker] = fn_text
                fixes.append(FixAction(
                    marker=marker,
                    issue_type="missing_marker",
                    fix_type="extract_missing",
                    description=f"Recovered missing footnote ({marker}) from raw evidence",
                    before_text="[not extracted]",
                    after_text=fn_text[:200],
                ))
        
        return updated, fixes
        
    except Exception as e:
        logger.error(f"  Fixer LLM call for missing markers failed: {e}")
        return footnotes, []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_client() -> genai.Client:
    """Create a Gemini client."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")
    return genai.Client(api_key=api_key)


def _parse_json(text: str) -> Dict:
    """Parse JSON from Gemini response, handling code fences."""
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
    logger.warning(f"Could not parse JSON from fixer response: {cleaned[:200]}")
    return {}


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def fix_issues(request: FixRequest, trace=None) -> FixResult:
    """
    Main entry point: apply targeted fixes to QA-flagged issues.
    
    Strategy order:
    1. Load memory (learned patterns from previous runs)
    2. Separate issues into fixable vs skipped (truncated = skipped)
    3. Apply deterministic fixes first (fast, no LLM needed):
       - strip_page_headers for contaminated
       - reclassify_sub_markers for wrong_marker
    4. Apply LLM fixes for remaining issues:
       - recover_missing_values for wrong_text
       - extract_missing for missing_marker
    5. Update memory with new patterns learned
    6. Return FixResult with corrected footnotes and metrics
    
    Args:
        request: FixRequest with issues and current footnotes
        trace: Optional Langfuse trace for observability
    
    Returns:
        FixResult with corrected footnotes, fixes applied, and fix_rate
    """
    logger.info(f"  Fixer: {len(request.issues)} accuracy issues, "
                f"{len(request.missing_markers)} missing markers")
    
    # Step 1: Load persistent memory
    memory = _load_memory()
    logger.debug(f"  Fixer memory: {len(memory['known_fixes'])} known patterns, "
                 f"{len(memory['page_header_patterns'])} header patterns")
    
    # Step 2: Separate fixable issues from skipped (truncated)
    fixable_issues = []
    skipped_issues = []
    for issue in request.issues:
        issue_type = issue.get("issue_type", "")
        # Skip truncated issues — not fixing those yet
        if "truncated" in issue_type:
            skipped_issues.append(issue)
        else:
            fixable_issues.append(issue)
    
    total_fixable = len(fixable_issues) + len(request.missing_markers)
    logger.info(f"  Fixer: {total_fixable} fixable issues, "
                f"{len(skipped_issues)} skipped (truncated)")
    
    if total_fixable == 0:
        return FixResult(
            corrected_footnotes=dict(request.extracted_footnotes),
            skipped_issues=skipped_issues,
            fix_rate=0.0,
        )
    
    # Start with a copy of the current footnotes
    footnotes = OrderedDict(request.extracted_footnotes)
    all_fixes = []
    
    # Step 3a: Deterministic fix — strip page headers from contaminated
    contaminated = [i for i in fixable_issues if i.get("issue_type") == "contaminated"]
    header_span = None
    if trace is not None and contaminated:
        try:
            header_span = trace.span(
                name="fix_strip_page_headers",
                input={
                    "strategy": "strip_page_headers",
                    "num_contaminated_issues": len(contaminated),
                    "markers": [i.get("marker", "?") for i in contaminated],
                    "known_header_patterns": memory.get("page_header_patterns", []),
                },
            )
        except Exception:
            pass
    header_fixes = []
    for issue in contaminated:
        marker = issue.get("marker", "")
        if marker not in footnotes:
            continue
        before = footnotes[marker]
        cleaned, was_modified = _strip_page_headers(before, memory)
        if was_modified:
            footnotes[marker] = cleaned
            fix = FixAction(
                marker=marker,
                issue_type="contaminated",
                fix_type="strip_page_headers",
                description=f"Stripped page header/footer from footnote ({marker})",
                before_text=before[:200],
                after_text=cleaned[:200],
            )
            header_fixes.append(fix)
            all_fixes.append(fix)
            _update_memory_with_fix(memory, fix)
    if header_span is not None:
        try:
            header_span.update(output={
                "num_fixed": len(header_fixes),
                "fixes": [{"marker": f.marker, "description": f.description} for f in header_fixes],
            })
            header_span.end()
        except Exception:
            pass
    
    # Step 3b: Deterministic fix — reclassify sub-markers
    wrong_marker_issues = [i for i in fixable_issues if i.get("issue_type") == "wrong_marker"]
    sub_span = None
    if trace is not None and wrong_marker_issues:
        try:
            sub_span = trace.span(
                name="fix_reclassify_sub_markers",
                input={
                    "strategy": "reclassify_sub_markers",
                    "num_wrong_marker_issues": len(wrong_marker_issues),
                    "markers": [i.get("marker", "?") for i in wrong_marker_issues],
                },
            )
        except Exception:
            pass
    footnotes, sub_fixes = _reclassify_sub_markers(footnotes, fixable_issues)
    for fix in sub_fixes:
        all_fixes.append(fix)
        _update_memory_with_fix(memory, fix)
    if sub_span is not None:
        try:
            sub_span.update(output={
                "num_fixed": len(sub_fixes),
                "fixes": [{"marker": f.marker, "description": f.description} for f in sub_fixes],
            })
            sub_span.end()
        except Exception:
            pass
    
    # Step 4a: LLM fix — recover missing XBRL values (wrong_text)
    wrong_text_issues = [i for i in fixable_issues if i.get("issue_type") == "wrong_text"]
    recover_span = None
    if trace is not None and wrong_text_issues:
        try:
            recover_span = trace.span(
                name="fix_recover_missing_values",
                input={
                    "strategy": "recover_missing_values",
                    "num_wrong_text_issues": len(wrong_text_issues),
                    "issues": wrong_text_issues,
                },
            )
        except Exception:
            pass
    footnotes, value_fixes = _recover_missing_values(
        footnotes, fixable_issues, request.footnote_area_text, memory, trace=trace
    )
    for fix in value_fixes:
        all_fixes.append(fix)
        _update_memory_with_fix(memory, fix)
    if recover_span is not None:
        try:
            recover_span.update(output={
                "num_fixed": len(value_fixes),
                "fixes": [{"marker": f.marker, "description": f.description} for f in value_fixes],
            })
            recover_span.end()
        except Exception:
            pass
    
    # Step 4b: LLM fix — extract missing markers
    extract_span = None
    if trace is not None and request.missing_markers:
        try:
            extract_span = trace.span(
                name="fix_extract_missing_markers",
                input={
                    "strategy": "extract_missing",
                    "missing_markers": request.missing_markers,
                },
            )
        except Exception:
            pass
    footnotes, missing_fixes = _extract_missing_markers(
        footnotes, request.missing_markers, request.footnote_area_text, memory, trace=trace
    )
    for fix in missing_fixes:
        all_fixes.append(fix)
        _update_memory_with_fix(memory, fix)
    if extract_span is not None:
        try:
            extract_span.update(output={
                "num_found": len(missing_fixes),
                "fixes": [{"marker": f.marker, "description": f.description} for f in missing_fixes],
            })
            extract_span.end()
        except Exception:
            pass
    
    # Step 5: Update stats and save memory
    memory["stats"]["total_issues_seen"] += total_fixable
    memory["stats"]["total_fixed"] += len(all_fixes)
    _save_memory(memory)
    
    # Step 6: Calculate fix rate
    fix_rate = len(all_fixes) / total_fixable if total_fixable > 0 else 0.0
    
    logger.info(f"  Fixer: applied {len(all_fixes)}/{total_fixable} fixes "
                f"(rate={fix_rate:.0%})")
    for fix in all_fixes:
        logger.info(f"    ✓ [{fix.fix_type}] ({fix.marker}): {fix.description}")
    
    return FixResult(
        corrected_footnotes=dict(footnotes),
        fixes_applied=all_fixes,
        skipped_issues=skipped_issues,
        fix_rate=fix_rate,
    )
