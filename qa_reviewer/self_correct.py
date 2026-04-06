"""
Self-Correction — uses Gemini to re-extract footnotes for filings that failed QA.

When the deterministic pipeline produces incorrect results (flagged by the reviewer),
this module sends the raw footnote area text to Gemini and asks it to independently
extract all footnotes. The LLM output replaces the failed deterministic result.

Results from this module are marked extraction_method = "llm" so we can track
which filings needed LLM assistance.
"""

import os
import json
import time
import logging
import concurrent.futures
from typing import Dict, Optional
from collections import OrderedDict

from google import genai

from qa_reviewer.tracing import get_langfuse, create_generation

logger = logging.getLogger(__name__)


def _parse_json_response(text: str, key: str = "footnotes") -> dict:
    """
    Parse JSON from Gemini response, handling markdown code fences and malformed JSON.
    
    Gemini sometimes wraps JSON in ```json ... ``` blocks or includes extra text.
    This function tries multiple strategies to extract valid JSON.
    """
    import re
    
    cleaned = text.strip()
    
    # Strategy 1: Strip markdown code fences
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
                    pass
    
    # Strategy 2: Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    
    # Strategy 3: Find JSON object boundaries
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(cleaned[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
    
    # Strategy 4: Extract items array directly with regex
    # This handles cases where the JSON is malformed but we can still extract data
    items = []
    pattern = r'\{\s*"marker"\s*:\s*"([^"]+)"\s*,\s*"text"\s*:\s*"([^"]*(?:[^"\\]|\\.)*)"'
    for match in re.finditer(pattern, cleaned, re.DOTALL):
        marker = match.group(1)
        fn_text = match.group(2)
        # Unescape JSON strings
        fn_text = fn_text.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        if marker and fn_text:
            items.append({"marker": marker, "text": fn_text})
    
    if items:
        return {key: items}
    
    logger.warning(f"Failed to parse JSON from LLM response: {text[:200]}")
    return {key: []}

MODEL_ID = "gemini-2.5-flash"
CALL_DELAY_SECONDS = 1
MAX_RETRIES = 3


def _get_client() -> genai.Client:
    """Create a Gemini client using GEMINI_API_KEY env var."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


# Timeout for each API call attempt (seconds)
API_TIMEOUT = 180

def _call_with_timeout(client, prompt, temperature, max_tokens):
    """
    Call Gemini API with a thread-based timeout.
    If the API hangs, the thread times out after API_TIMEOUT seconds
    and we raise a TimeoutError instead of blocking forever.
    """
    def _do_call():
        resp = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
            config={"temperature": temperature, "max_output_tokens": max_tokens},
        )
        return resp.text.strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_do_call)
        return future.result(timeout=API_TIMEOUT)


def _is_false_positive_marker(marker: str) -> bool:
    """
    Check if a marker looks like table data rather than a real footnote marker.
    
    False positives are typically:
    - 3+ digit numbers (e.g., 987, 758, 930) - these are table values
    - Decimal numbers (e.g., 930.0, 320.0)
    - Very large numbers
    
    Real footnote markers are typically:
    - Single digits (1-9) or small numbers (10-20)
    - Letters (A-Z, a-z)
    - Symbols (*, **, †)
    - Parenthesized markers ((1), (A))
    """
    import re
    
    # Strip parentheses and whitespace
    clean = marker.strip().strip("()").strip()
    
    # Check if it's a number
    try:
        num = float(clean)
        # Numbers >= 100 are almost certainly table data, not markers
        if num >= 100:
            return True
        # Decimal numbers are likely table data
        if "." in clean and num != int(num):
            return True
    except ValueError:
        pass
    
    return False


def correct_footnotes(
    footnote_area_text: str,
    existing_footnotes: Dict[str, str],
    issues: list,
    missing_markers: list,
    trace=None,
) -> Dict[str, str]:
    """
    Ask Gemini to correct specific footnote issues while keeping correct ones.

    This is a TARGETED correction - not a full re-extraction:
    - Keep footnotes that are already correct
    - Fix footnotes flagged as wrong_text, truncated, or contaminated
    - Add footnotes for missing markers
    - Remove false positive markers (table data picked up as markers)

    Args:
        footnote_area_text: Raw text of the footnote area
        existing_footnotes: Current extracted footnotes {marker: text}
        issues: List of issue dicts from QA (e.g., {"marker": "1", "issue": "wrong_text"})
        missing_markers: List of markers that were not extracted
        trace: Optional Langfuse trace

    Returns:
        OrderedDict of corrected footnotes (merged with existing correct ones)
    """
    client = _get_client()

    # First, filter out false positive markers from existing footnotes
    # These are table data values incorrectly picked up as markers
    filtered_footnotes = OrderedDict()
    removed_false_positives = []
    for marker, text in existing_footnotes.items():
        if _is_false_positive_marker(marker):
            removed_false_positives.append(marker)
        else:
            filtered_footnotes[marker] = text
    
    if removed_false_positives:
        logger.info(f"  Removed {len(removed_false_positives)} false positive markers: {removed_false_positives[:5]}")

    # Identify which markers need correction
    markers_to_fix = set()
    for issue in issues:
        marker = issue.get("marker", "")
        issue_type = issue.get("issue_type", "")
        # Fix wrong_text, truncated, contaminated, wrong_marker issues
        if issue_type in ("wrong_text", "truncated", "contaminated", "wrong_marker"):
            # Only fix if it's not a false positive (we already removed those)
            if not _is_false_positive_marker(marker):
                markers_to_fix.add(marker)
    
    # Add missing markers
    markers_to_add = set(missing_markers)
    
    # If nothing to fix or add, return filtered (false positives removed)
    if not markers_to_fix and not markers_to_add:
        return filtered_footnotes
    
    # Build the prompt for targeted correction
    fix_list = ", ".join(sorted(markers_to_fix)) if markers_to_fix else "none"
    add_list = ", ".join(sorted(markers_to_add)) if markers_to_add else "none"
    
    # Show existing footnotes for context
    existing_str = "\n".join([f"  {m}: {t[:100]}..." if len(t) > 100 else f"  {m}: {t}" 
                               for m, t in existing_footnotes.items()])

    prompt = f"""You are a financial document extraction specialist. I have extracted footnotes from an SEC filing but some are wrong or missing.

---RAW FOOTNOTE AREA---
{footnote_area_text}
---END---

---CURRENT EXTRACTED FOOTNOTES---
{existing_str}
---END---

ISSUES TO FIX:
- Markers with WRONG TEXT (need correction): {fix_list}
- MISSING markers (need to extract): {add_list}

TASK: Provide the CORRECT text for ONLY the markers listed above.
- For wrong text markers: extract the correct definition from the raw text
- For missing markers: extract the definition from the raw text
- Do NOT include markers that are already correct

Respond with ONLY this JSON (no other text):
{{"corrections": [{{"marker": "X", "text": "correct definition text..."}}, ...]}}"""

    # Retry loop for robustness
    for attempt in range(MAX_RETRIES):
        time.sleep(CALL_DELAY_SECONDS)

        # Create generation using v4 API helper (trace parameter ignored in v4)
        generation = create_generation(
            name=f"correct_footnotes_attempt_{attempt+1}",
            model=MODEL_ID,
            input_data=prompt,
            metadata={"purpose": "targeted_correction", "attempt": attempt + 1,
                      "markers_to_fix": list(markers_to_fix),
                      "markers_to_add": list(markers_to_add)},
        )

        try:
            text = _call_with_timeout(client, prompt, temperature=0.1 * attempt, max_tokens=4096)
            result = _parse_json_response(text, key="corrections")
            corrections_list = result.get("corrections", [])

            # Merge corrections with filtered footnotes (false positives already removed)
            merged = OrderedDict(filtered_footnotes)
            corrections_applied = 0
            
            for corr in corrections_list:
                marker = str(corr.get("marker", ""))
                corr_text = corr.get("text", "").strip()
                if marker and corr_text:
                    merged[marker] = corr_text
                    corrections_applied += 1

            if generation is not None:
                try:
                    generation.update(output=json.dumps({
                        "corrections_applied": corrections_applied,
                        "total_footnotes": len(merged),
                        "markers_corrected": [c.get("marker") for c in corrections_list]
                    }))
                    generation.end()
                except Exception:
                    pass

            if corrections_applied > 0:
                logger.info(f"  LLM corrected {corrections_applied} footnotes (attempt {attempt+1})")
                return merged
            
            logger.warning(f"  LLM returned 0 corrections (attempt {attempt+1}/{MAX_RETRIES})")

        except Exception as e:
            if generation is not None:
                try:
                    generation.update(output=f"ERROR: {str(e)}", level="ERROR")
                    generation.end()
                except Exception:
                    pass
            logger.warning(f"  LLM correction attempt {attempt+1} failed: {e}")
    
    logger.error(f"  LLM correction failed after {MAX_RETRIES} attempts")
    return OrderedDict(existing_footnotes)


def re_extract_footnotes(
    footnote_area_text: str,
    trace=None,
) -> Dict[str, str]:
    """
    Ask Gemini to independently extract all footnotes from raw section text.

    This is the FULL re-extraction fallback when targeted correction fails.
    The LLM reads the raw text and returns a clean marker -> definition mapping.

    Args:
        footnote_area_text: Raw text of the footnote area (~4000 chars)
        trace: Optional Langfuse trace to log this call under

    Returns:
        OrderedDict of marker -> definition text, or empty dict on failure
    """
    client = _get_client()

    prompt = f"""You are a financial document extraction specialist. Below is the raw text from the footnote area of a Consolidated Schedule of Investments in an SEC filing.

---FOOTNOTE AREA---
{footnote_area_text}
---END---

TASK: Extract ALL footnote definitions from this text.

Footnotes are marked with patterns like (1), (2), (A), (B), (a), (b), *, **, etc.
Each marker is followed by its definition text.

Rules:
- Only extract DEFINITION footnotes (marker followed by explanatory text)
- Do NOT include inline references from investment data rows
- Capture the full definition text for each marker
- Preserve the original marker format (number or letter)

Respond with ONLY this JSON (no other text):
{{"footnotes": [{{"marker": "1", "text": "full definition text..."}}, {{"marker": "2", "text": "..."}}]}}"""

    # Retry loop for robustness
    for attempt in range(MAX_RETRIES):
        time.sleep(CALL_DELAY_SECONDS)

        # Create generation using v4 API helper
        generation = create_generation(
            name=f"self_correct_re_extract_attempt_{attempt+1}",
            model=MODEL_ID,
            input_data=prompt,
            metadata={"purpose": "self_correction", "attempt": attempt + 1},
        )

        try:
            text = _call_with_timeout(client, prompt, temperature=0.1 * attempt, max_tokens=4096)
            result = _parse_json_response(text)
            footnotes_list = result.get("footnotes", [])

            # Convert to OrderedDict
            footnotes = OrderedDict()
            for fn in footnotes_list:
                marker = str(fn.get("marker", ""))
                fn_text = fn.get("text", "").strip()
                if marker and fn_text:
                    footnotes[marker] = fn_text

            # Log output to Langfuse
            if generation is not None:
                try:
                    generation.update(output=json.dumps({"num_footnotes": len(footnotes), "markers": list(footnotes.keys())}))
                    generation.end()
                except Exception:
                    pass

            # Success if we got at least 1 footnote
            if footnotes:
                logger.info(f"  LLM re-extracted {len(footnotes)} footnotes (attempt {attempt+1})")
                return footnotes
            
            # No footnotes extracted - retry
            logger.warning(f"  LLM returned 0 footnotes (attempt {attempt+1}/{MAX_RETRIES})")

        except Exception as e:
            if generation is not None:
                try:
                    generation.update(output=f"ERROR: {str(e)}", level="ERROR")
                    generation.end()
                except Exception:
                    pass
            logger.warning(f"  LLM re-extraction attempt {attempt+1} failed: {e}")
    
    logger.error(f"  LLM re-extraction failed after {MAX_RETRIES} attempts")
    return OrderedDict()
