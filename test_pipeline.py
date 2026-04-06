"""
Test Pipeline — runs extraction + QA on 3 sample filings and builds golden dataset.

This script:
1. Loads API keys from a .env file
2. Runs the deterministic extractor on 3 diverse filings
3. Runs the Gemini QA reviewer on each result
4. Saves the extraction results + QA verdicts for manual review
5. Generates a golden_dataset.json you can curate as ground truth

Usage:
    source venv/bin/activate && python test_pipeline.py

Required env vars (set in .env file):
    GEMINI_API_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
"""

import os
import json
import logging
from datetime import datetime

# Load .env file if it exists (so you don't have to export manually)
from pathlib import Path
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            # Strip surrounding quotes (single or double) from the value
            value = value.strip().strip("'\"")
            os.environ.setdefault(key.strip(), value)

from models import FilingMetadata, FootnoteResult
from parsers.html_parser import load_html, find_schedule_sections
from parsers.footnote_extractor import (
    extract_footnotes_from_section,
    detect_marker_type,
    check_first_marker_valid,
)
from qa_reviewer.evidence_extractor import extract_evidence
from qa_reviewer.reviewer import review_extraction, review_fixed_markers
from qa_reviewer.self_correct import re_extract_footnotes
from qa_reviewer.fixer import fix_issues, FixRequest
from qa_reviewer.tracing import get_langfuse, flush_langfuse


# ---------------------------------------------------------------------------
# The 3 test filings — diverse: BlackRock 10-K, Blue Owl 10-K, Blue Owl 10-Q
# ---------------------------------------------------------------------------

TEST_FILINGS = [
    FilingMetadata(
        cik="0001370755",
        filing_type="10-K",
        accession="0000950170-23-004871",
        html_path="BDC Footnotes/0001370755/10-K/0000950170-23-004871/primary-document.html",
    ),
    FilingMetadata(
        cik="0001655888",
        filing_type="10-K",
        accession="0000950170-22-001814",
        html_path="BDC Footnotes/0001655888/10-K/0000950170-22-001814/primary-document.html",
    ),
    FilingMetadata(
        cik="0001655888",
        filing_type="10-Q",
        accession="0000950170-21-000801",
        html_path="BDC Footnotes/0001655888/10-Q/0000950170-21-000801/primary-document.html",
    ),
]


def setup_logging():
    """Configure logging to console + file."""
    os.makedirs("output/logs", exist_ok=True)
    log_file = f"output/logs/test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


def main():
    logger = setup_logging()

    # Check API keys
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY not set. Create a .env file (see .env.example)")
        return

    logger.info("=" * 60)
    logger.info("TEST PIPELINE — 3 sample filings")
    logger.info("=" * 60)

    # This will hold the golden dataset entries
    golden_dataset = []
    all_verdicts = []

    for i, filing in enumerate(TEST_FILINGS):
        logger.info(f"\n{'='*60}")
        logger.info(f"FILE {i+1}/3: CIK={filing.cik} type={filing.filing_type} acc={filing.accession}")
        logger.info(f"{'='*60}")

        # Step 1: Load HTML and find sections
        soup = load_html(filing.html_path)
        sections = find_schedule_sections(soup)
        logger.info(f"Found {len(sections)} schedule sections")

        for j, (start_elem, end_elem, as_at_date) in enumerate(sections):
            logger.info(f"\n--- Section {j+1}: {as_at_date} ---")

            # Step 2: Extract footnotes deterministically
            footnotes = extract_footnotes_from_section(start_elem, end_elem, soup)
            m_type = detect_marker_type(footnotes)
            first_valid = check_first_marker_valid(footnotes)
            logger.info(f"Extracted {len(footnotes)} footnotes: markers={list(footnotes.keys())}")
            logger.info(f"  marker_type={m_type}, first_marker_valid={first_valid}")
            if not first_valid:
                logger.warning(f"  ⚠ First marker is '{list(footnotes.keys())[0] if footnotes else '?'}' — machine may have missed earlier footnotes")

            # Step 3: Build evidence for QA (include html_path for traceability)
            evidence = extract_evidence(
                start_elem, end_elem, soup, footnotes,
                html_path=filing.html_path,
            )

            # Step 4: Run QA review via Gemini (traced in Langfuse)
            verdict = review_extraction(
                evidence=evidence,
                cik=filing.cik,
                file_code=filing.accession,
                as_at_date=as_at_date,
            )
            all_verdicts.append(verdict)

            overall = "✅ PASS" if verdict.overall_pass else "❌ FAIL"
            logger.info(f"QA Verdict: {overall}")
            logger.info(f"  Section:      {'PASS' if verdict.section_check.passed else 'FAIL'} — {verdict.section_check.reasoning[:100]}")
            logger.info(f"  Completeness: {'PASS' if verdict.completeness_check.passed else 'FAIL'} — {verdict.completeness_check.reasoning[:100]}")
            logger.info(f"  Accuracy:     {'PASS' if verdict.accuracy_check.passed else 'FAIL'} — {verdict.accuracy_check.reasoning[:100]}")

            # Step 5: Fixer agent — targeted repairs for non-truncated issues
            fixer_result = None
            fixed_footnotes = footnotes
            extraction_method = "deterministic"
            reqa_verdict = None

            if not verdict.overall_pass:
                logger.info("  Running fixer agent on flagged issues...")
                lf = get_langfuse()

                # Build fix request first so we can log it as span input
                fix_request = FixRequest(
                    cik=filing.cik,
                    file_code=filing.accession,
                    as_at_date=as_at_date,
                    html_path=filing.html_path,
                    issues=verdict.accuracy_check.details.get("issues", []),
                    missing_markers=verdict.completeness_check.details.get("missing_markers", []),
                    extracted_footnotes=dict(footnotes),
                    footnote_area_text=evidence.footnote_area_text,
                )

                fixer_trace = None
                if lf is not None:
                    fixer_trace = lf.start_span(
                        name="fixer_agent",
                        input={
                            "cik": filing.cik,
                            "file_code": filing.accession,
                            "as_at_date": as_at_date,
                            "html_path": filing.html_path,
                            "num_issues": len(fix_request.issues),
                            "issues": fix_request.issues,
                            "missing_markers": fix_request.missing_markers,
                            "num_extracted_footnotes": len(fix_request.extracted_footnotes),
                            "extracted_markers": list(fix_request.extracted_footnotes.keys()),
                        },
                        metadata={"cik": filing.cik, "accession": filing.accession, "as_at_date": as_at_date},
                    )

                fixer_result = fix_issues(fix_request, trace=fixer_trace)

                if fixer_trace is not None:
                    fixer_trace.score(name="fix_rate", value=fixer_result.fix_rate,
                                     comment=f"{len(fixer_result.fixes_applied)} fixes")
                    fixer_trace.update(output=fixer_result.to_dict())
                    fixer_trace.end()

                if fixer_result.fixes_applied:
                    fixed_footnotes = fixer_result.corrected_footnotes
                    extraction_method = "deterministic+fixer"
                    fixed_markers = [f.marker for f in fixer_result.fixes_applied]
                    logger.info(f"  Fixer applied {len(fixer_result.fixes_applied)} fixes — re-verifying {fixed_markers}...")

                    # Targeted re-verify: only check the markers the fixer touched.
                    # 1 LLM call instead of full 3-layer re-QA (2 calls).
                    reqa_evidence = extract_evidence(
                        start_elem, end_elem, soup, fixed_footnotes,
                        html_path=filing.html_path,
                    )
                    reqa_verdict = review_fixed_markers(
                        evidence=reqa_evidence,
                        fixed_markers=fixed_markers,
                        cik=filing.cik,
                        file_code=filing.accession,
                        as_at_date=as_at_date,
                    )
                    reqa_status = "✅ PASS" if reqa_verdict.passed else "❌ STILL FAILING"
                    logger.info(f"  Re-verify fixed markers: {reqa_status}")
                    remaining_issues = len(reqa_verdict.details.get("issues", []))
                    logger.info(f"  Remaining issues on fixed markers: {remaining_issues}")

            # Step 6: Build golden dataset entry with before/after comparison
            golden_entry = {
                "cik": filing.cik,
                "filing_type": filing.filing_type,
                "accession": filing.accession,
                "html_path": filing.html_path,
                "as_at_date": as_at_date,
                "extraction_method": extraction_method,
                "marker_type": m_type,
                "first_marker_valid": first_valid,
                "num_footnotes": len(fixed_footnotes),
                "markers": list(fixed_footnotes.keys()),
                "footnotes": {k: v for k, v in fixed_footnotes.items()},
                "qa_verdict_before_fixer": {
                    "overall_pass": verdict.overall_pass,
                    "section_pass": verdict.section_check.passed,
                    "completeness_pass": verdict.completeness_check.passed,
                    "accuracy_pass": verdict.accuracy_check.passed,
                    "completeness_details": verdict.completeness_check.details,
                    "accuracy_details": verdict.accuracy_check.details,
                },
                "fixer": {
                    "applied": fixer_result is not None and len(fixer_result.fixes_applied) > 0,
                    "num_fixes": len(fixer_result.fixes_applied) if fixer_result else 0,
                    "fix_rate": fixer_result.fix_rate if fixer_result else 0.0,
                    "fixes": [{"marker": f.marker, "fix_type": f.fix_type, "description": f.description}
                              for f in fixer_result.fixes_applied] if fixer_result else [],
                    "num_skipped_truncated": len(fixer_result.skipped_issues) if fixer_result else 0,
                },
                "qa_verdict_after_fixer": {
                    "fixed_markers_pass": reqa_verdict.passed if reqa_verdict else None,
                    "reasoning": reqa_verdict.reasoning if reqa_verdict else None,
                    "remaining_issues": reqa_verdict.details.get("issues", []) if reqa_verdict else None,
                } if reqa_verdict else None,
                "human_verified": False,
                "human_notes": "",
            }
            golden_dataset.append(golden_entry)

    # File-level Langfuse scoring: date_coverage per filing
    # Group golden dataset entries by accession to count distinct dates
    from collections import defaultdict
    lf = get_langfuse()
    if lf is not None:
        dates_by_filing = defaultdict(set)
        for entry in golden_dataset:
            dates_by_filing[entry["accession"]].add(entry["as_at_date"])
        
        for accession, dates in dates_by_filing.items():
            # Filter out error/unknown dates
            valid_dates = {d for d in dates if d not in ("NOT_FOUND", "ERROR")}
            num_dates = len(valid_dates)
            date_coverage_score = 1.0 if num_dates >= 2 else 0.0
            
            # Find the CIK for this filing (for metadata)
            cik = next((e["cik"] for e in golden_dataset if e["accession"] == accession), "unknown")
            
            file_span = lf.start_span(
                name="filing_processing",
                input={
                    "file_code": accession,
                    "cik": cik,
                    "as_at_dates": sorted(valid_dates),
                    "num_distinct_dates": num_dates,
                },
                metadata={"file_code": accession, "cik": cik},
            )
            file_span.update(output={
                "num_distinct_dates": num_dates,
                "as_at_dates": sorted(valid_dates),
                "date_coverage_pass": date_coverage_score == 1.0,
            })
            file_span.score(
                name="date_coverage",
                value=date_coverage_score,
                comment=f"{num_dates} distinct dates (expected ≥2)"
            )
            file_span.end()
            logger.info(f"  Filing {accession}: {num_dates} distinct dates → date_coverage={'PASS' if date_coverage_score == 1.0 else 'FAIL'}")

    # Save golden dataset
    os.makedirs("output", exist_ok=True)
    golden_path = "output/golden_dataset.json"
    with open(golden_path, "w") as f:
        json.dump(golden_dataset, f, indent=2, ensure_ascii=False)
    logger.info(f"\nGolden dataset saved to {golden_path}")
    logger.info(f"  {len(golden_dataset)} entries — review and set 'human_verified': true")

    # Save QA verdicts
    verdicts_path = "output/test_qa_verdicts.json"
    with open(verdicts_path, "w") as f:
        json.dump([v.to_dict() for v in all_verdicts], f, indent=2)
    logger.info(f"QA verdicts saved to {verdicts_path}")

    # Summary
    passed = sum(1 for v in all_verdicts if v.overall_pass)
    failed = len(all_verdicts) - passed
    logger.info(f"\n{'='*60}")
    logger.info(f"SUMMARY: {passed} PASSED, {failed} FAILED out of {len(all_verdicts)} sections")
    logger.info(f"{'='*60}")

    # Flush Langfuse
    flush_langfuse()
    logger.info("Done. Check Langfuse dashboard for traces.")


if __name__ == "__main__":
    main()
