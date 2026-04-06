"""
LLM Reviewer — uses Gemini to verify footnote extraction results.

Three verification layers, each a focused LLM call:
1. Section Boundary Check — did we find the right section?
2. Footnote Completeness Check — did we get ALL footnotes?
3. Footnote Accuracy Check — is each footnote text correct?

Uses Google Gemini 2.0 Flash (fast, cheap, good at structured tasks).
API key must be set via GEMINI_API_KEY environment variable.
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from google import genai

from qa_reviewer.evidence_extractor import SectionEvidence
from qa_reviewer.tracing import get_langfuse, flush_langfuse, create_trace, create_generation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Gemini 2.0 Flash — fast, cheap, strong at structured verification tasks
MODEL_ID = "gemini-2.5-flash"

# Delay between API calls to respect rate limits.
# Gemini 2.5 Flash free tier: 1,000 RPM. 0.2s = max 300 RPM, well within limits.
CALL_DELAY_SECONDS = 0.2


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------

@dataclass
class LayerVerdict:
    """Result of one verification layer."""
    passed: bool = True
    reasoning: str = ""
    details: Dict = field(default_factory=dict)


@dataclass
class QAVerdict:
    """Full QA verdict for one schedule date sub-section."""
    cik: str = ""
    file_code: str = ""
    as_at_date: str = ""
    section_check: LayerVerdict = field(default_factory=LayerVerdict)
    completeness_check: LayerVerdict = field(default_factory=LayerVerdict)
    accuracy_check: LayerVerdict = field(default_factory=LayerVerdict)
    overall_pass: bool = True
    error: str = ""

    def to_dict(self) -> Dict:
        return {
            "cik": self.cik,
            "file_code": self.file_code,
            "as_at_date": self.as_at_date,
            "overall_pass": self.overall_pass,
            "section_check": {
                "pass": self.section_check.passed,
                "reasoning": self.section_check.reasoning,
            },
            "completeness_check": {
                "pass": self.completeness_check.passed,
                "reasoning": self.completeness_check.reasoning,
                **self.completeness_check.details,
            },
            "accuracy_check": {
                "pass": self.accuracy_check.passed,
                "reasoning": self.accuracy_check.reasoning,
                **self.accuracy_check.details,
            },
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

def _get_client() -> genai.Client:
    """
    Create a Gemini client using the API key from environment.
    The key must be set as GEMINI_API_KEY.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY environment variable is not set. "
            "Get your key at https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def _call_gemini(
    client: genai.Client,
    prompt: str,
    trace=None,
    span_name: str = "gemini_call",
    metadata: dict = None,
) -> str:
    """
    Make a single Gemini API call and return the text response.
    Includes rate-limit delay and Langfuse tracing.
    
    If a Langfuse trace is provided, logs this call as a 'generation' span
    so you can see the exact prompt/response in your Langfuse dashboard.
    """
    time.sleep(CALL_DELAY_SECONDS)

    # Start a Langfuse generation span (if tracing is active).
    # In Langfuse v4, we use create_generation() helper which calls start_observation()
    generation = None
    if trace is not None:
        generation = create_generation(
            name=span_name,
            model=MODEL_ID,
            input_data=prompt,
            metadata=metadata or {},
        )

    # Retry logic for transient failures with thread-based timeout
    import concurrent.futures
    max_retries = 3
    timeout_seconds = 180  # 3 minute timeout per attempt
    last_error = None
    
    def make_api_call():
        """Make the actual API call (runs in a separate thread for timeout)."""
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
            config={
                "temperature": 0.0,
                "max_output_tokens": 8192,
            },
        )
        return response.text.strip()
    
    for attempt in range(max_retries):
        try:
            # Use ThreadPoolExecutor to enforce timeout on the API call
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(make_api_call)
                output_text = future.result(timeout=timeout_seconds)
            
            # Log the output to Langfuse
            if generation is not None:
                try:
                    generation.update(output=output_text)
                    generation.end()
                except Exception:
                    pass

            return output_text
        except concurrent.futures.TimeoutError:
            last_error = TimeoutError(f"API call timed out after {timeout_seconds}s")
            logger.warning(f"Gemini API timeout (attempt {attempt+1}/{max_retries}), retrying...")
            time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
            continue
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            if "timed out" in error_str or "timeout" in error_str or "reset" in error_str:
                logger.warning(f"Gemini API error (attempt {attempt+1}/{max_retries}): {e}, retrying...")
                time.sleep(2 ** attempt)
                continue
            else:
                # Non-retryable error
                break
    
    # All retries failed
    if generation is not None:
        try:
            generation.update(output=f"ERROR: {str(last_error)}", level="ERROR")
            generation.end()
        except Exception:
            pass
    logger.error(f"Gemini API error: {last_error}")
    raise last_error


def _parse_json_response(text: str) -> Dict:
    """
    Parse a JSON response from Gemini, handling markdown code fences.
    
    Gemini 2.5 Flash sometimes wraps JSON in ```json ... ``` blocks,
    or includes thinking tokens before/after the JSON. We try multiple
    strategies to extract the JSON object.
    """
    cleaned = text.strip()
    
    # Strategy 1: Strip markdown code fences
    if "```" in cleaned:
        # Find content between first ``` and last ```
        parts = cleaned.split("```")
        for part in parts:
            # Skip empty parts and the language identifier line
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
    
    # Strategy 3: Find the JSON object boundaries { ... }
    # This handles cases where there's text before/after the JSON
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(cleaned[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
    
    # Strategy 4: Try to extract partial JSON for completeness/accuracy checks
    # Even if full JSON fails, try to extract the missing_markers array
    # This is critical for the fixer to work
    result = _extract_partial_json(cleaned)
    if result:
        return result
    
    logger.warning(f"Failed to parse JSON from Gemini response: {text[:200]}")
    return {"error": "Failed to parse response", "raw": text[:500]}


def _extract_partial_json(text: str) -> Optional[Dict]:
    """
    Extract partial JSON when full parsing fails.
    
    Specifically looks for completeness and accuracy sub-objects,
    and extracts missing_markers and issues arrays even from
    malformed JSON. This ensures the fixer gets the data it needs.
    """
    import re
    
    result = {}
    
    # Try to extract completeness block
    comp_match = re.search(
        r'"completeness"\s*:\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}',
        text, re.DOTALL
    )
    if comp_match:
        comp_text = "{" + comp_match.group(1) + "}"
        try:
            # Try to parse the completeness block
            comp_obj = json.loads(comp_text)
            result["completeness"] = comp_obj
        except json.JSONDecodeError:
            # Extract individual fields
            pass_match = re.search(r'"pass"\s*:\s*(true|false)', comp_match.group(1), re.IGNORECASE)
            reasoning_match = re.search(r'"reasoning"\s*:\s*"([^"]*)"', comp_match.group(1))
            missing_match = re.search(r'"missing_markers"\s*:\s*\[([^\]]*)\]', comp_match.group(1))
            
            result["completeness"] = {
                "pass": pass_match.group(1).lower() == "true" if pass_match else False,
                "reasoning": reasoning_match.group(1) if reasoning_match else "",
                "missing_markers": _parse_marker_list(missing_match.group(1)) if missing_match else [],
            }
    
    # Try to extract accuracy block
    acc_match = re.search(
        r'"accuracy"\s*:\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}',
        text, re.DOTALL
    )
    if acc_match:
        acc_text = "{" + acc_match.group(1) + "}"
        try:
            acc_obj = json.loads(acc_text)
            result["accuracy"] = acc_obj
        except json.JSONDecodeError:
            pass_match = re.search(r'"pass"\s*:\s*(true|false)', acc_match.group(1), re.IGNORECASE)
            reasoning_match = re.search(r'"reasoning"\s*:\s*"([^"]*)"', acc_match.group(1))
            
            result["accuracy"] = {
                "pass": pass_match.group(1).lower() == "true" if pass_match else False,
                "reasoning": reasoning_match.group(1) if reasoning_match else "",
                "issues": [],  # Issues are complex, skip if we can't parse
            }
    
    return result if result else None


def _parse_marker_list(text: str) -> List[str]:
    """
    Parse a list of markers from a JSON array string.
    Handles both quoted strings and numbers.
    """
    import re
    markers = []
    # Match quoted strings or numbers
    for match in re.finditer(r'"([^"]+)"|(\d+)', text):
        if match.group(1):
            markers.append(match.group(1))
        elif match.group(2):
            markers.append(match.group(2))
    return markers


# ---------------------------------------------------------------------------
# Verification Layer 1: Section Boundary Check
# ---------------------------------------------------------------------------

def check_section_boundary(
    client: genai.Client,
    evidence: SectionEvidence,
    trace=None,
) -> LayerVerdict:
    """
    Verify that we identified the correct Consolidated Schedule of Investments section.

    Sends the first ~500 chars and last ~500 chars of our identified section
    to Gemini, asking if this looks like the right section.
    """
    prompt = f"""You are a financial document QA analyst. Your job is to verify that a text extraction system correctly identified the "Consolidated Schedule of Investments" section in an SEC filing.

Below is the START of the section we identified (first ~500 characters):
---START---
{evidence.section_start_text}
---END---

Below is the END of the section we identified (last ~500 characters):
---START---
{evidence.section_end_text}
---END---

TASK: Determine if this text is from the Consolidated Schedule of Investments section.

The START should contain headers like "Consolidated Schedule of Investments", a date ("As of..."), and column headers for investment data (Company, Investment, Maturity, Cost, Fair Value, etc.).

The END should contain either:
- "See accompanying notes to the consolidated financial statements"
- "The accompanying notes are an integral part of these consolidated financial statements"
- Or the last footnote definitions before such a marker

Respond with ONLY this JSON (no other text):
{{"pass": true/false, "reasoning": "brief explanation"}}"""

    try:
        response_text = _call_gemini(
            client, prompt,
            trace=trace,
            span_name="layer1_section_boundary",
            metadata={"layer": "section_boundary"},
        )
        result = _parse_json_response(response_text)
        return LayerVerdict(
            passed=result.get("pass", False),
            reasoning=result.get("reasoning", ""),
        )
    except Exception as e:
        return LayerVerdict(passed=False, reasoning=f"API error: {str(e)}")


# ---------------------------------------------------------------------------
# Verification Layer 2+3 (merged): Completeness + Accuracy in one call
# ---------------------------------------------------------------------------

def check_completeness_and_accuracy(
    client: genai.Client,
    evidence: SectionEvidence,
    trace=None,
) -> tuple:
    """
    Combined completeness + accuracy check in a SINGLE LLM call.
    
    Why merged: Both checks need the same footnote_area_text (~15K chars).
    Sending it once instead of twice saves ~3,750 tokens per QA review.
    
    Returns (completeness_verdict, accuracy_verdict) as a tuple of LayerVerdicts.
    """
    # Format extracted footnotes for accuracy check
    footnotes_formatted = "\n".join(
        f"({marker}): {text[:300]}"
        for marker, text in evidence.extracted_footnotes.items()
    )

    prompt = f"""You are a financial document QA analyst. Perform TWO checks on the extracted footnotes from a Consolidated Schedule of Investments.

Below is the raw text of the footnote definition area:
---FOOTNOTE AREA---
{evidence.footnote_area_text}
---END---

Our extraction system found these markers: {evidence.extracted_markers}

Here are the extracted footnotes:
---EXTRACTED---
{footnotes_formatted}
---END---

TASK 1 — COMPLETENESS: Identify ALL footnote definition markers in the text (e.g., (1), (A), *, **). Compare to our extracted list. Report missing or extra markers. Only count DEFINITION markers (followed by explanatory text), NOT inline references.

TASK 2 — ACCURACY: For each extracted footnote, check:
- Is the text truncated (cut off before the definition ends)?
- Is the text contaminated with non-footnote content (table data, page headers)?
- Is the text attributed to the wrong marker?
- Are numeric values missing (dropped XBRL tags)?

Respond with ONLY this JSON (no other text):
{{
  "completeness": {{
    "pass": true/false,
    "reasoning": "brief explanation",
    "missing_markers": [],
    "extra_markers": [],
    "markers_found_in_text": []
  }},
  "accuracy": {{
    "pass": true/false,
    "reasoning": "brief summary",
    "issues": [{{"marker": "X", "issue_type": "truncated|wrong_text|contaminated|wrong_marker", "detail": "..."}}]
  }}
}}"""

    try:
        response_text = _call_gemini(
            client, prompt,
            trace=trace,
            span_name="layer2_3_completeness_accuracy",
            metadata={
                "layer": "completeness+accuracy",
                "num_extracted": len(evidence.extracted_markers),
                "num_footnotes": len(evidence.extracted_footnotes),
            },
        )
        result = _parse_json_response(response_text)
        
        # Parse completeness sub-result
        comp = result.get("completeness", {})
        completeness_verdict = LayerVerdict(
            passed=comp.get("pass", False),
            reasoning=comp.get("reasoning", ""),
            details={
                "missing_markers": comp.get("missing_markers", []),
                "extra_markers": comp.get("extra_markers", []),
                "markers_found_in_text": comp.get("markers_found_in_text", []),
            },
        )
        
        # Parse accuracy sub-result
        acc = result.get("accuracy", {})
        accuracy_verdict = LayerVerdict(
            passed=acc.get("pass", False),
            reasoning=acc.get("reasoning", ""),
            details={"issues": acc.get("issues", [])},
        )
        
        return completeness_verdict, accuracy_verdict
        
    except Exception as e:
        err_verdict = LayerVerdict(passed=False, reasoning=f"API error: {str(e)}")
        return err_verdict, err_verdict


# ---------------------------------------------------------------------------
# Optimization 3: Targeted re-verify for only fixed markers
# ---------------------------------------------------------------------------

def review_fixed_markers(
    evidence: SectionEvidence,
    fixed_markers: List[str],
    cik: str,
    file_code: str,
    as_at_date: str,
) -> LayerVerdict:
    """
    Lightweight re-verify that checks ONLY the markers the fixer touched.
    
    Instead of re-running the full 3-layer QA (~3 calls), this sends a
    single focused call with just the before/after for fixed markers.
    Much cheaper: ~500 tokens instead of ~10K tokens.
    
    Returns a LayerVerdict for the accuracy of the fixed markers only.
    """
    # Build a small payload with just the fixed footnotes
    fixed_footnotes = {
        m: evidence.extracted_footnotes[m]
        for m in fixed_markers
        if m in evidence.extracted_footnotes
    }
    
    if not fixed_footnotes:
        return LayerVerdict(passed=True, reasoning="No fixed markers to verify")
    
    footnotes_formatted = "\n".join(
        f"({marker}): {text[:500]}"
        for marker, text in fixed_footnotes.items()
    )
    
    prompt = f"""You are a financial document QA analyst. Verify that these SPECIFIC corrected footnotes are now accurate.

Raw footnote area text:
---FOOTNOTE AREA---
{evidence.footnote_area_text}
---END---

Corrected footnotes to verify:
---EXTRACTED---
{footnotes_formatted}
---END---

For each footnote, check: Is the text complete (not truncated)? Is it clean (no page headers or table data contamination)? Are numeric values present?

Respond with ONLY this JSON:
{{"pass": true/false, "reasoning": "brief summary", "issues": [{{"marker": "X", "issue_type": "truncated|wrong_text|contaminated", "detail": "..."}}]}}"""

    try:
        client = _get_client()
        
        # Create trace using v4 API helper
        trace = create_trace(
            name="reqa_fixed_markers",
            input_data={
                "cik": cik,
                "file_code": file_code,
                "as_at_date": as_at_date,
                "fixed_markers": fixed_markers,
                "corrected_footnotes": {
                    m: text[:300] for m, text in fixed_footnotes.items()
                },
                "footnote_area_length": len(evidence.footnote_area_text),
            },
            metadata={"cik": cik, "file_code": file_code, "fixed_markers": fixed_markers},
        )
        
        response_text = _call_gemini(
            client, prompt,
            trace=trace,
            span_name="verify_fixed_markers",
            metadata={"num_fixed": len(fixed_markers)},
        )
        result = _parse_json_response(response_text)
        
        verdict = LayerVerdict(
            passed=result.get("pass", False),
            reasoning=result.get("reasoning", ""),
            details={"issues": result.get("issues", [])},
        )
        
        if trace is not None:
            try:
                trace.update(output=result)
                trace.end()
            except Exception:
                pass
        
        return verdict
        
    except Exception as e:
        return LayerVerdict(passed=False, reasoning=f"API error: {str(e)}")


# ---------------------------------------------------------------------------
# Full review orchestration
# ---------------------------------------------------------------------------

def review_extraction(
    evidence: SectionEvidence,
    cik: str,
    file_code: str,
    as_at_date: str,
    skip_layer1: bool = False,
) -> QAVerdict:
    """
    Run all 3 verification layers on one extraction result.

    Returns a QAVerdict with pass/fail for each layer and an overall verdict.
    If any layer fails, overall_pass = False.
    """
    verdict = QAVerdict(cik=cik, file_code=file_code, as_at_date=as_at_date)

    # Create a Langfuse parent span for this filing section using v4 API.
    trace = create_trace(
        name="qa_review",
        input_data={
            "cik": cik,
            "file_code": file_code,
            "as_at_date": as_at_date,
            "html_path": evidence.html_path,
            "num_extracted_markers": len(evidence.extracted_markers),
            "extracted_markers": evidence.extracted_markers,
            "section_start_preview": evidence.section_start_text[:200],
            "section_end_preview": evidence.section_end_text[:200],
            "footnote_area_length": len(evidence.footnote_area_text),
        },
        metadata={
            "cik": cik,
            "file_code": file_code,
            "as_at_date": as_at_date,
            "html_path": evidence.html_path,
        },
    )

    try:
        client = _get_client()

        # Layer 1: Section boundary (skip if requested - saves 1 LLM call)
        if skip_layer1:
            logger.info(f"  QA Layer 1: SKIPPED (assumed correct)")
            verdict.section_check = LayerVerdict(passed=True, reasoning="Skipped - assumed correct")
        else:
            logger.info(f"  QA Layer 1: Section boundary check...")
            verdict.section_check = check_section_boundary(client, evidence, trace=trace)
            logger.info(f"    -> {'PASS' if verdict.section_check.passed else 'FAIL'}: {verdict.section_check.reasoning[:80]}")

        # Layer 2+3 (merged): Completeness + Accuracy in one LLM call.
        # Sends footnote_area_text once instead of twice — saves ~3,750 tokens.
        logger.info(f"  QA Layer 2+3: Completeness + Accuracy check (merged)...")
        verdict.completeness_check, verdict.accuracy_check = check_completeness_and_accuracy(
            client, evidence, trace=trace
        )
        logger.info(f"    -> Completeness: {'PASS' if verdict.completeness_check.passed else 'FAIL'}: {verdict.completeness_check.reasoning[:80]}")
        logger.info(f"    -> Accuracy:     {'PASS' if verdict.accuracy_check.passed else 'FAIL'}: {verdict.accuracy_check.reasoning[:80]}")

        # Overall verdict
        verdict.overall_pass = (
            verdict.section_check.passed
            and verdict.completeness_check.passed
            and verdict.accuracy_check.passed
        )

        # Close the parent span with the final verdict and scores
        if trace is not None:
            try:
                # Calculate scores for Langfuse tracking
                # Completeness score (HIGH priority): Did we get all footnotes?
                completeness_score = 1.0 if verdict.completeness_check.passed else 0.0
                missing_count = len(verdict.completeness_check.details.get("missing_markers", []))
                if missing_count > 0:
                    # Penalize based on how many we missed
                    completeness_score = max(0.0, 1.0 - (missing_count * 0.2))
                
                # Truncation score (MEDIUM priority): Are footnotes complete or truncated?
                truncation_issues = [
                    issue for issue in verdict.accuracy_check.details.get("issues", [])
                    if issue.get("issue_type") == "truncated"
                ]
                truncation_score = 1.0 if len(truncation_issues) == 0 else 0.0
                if len(truncation_issues) > 0:
                    # Penalize based on how many are truncated
                    truncation_score = max(0.0, 1.0 - (len(truncation_issues) * 0.1))
                
                # Overall score: weighted average (completeness 70%, truncation 30%)
                overall_score = (completeness_score * 0.7) + (truncation_score * 0.3)
                
                trace.update(
                    output=verdict.to_dict(),
                    metadata={
                        "cik": cik,
                        "file_code": file_code,
                        "as_at_date": as_at_date,
                        "html_path": evidence.html_path,
                        "overall_pass": verdict.overall_pass,
                        "missing_markers_count": missing_count,
                        "truncation_issues_count": len(truncation_issues),
                    },
                )
                
                # Add scores to the trace
                trace.score(
                    name="completeness",
                    value=completeness_score,
                    comment=f"Missing {missing_count} markers" if missing_count > 0 else "All markers found"
                )
                trace.score(
                    name="truncation",
                    value=truncation_score,
                    comment=f"{len(truncation_issues)} truncated footnotes" if len(truncation_issues) > 0 else "No truncation"
                )
                trace.score(
                    name="overall_quality",
                    value=overall_score,
                    comment=f"Weighted: completeness 70%, truncation 30%"
                )
                
                trace.end()
            except Exception:
                pass  # Tracing is optional

    except ValueError as e:
        # Missing API key
        verdict.error = str(e)
        verdict.overall_pass = False
        logger.error(f"  QA Error: {e}")
    except Exception as e:
        verdict.error = str(e)
        verdict.overall_pass = False
        logger.error(f"  QA Error: {e}", exc_info=True)

    return verdict
