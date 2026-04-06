"""
BDC Footnote Extraction Pipeline — Main Entry Point

This script orchestrates the full extraction workflow:
1. Discover all SEC filing HTML files on disk
2. For each filing, locate the "Consolidated Schedule of Investments" section(s)
3. Extract footnote definitions from within each section
4. Assemble results into a structured CSV dataset
5. (Optional) Run LLM-based QA review via --qa flag

Output schema: cik | file_code | as_at_date | footnote_1 | footnote_2 | ... | footnote_N

Usage:
    source venv/bin/activate && python extract_footnotes.py        # Extract only
    source venv/bin/activate && python extract_footnotes.py --qa   # Extract + QA review
"""

import os
import argparse
import logging
from datetime import datetime
from typing import List, Tuple, Optional
# concurrent.futures removed — sequential processing with text-node-index
# optimization is fast enough (100x+ speedup per file)

import pandas as pd
from bs4 import BeautifulSoup

from models import FilingMetadata, FootnoteResult
from parsers.html_parser import discover_filings, load_html, find_schedule_sections
from parsers.footnote_extractor import (
    extract_footnotes_from_section,
    build_text_node_index,
    detect_marker_type,
    check_first_marker_valid,
)
from qa_reviewer.evidence_extractor import extract_evidence, SectionEvidence
from qa_reviewer.reviewer import review_extraction, review_fixed_markers, QAVerdict
from qa_reviewer.self_correct import re_extract_footnotes
from qa_reviewer.fixer import fix_issues, FixRequest
from qa_reviewer.report import generate_report
from qa_reviewer.tracing import flush_langfuse


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path to the downloaded filings
# Structure: FILINGS_BASE_PATH/{CIK}/{FILING_TYPE}/{ACCESSION}/primary-document.html
FILINGS_BASE_PATH = os.path.join("BDC Footnotes")

# Where to save the output CSV
OUTPUT_DIR = "output"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "bdc_footnotes.csv")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    """
    Configure logging to both file and console.
    
    Logs go to:
    - Console (INFO level) — so you can watch progress
    - File (DEBUG level) — for detailed troubleshooting
    """
    log_dir = os.path.join(OUTPUT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f"extraction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    
    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

# Type alias: section info tuple = (start_elem, end_elem, soup) per result
SectionInfo = Tuple  # (start_elem, end_elem, soup)


def process_filing(
    filing: FilingMetadata,
    _logger=None,
    keep_section_info: bool = False,
) -> Tuple[List[FootnoteResult], List[Optional[SectionInfo]]]:
    """
    Process a single filing: load HTML, find schedule sections, extract footnotes.
    
    Returns:
        - List of FootnoteResult — one per date sub-section found.
        - List of section info tuples (start, end, soup) — only populated when
          keep_section_info=True (needed for QA review). Otherwise empty tuples.
    
    A 10-K may return 2 results (current + prior year), a 10-Q typically 1.
    
    _logger is ignored — we create our own so the function is picklable
    for ProcessPoolExecutor (Logger objects can't cross process boundaries).
    """
    logger = logging.getLogger(__name__)
    results = []
    section_infos = []  # Parallel list: one entry per result
    
    logger.info(f"Processing: CIK={filing.cik} type={filing.filing_type} acc={filing.accession}")
    
    try:
        # Load and parse the HTML
        soup = load_html(filing.html_path)
        
        # Find schedule-of-investments section boundaries and dates
        sections = find_schedule_sections(soup)
        
        if not sections:
            logger.warning(f"  No schedule sections found in {filing.accession}")
            results.append(FootnoteResult(
                cik=filing.cik,
                file_code=filing.accession,
                as_at_date="NOT_FOUND",
            ))
            section_infos.append(None)
            return results, section_infos
        
        # Pre-build text node index ONCE for the whole document.
        # This turns each section's text-node collection from O(n) DOM walk
        # into O(1) dict lookup + list slice — huge win for 60 MB files.
        all_nodes, id_to_pos = build_text_node_index(soup)

        # Extract footnotes from each date sub-section
        for start_elem, end_elem, as_at_date in sections:
            footnotes = extract_footnotes_from_section(
                start_elem, end_elem, soup, all_nodes, id_to_pos
            )
            
            result = FootnoteResult(
                cik=filing.cik,
                file_code=filing.accession,
                as_at_date=as_at_date,
                html_path=filing.html_path,
                footnotes=footnotes,
                marker_type=detect_marker_type(footnotes),
                first_marker_valid=check_first_marker_valid(footnotes),
            )
            results.append(result)
            
            # Keep section boundaries if QA will need them later
            if keep_section_info:
                section_infos.append((start_elem, end_elem, soup))
            else:
                section_infos.append(None)
            
            logger.info(f"  Date '{as_at_date}': {len(footnotes)} footnotes extracted")
    
    except Exception as e:
        logger.error(f"  ERROR processing {filing.accession}: {str(e)}", exc_info=True)
        results.append(FootnoteResult(
            cik=filing.cik,
            file_code=filing.accession,
            as_at_date="ERROR",
        ))
        section_infos.append(None)
    
    return results, section_infos


def results_to_dataframe(all_results: List[FootnoteResult]) -> pd.DataFrame:
    """
    Convert a list of FootnoteResult objects into a pandas DataFrame.
    
    The tricky part: different filings have different numbers of footnotes.
    We dynamically create columns footnote_1, footnote_2, ..., footnote_N
    where N is the maximum number of footnotes across all results.
    
    Each footnote column header uses the original marker (e.g., "1", "A")
    but the column name is standardized as footnote_1, footnote_2, etc.
    """
    rows = []
    
    for result in all_results:
        row = {
            "cik": result.cik,
            "file_code": result.file_code,
            "as_at_date": result.as_at_date,
            "html_path": result.html_path,
            "extraction_method": result.extraction_method,
            "marker_type": result.marker_type,
            "first_marker_valid": result.first_marker_valid,
            "num_footnotes": len(result.footnotes),
            # QA tracking flags — tells you which QA path this section took
            "qa_reviewed": result.qa_reviewed,
            "qa_passed_first": result.qa_passed_first,
            "fixer_applied": result.fixer_applied,
            "qa_reviewed_twice": result.qa_reviewed_twice,
            "qa_passed_final": result.qa_passed_final,
        }
        
        # Add footnote columns: footnote_1, footnote_2, ...
        # We use the original marker as part of the value for traceability
        for idx, (marker, text) in enumerate(result.footnotes.items(), start=1):
            row[f"footnote_{idx}"] = text
            row[f"footnote_{idx}_marker"] = marker
        
        rows.append(row)
    
    df = pd.DataFrame(rows)
    
    # Sort by CIK and date for readability
    df = df.sort_values(["cik", "as_at_date"]).reset_index(drop=True)
    
    return df


# ---------------------------------------------------------------------------
# QA Review
# ---------------------------------------------------------------------------

def run_qa_review(
    all_results: List[FootnoteResult],
    all_section_infos: List[Optional[SectionInfo]],
    logger: logging.Logger,
) -> Tuple[List[FootnoteResult], List[QAVerdict]]:
    """
    Run LLM-based QA review on all extraction results.
    
    For each result:
    1. Build evidence windows from the HTML section
    2. Send to Gemini for 3-layer verification
    3. If any layer fails, use Gemini to re-extract (self-correction)
    4. Replace failed results with LLM-corrected ones
    
    Returns:
        - Updated results list (with corrections applied)
        - List of QA verdicts for report generation
    """
    from qa_reviewer.tracing import get_langfuse
    from collections import defaultdict
    
    verdicts = []
    corrected_count = 0
    
    # Group results by file_code to track file-level metrics
    results_by_file = defaultdict(list)
    for idx, result in enumerate(all_results):
        results_by_file[result.file_code].append((idx, result))
    
    # Create file-level Langfuse spans for tracking date coverage
    langfuse = get_langfuse()
    file_spans = {}
    if langfuse is not None:
        for file_code, file_results in results_by_file.items():
            # Get unique dates for this file
            unique_dates = set(r.as_at_date for _, r in file_results if r.as_at_date not in ("NOT_FOUND", "ERROR"))
            cik = file_results[0][1].cik if file_results else "unknown"
            
            file_span = langfuse.start_span(
                name="filing_processing",
                input={
                    "file_code": file_code,
                    "cik": cik,
                    "num_sections": len(file_results),
                    "as_at_dates": sorted(unique_dates),
                    "num_distinct_dates": len(unique_dates),
                },
                metadata={
                    "file_code": file_code,
                    "cik": cik,
                },
            )
            file_spans[file_code] = file_span
    
    for i, (result, sinfo) in enumerate(zip(all_results, all_section_infos)):
        logger.info(f"--- QA Review {i+1}/{len(all_results)}: "
                    f"CIK={result.cik} date={result.as_at_date} ---")
        
        # Skip results that had no section found (nothing to review)
        if sinfo is None or result.as_at_date in ("NOT_FOUND", "ERROR"):
            logger.info("  Skipping (no section to review)")
            verdicts.append(QAVerdict(
                cik=result.cik,
                file_code=result.file_code,
                as_at_date=result.as_at_date,
                overall_pass=False,
                error="No section found — nothing to review",
            ))
            continue
        
        start_elem, end_elem, soup = sinfo
        
        # Build evidence windows for the LLM (include html_path for traceability)
        evidence = extract_evidence(
            start_elem, end_elem, soup, result.footnotes,
            html_path=result.file_code,  # Use accession as identifier in traces
        )
        
        # Run 3-layer verification (returns verdict; Langfuse trace created inside)
        verdict = review_extraction(
            evidence=evidence,
            cik=result.cik,
            file_code=result.file_code,
            as_at_date=result.as_at_date,
        )
        verdicts.append(verdict)
        
        # Track QA flags on the result object (flow into CSV)
        result.qa_reviewed = True
        result.qa_passed_first = verdict.overall_pass
        result.qa_passed_final = verdict.overall_pass  # May be updated below
        
        # Two-stage correction when QA fails:
        # Stage 1: Targeted fixer — surgically repairs specific issues
        # Stage 2: Full self-correction — last resort, re-extracts everything
        if not verdict.overall_pass and evidence.footnote_area_text:
            logger.info("  QA FAILED — running targeted fixer agent...")
            try:
                from qa_reviewer.tracing import get_langfuse
                lf = get_langfuse()
                
                # Build structured fix request from QA verdict
                fix_request = FixRequest(
                    cik=result.cik,
                    file_code=result.file_code,
                    as_at_date=result.as_at_date,
                    html_path=result.html_path,
                    issues=verdict.accuracy_check.details.get("issues", []),
                    missing_markers=verdict.completeness_check.details.get("missing_markers", []),
                    extracted_footnotes=dict(result.footnotes),
                    footnote_area_text=evidence.footnote_area_text,
                )
                
                # Stage 1: Targeted fixer (span created after request so input is populated)
                fixer_trace = None
                if lf is not None:
                    fixer_trace = lf.start_span(
                        name="fixer_agent",
                        input={
                            "cik": result.cik,
                            "file_code": result.file_code,
                            "as_at_date": result.as_at_date,
                            "html_path": result.html_path,
                            "num_issues": len(fix_request.issues),
                            "issues": fix_request.issues,
                            "missing_markers": fix_request.missing_markers,
                            "num_extracted_footnotes": len(fix_request.extracted_footnotes),
                            "extracted_markers": list(fix_request.extracted_footnotes.keys()),
                        },
                        metadata={"cik": result.cik, "file_code": result.file_code, "as_at_date": result.as_at_date},
                    )
                
                fix_result = fix_issues(fix_request, trace=fixer_trace)
                
                # Score the fixer in Langfuse
                if fixer_trace is not None:
                    fixer_trace.score(
                        name="fix_rate",
                        value=fix_result.fix_rate,
                        comment=f"{len(fix_result.fixes_applied)} fixes applied"
                    )
                    fixer_trace.update(output=fix_result.to_dict())
                    fixer_trace.end()
                
                # Apply fixer corrections if any were made
                if fix_result.fixes_applied:
                    result.footnotes = fix_result.corrected_footnotes
                    result.extraction_method = "deterministic+fixer"
                    result.fixer_applied = True
                    corrected_count += 1
                    fixed_markers = [f.marker for f in fix_result.fixes_applied]
                    logger.info(f"  Fixer: applied {len(fix_result.fixes_applied)} targeted fixes")
                    
                    # Targeted re-verify: only check the markers the fixer touched.
                    # Uses 1 LLM call instead of full 3-layer re-QA (2 calls).
                    reqa_evidence = extract_evidence(
                        start_elem, end_elem, soup, result.footnotes,
                        html_path=result.file_code,
                    )
                    reqa_verdict = review_fixed_markers(
                        evidence=reqa_evidence,
                        fixed_markers=fixed_markers,
                        cik=result.cik,
                        file_code=result.file_code,
                        as_at_date=result.as_at_date,
                    )
                    result.qa_reviewed_twice = True
                    result.qa_passed_final = reqa_verdict.passed
                    reqa_status = "✅ PASS" if reqa_verdict.passed else "❌ STILL FAILING"
                    logger.info(f"  Re-verify fixed markers {fixed_markers}: {reqa_status}")
                
            except Exception as e:
                logger.error(f"  Fixer agent failed: {e}")
    
    # Close file-level spans and add date_coverage scores
    if langfuse is not None and file_spans:
        for file_code, file_span in file_spans.items():
            # Get final results for this file
            file_results = [r for _, r in results_by_file[file_code]]
            unique_dates = set(r.as_at_date for r in file_results if r.as_at_date not in ("NOT_FOUND", "ERROR"))
            num_dates = len(unique_dates)
            
            # Calculate metrics
            sections_qa_passed = sum(1 for r in file_results if r.qa_passed_final)
            sections_with_fixer = sum(1 for r in file_results if r.fixer_applied)
            
            # Date coverage score: 1.0 if ≥2 dates (expected), 0.0 if <2
            date_coverage_score = 1.0 if num_dates >= 2 else 0.0
            
            file_span.update(
                output={
                    "num_distinct_dates": num_dates,
                    "as_at_dates": sorted(unique_dates),
                    "sections_processed": len(file_results),
                    "sections_qa_passed": sections_qa_passed,
                    "sections_with_fixer_applied": sections_with_fixer,
                }
            )
            
            # Add date_coverage score
            file_span.score(
                name="date_coverage",
                value=date_coverage_score,
                comment=f"{num_dates} distinct dates (expected ≥2)"
            )
            
            file_span.end()
    
    logger.info(f"QA Review complete. {corrected_count} sections self-corrected via LLM.")
    return all_results, verdicts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Extract footnotes from BDC SEC filings"
    )
    parser.add_argument(
        "--qa",
        action="store_true",
        help="Enable LLM-based QA review (requires GEMINI_API_KEY env var)",
    )
    return parser.parse_args()


def main():
    """
    Main pipeline: discover → process → (optional QA) → assemble → export.
    """
    args = parse_args()
    logger = setup_logging()
    
    logger.info("=" * 70)
    logger.info("BDC Footnote Extraction Pipeline")
    if args.qa:
        logger.info("QA Review: ENABLED (Gemini)")
    logger.info("=" * 70)
    
    # Step 1: Discover all filing HTML files
    filings = discover_filings(FILINGS_BASE_PATH)
    
    if not filings:
        logger.error("No filings found. Check FILINGS_BASE_PATH.")
        return
    
    logger.info(f"Found {len(filings)} filings to process")
    
    # Step 2: Process each filing sequentially.
    # The real speedup comes from the text-node-index optimization inside
    # process_filing (100x+ faster per file), not from parallelism.
    # Sequential keeps logging clean and avoids pickling issues.
    all_results = []
    all_section_infos = []  # Only populated when --qa is set
    success_count = 0
    fail_count = 0
    
    logger.info(f"Processing {len(filings)} filings sequentially (optimized)...")
    
    for i, filing in enumerate(filings):
        results, section_infos = process_filing(filing, keep_section_info=args.qa)
        all_results.extend(results)
        all_section_infos.extend(section_infos)
        for r in results:
            if r.footnotes:
                success_count += 1
            else:
                fail_count += 1
        if (i + 1) % 50 == 0 or (i + 1) == len(filings):
            logger.info(f"  Extracted {i + 1}/{len(filings)} filings")
    
    # Step 3 (optional): QA review via Gemini
    qa_verdicts = []
    if args.qa:
        logger.info("=" * 70)
        logger.info("STARTING QA REVIEW")
        logger.info("=" * 70)
        all_results, qa_verdicts = run_qa_review(
            all_results, all_section_infos, logger
        )
    
    # Step 4: Convert to DataFrame
    df = results_to_dataframe(all_results)
    
    # Step 5: Save to CSV
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df.to_csv(OUTPUT_FILE, index=False)
    
    # Step 6: Calculate success metrics
    # SUCCESS METRIC: Each file_code should have at least 2 distinct as_at_date values
    # (current period + prior period comparison)
    dates_per_file = df.groupby('file_code')['as_at_date'].nunique()
    files_with_insufficient_dates = dates_per_file[dates_per_file < 2]
    
    # Step 7: Print extraction summary
    logger.info("=" * 70)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total filings processed: {len(filings)}")
    logger.info(f"Total date sub-sections: {len(all_results)}")
    logger.info(f"  Successful (with footnotes): {success_count}")
    logger.info(f"  Failed (no footnotes):       {fail_count}")
    logger.info(f"Output saved to: {os.path.abspath(OUTPUT_FILE)}")
    logger.info(f"Max footnotes in a single section: {df['num_footnotes'].max()}")
    logger.info("")
    logger.info("SUCCESS METRIC: Distinct as_at_date per file_code")
    logger.info(f"  Expected: ≥2 dates per filing (current + prior period)")
    logger.info(f"  Files meeting criteria: {len(dates_per_file[dates_per_file >= 2])}/{len(dates_per_file)}")
    if len(files_with_insufficient_dates) > 0:
        logger.warning(f"  ⚠️  {len(files_with_insufficient_dates)} files have <2 dates:")
        for file_code, count in files_with_insufficient_dates.items():
            logger.warning(f"      {file_code}: {count} date(s)")
    else:
        logger.info(f"  ✅ All filings have ≥2 distinct dates")
    logger.info("=" * 70)
    
    # Step 7 (optional): Generate QA report
    if args.qa and qa_verdicts:
        generate_report(qa_verdicts, OUTPUT_DIR)
    
    # Print a preview of the results
    print("\n--- PREVIEW (first 10 rows, key columns) ---")
    preview_cols = ["cik", "file_code", "as_at_date", "num_footnotes", "extraction_method", "marker_type", "first_marker_valid"]
    print(df[preview_cols].head(10).to_string())

    # Flush Langfuse events so nothing is lost on exit
    flush_langfuse()


if __name__ == "__main__":
    main()
