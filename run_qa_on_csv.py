"""
Run QA reviewer + fixer on an existing bdc_footnotes CSV.

This script:
1. Reads each row from the input CSV (already extracted footnotes)
2. Loads the original HTML to build evidence windows
3. Runs the Gemini QA reviewer (section + completeness + accuracy checks)
4. Runs the fixer agent on any failures
5. Updates the CSV with QA results and corrected footnotes

Usage:
    source venv/bin/activate && python run_qa_on_csv.py output/bdc_footnotes\ copy.csv
"""

import os
import sys
import json
import logging
import argparse
import threading
from datetime import datetime
from collections import OrderedDict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load .env file for API keys
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            os.environ.setdefault(key.strip(), value)

import pandas as pd
from bs4 import BeautifulSoup

from parsers.html_parser import load_html, find_schedule_sections
from parsers.footnote_extractor import build_text_node_index
from qa_reviewer.evidence_extractor import extract_evidence
from qa_reviewer.reviewer import review_extraction, review_fixed_markers
from qa_reviewer.self_correct import correct_footnotes, re_extract_footnotes
from qa_reviewer.fixer_learner import learn_from_issues, LearningRequest, get_learning_summary
from qa_reviewer.tracing import get_langfuse, flush_langfuse


# ---------------------------------------------------------------------------
# Quarter-end date validation
# ---------------------------------------------------------------------------

QUARTER_END_DATES = {
    # Q1: March 31
    (3, 31),
    # Q2: June 30
    (6, 30),
    # Q3: September 30
    (9, 30),
    # Q4: December 31
    (12, 31),
}

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def is_quarter_end_date(date_str: str) -> bool:
    """
    Check if a date string like "December 31, 2025" is a quarter-end date.
    
    Quarter-end dates are: March 31, June 30, September 30, December 31.
    Returns True if valid quarter-end, False otherwise.
    """
    import re
    
    if not date_str or date_str in ("NOT_FOUND", "ERROR", "UNKNOWN"):
        return False
    
    # Parse "Month Day, Year" format
    match = re.match(r"(\w+)\s+(\d{1,2}),?\s*(\d{4})?", date_str.strip(), re.IGNORECASE)
    if not match:
        return False
    
    month_name = match.group(1).lower()
    day = int(match.group(2))
    
    month = MONTH_MAP.get(month_name)
    if not month:
        return False
    
    return (month, day) in QUARTER_END_DATES


def setup_logging():
    """Configure logging to console + file."""
    os.makedirs("output/logs", exist_ok=True)
    log_file = f"output/logs/qa_csv_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


def row_to_footnotes(row: pd.Series) -> OrderedDict:
    """
    Extract footnotes from a CSV row back into an OrderedDict.
    
    The CSV has columns like footnote_1, footnote_1_marker, footnote_2, etc.
    We reconstruct {marker: text} from these columns.
    """
    footnotes = OrderedDict()
    i = 1
    while True:
        text_col = f"footnote_{i}"
        marker_col = f"footnote_{i}_marker"
        if text_col not in row or pd.isna(row[text_col]):
            break
        text = row[text_col]
        marker = row[marker_col] if marker_col in row and pd.notna(row[marker_col]) else str(i)
        if text and str(text).strip():
            footnotes[str(marker)] = str(text)
        i += 1
    return footnotes


def _normalize_marker(marker: str) -> tuple:
    """
    Normalize a marker to extract its base number/letter and any sub-marker.
    
    Examples:
        "1" -> (1, None, "number")
        "(1)" -> (1, None, "number")
        "6a" -> (6, "a", "number")
        "6b" -> (6, "b", "number")
        "A" -> ("A", None, "letter")
        "(A)" -> ("A", None, "letter")
        "b" -> ("b", None, "letter")
        "*" -> ("*", None, "special")
        "**" -> ("**", None, "special")
    
    Returns: (base, sub_marker, marker_type)
    """
    import re
    
    # Strip parentheses
    clean = marker.strip().strip("()")
    
    # Check for special characters first
    if clean in ("*", "**", "†", "‡", "§"):
        return (clean, None, "special")
    
    # Check for number with optional letter suffix (e.g., "6a", "6b")
    match = re.match(r'^(\d+)([a-zA-Z])?$', clean)
    if match:
        base = int(match.group(1))
        sub = match.group(2)
        return (base, sub, "number")
    
    # Check for pure number
    if clean.isdigit():
        return (int(clean), None, "number")
    
    # Check for single letter
    if len(clean) == 1 and clean.isalpha():
        return (clean.upper(), None, "letter")
    
    # Fallback - treat as special
    return (clean, None, "special")


def footnotes_to_row_updates(footnotes: OrderedDict, max_num_cols: int = 50) -> dict:
    """
    Convert an OrderedDict of footnotes to properly structured CSV columns.
    
    Structure:
    - footnote_1 to footnote_50: For numbered markers (1, 2, 3...)
    - Sub-markers (6a, 6b, 6c) are concatenated into their parent (footnote_6)
    - Standalone letters (a, b, c, d) after numbered footnotes are appended to the last number
    - footnote_* and footnote_**: For special character markers at the end
    
    The footnote_{n}_marker column is removed - the column name IS the marker.
    """
    import re
    
    # Group footnotes by their normalized base marker
    numbered = {}  # {base_num: [(sub, text), ...]}
    special = {}   # {"*": text, "**": text}
    
    # Track the last numbered marker seen for appending standalone letters
    last_num = 0
    
    for marker, text in footnotes.items():
        base, sub, mtype = _normalize_marker(marker)
        
        if mtype == "number":
            if base not in numbered:
                numbered[base] = []
            numbered[base].append((sub, text))
            last_num = max(last_num, base)
        elif mtype == "letter":
            # Standalone letter (a, b, c, d) - append to the last numbered footnote
            # These are typically sub-footnotes like 6a, 6b, 6c
            if last_num > 0:
                if last_num not in numbered:
                    numbered[last_num] = []
                # Use the letter as a sub-marker
                numbered[last_num].append((base.lower(), text))
            else:
                # No previous number - treat as special
                special[base] = text
        else:  # special
            special[base] = text
    
    updates = {}
    
    # Process numbered footnotes (1-50)
    for num in sorted(numbered.keys()):
        if num > max_num_cols:
            continue
        
        entries = numbered[num]
        if len(entries) == 1 and entries[0][0] is None:
            # Single entry without sub-marker
            updates[f"footnote_{num}"] = entries[0][1]
        else:
            # Multiple entries or has sub-markers - concatenate
            # Sort by sub-marker (None first, then a, b, c...)
            entries.sort(key=lambda x: (x[0] is not None, x[0] or ""))
            combined_text = " ".join([e[1] for e in entries])
            updates[f"footnote_{num}"] = combined_text
    
    # Clear unused numbered columns and set markers
    for j in range(1, max_num_cols + 1):
        if f"footnote_{j}" not in updates:
            updates[f"footnote_{j}"] = None
            updates[f"footnote_{j}_marker"] = None
        else:
            # Set the marker to match the footnote number
            updates[f"footnote_{j}_marker"] = str(j)
    
    # Process special character footnotes
    for sym in ["*", "**", "†", "‡", "§"]:
        col_name = f"footnote_{sym}"
        if sym in special:
            updates[col_name] = special[sym]
        else:
            updates[col_name] = None
    
    # Count actual footnotes for num_footnotes field
    actual_count = len([k for k in updates if k.startswith("footnote_") and updates[k] is not None and not k.endswith("_marker")])
    updates["num_footnotes"] = actual_count
    
    return updates


def find_section_for_date(sections, target_date: str):
    """
    Find the section matching the target as_at_date.
    
    sections is a list of (start_elem, end_elem, date_str) tuples.
    Returns (start_elem, end_elem) or (None, None) if not found.
    """
    # Normalize target date for comparison
    target_normalized = target_date.strip().upper()
    
    for start_elem, end_elem, section_date in sections:
        section_normalized = section_date.strip().upper()
        if section_normalized == target_normalized:
            return start_elem, end_elem
    
    # Fallback: partial match (e.g., "December 31, 2023" matches "DECEMBER 31, 2023")
    for start_elem, end_elem, section_date in sections:
        if target_date.lower() in section_date.lower() or section_date.lower() in target_date.lower():
            return start_elem, end_elem
    
    return None, None


def run_qa_on_row(row: pd.Series, soup: BeautifulSoup, sections: list, logger) -> dict:
    """
    Run QA review + LLM self-correction on a single CSV row.
    
    Flow:
    1. Run QA review on existing footnotes
    2. If QA fails, use LLM to re-extract correct footnotes
    3. Run QA again on LLM-extracted footnotes
    4. Update CSV with corrected footnotes
    
    Returns a dict of column updates to apply to the row.
    """
    cik = str(row["cik"])
    file_code = str(row["file_code"])
    as_at_date = str(row["as_at_date"])
    html_path = str(row["html_path"]) if pd.notna(row.get("html_path")) else ""
    
    # Reconstruct footnotes from CSV columns
    footnotes = row_to_footnotes(row)
    
    # Check if date is a valid quarter-end
    is_valid_quarter = is_quarter_end_date(as_at_date)
    
    # Get current timestamp for tracking
    current_time = datetime.now().isoformat()
    
    updates = {
        "llm_reviewed": False,           # Was this row reviewed by LLM?
        "llm_reviewed_at": None,         # Timestamp when LLM reviewed
        "llm_corrected": False,          # Did LLM make corrections?
        "llm_corrected_at": None,        # Timestamp when LLM corrected
        "correction_applied": None,      # Summary of what was corrected
        "qa_passed": False,              # Did the row pass QA?
        "num_footnotes": len(footnotes),
        "is_quarter_end": is_valid_quarter,
        "extraction_method": "deterministic",
    }
    
    # Skip rows with no footnotes or special dates
    if not footnotes or as_at_date in ("NOT_FOUND", "ERROR", "UNKNOWN"):
        logger.info(f"  Skipping (no footnotes or invalid date)")
        return updates
    
    # Flag non-quarter-end dates
    if not is_valid_quarter:
        logger.warning(f"  ⚠️ Date '{as_at_date}' is NOT a quarter-end date")
    
    # Find the matching section in the HTML
    start_elem, end_elem = find_section_for_date(sections, as_at_date)
    if start_elem is None:
        logger.warning(f"  Could not find section for date '{as_at_date}' — skipping QA")
        return updates
    
    # Build evidence for QA
    evidence = extract_evidence(start_elem, end_elem, soup, footnotes, html_path=file_code)
    
    # Run QA review (skip Layer 1 boundary check to save 1 LLM call)
    verdict = review_extraction(
        evidence=evidence,
        cik=cik,
        file_code=file_code,
        as_at_date=as_at_date,
        skip_layer1=True,
    )
    
    # Mark as LLM reviewed with timestamp
    updates["llm_reviewed"] = True
    updates["llm_reviewed_at"] = current_time
    updates["qa_passed"] = verdict.overall_pass
    
    overall = "✅ PASS" if verdict.overall_pass else "❌ FAIL"
    logger.info(f"  QA: {overall}")
    
    # If QA failed, use LLM to correct specific issues (keep correct ones, fix wrong ones, add missing)
    if not verdict.overall_pass and evidence.footnote_area_text:
        logger.info(f"  Running LLM targeted correction...")
        
        # Get issues and missing markers from QA verdict
        issues = verdict.accuracy_check.details.get("issues", [])
        missing = verdict.completeness_check.details.get("missing_markers", [])
        
        # Log learnings from the failure (for analysis)
        if issues or missing:
            learning_request = LearningRequest(
                cik=cik,
                file_code=file_code,
                as_at_date=as_at_date,
                html_path=html_path,
                issues=issues,
                missing_markers=missing,
                extracted_footnotes=dict(footnotes),
                footnote_area_text=evidence.footnote_area_text,
            )
            learn_from_issues(learning_request)
        
        # Get Langfuse trace for this correction
        lf = get_langfuse()
        trace = None
        if lf is not None:
            trace = lf.start_span(
                name="llm_targeted_correct",
                input={
                    "cik": cik,
                    "file_code": file_code,
                    "as_at_date": as_at_date,
                    "original_footnotes": len(footnotes),
                    "issues": issues[:5],
                    "missing_markers": missing,
                },
                metadata={"cik": cik, "file_code": file_code},
            )
        
        # LLM corrects specific issues while keeping correct footnotes
        corrected_footnotes = correct_footnotes(
            footnote_area_text=evidence.footnote_area_text,
            existing_footnotes=dict(footnotes),
            issues=issues,
            missing_markers=missing,
            trace=trace,
        )
        
        if corrected_footnotes and corrected_footnotes != footnotes:
            updates["llm_corrected"] = True
            updates["llm_corrected_at"] = current_time
            updates["extraction_method"] = "llm_corrected"
            updates["num_footnotes"] = len(corrected_footnotes)
            
            # Build a short summary of what changed
            added = set(corrected_footnotes.keys()) - set(footnotes.keys())
            removed = set(footnotes.keys()) - set(corrected_footnotes.keys())
            changed = {k for k in set(footnotes.keys()) & set(corrected_footnotes.keys())
                       if footnotes.get(k) != corrected_footnotes.get(k)}
            parts = []
            if added:   parts.append(f"added:{','.join(sorted(added))}")
            if removed: parts.append(f"removed:{','.join(sorted(removed))}")
            if changed: parts.append(f"fixed:{','.join(sorted(changed))}")
            updates["correction_applied"] = "; ".join(parts) if parts else "minor"
            
            # Update footnote columns with corrected values
            footnote_updates = footnotes_to_row_updates(corrected_footnotes)
            updates.update(footnote_updates)
            
            logger.info(f"  LLM corrected footnotes: {len(corrected_footnotes)} total (was {len(footnotes)}) [{updates['correction_applied']}]")
            
            if trace is not None:
                trace.update(output={
                    "corrected_footnotes": len(corrected_footnotes),
                    "skipped_reqa": True,
                })
                trace.end()
        else:
            logger.warning(f"  LLM correction made no changes")
            if trace is not None:
                trace.update(output={"error": "No corrections applied"})
                trace.end()
    
    return updates


def main():
    parser = argparse.ArgumentParser(description="Run QA reviewer on existing CSV")
    parser.add_argument("csv_path", help="Path to the bdc_footnotes CSV file")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of rows to process")
    parser.add_argument("--workers", type=int, default=5, help="Number of parallel workers (default: 5)")
    args = parser.parse_args()
    
    logger = setup_logging()
    
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY not set. Create a .env file.")
        return
    
    logger.info("=" * 60)
    logger.info(f"QA REVIEW ON CSV: {args.csv_path}")
    logger.info(f"PARALLEL WORKERS: {args.workers}")
    logger.info("=" * 60)
    
    # Resume support: load checkpoint if it exists, otherwise load input CSV
    checkpoint_path = "output/bdc_footnotes_qa_output.csv"
    if os.path.exists(checkpoint_path):
        df = pd.read_csv(checkpoint_path, low_memory=False)
        already_done = int(df["llm_reviewed"].sum()) if "llm_reviewed" in df.columns else 0
        logger.info(f"RESUMING from checkpoint: {checkpoint_path} ({already_done} rows already reviewed)")
    else:
        df = pd.read_csv(args.csv_path, low_memory=False)
        logger.info(f"Starting fresh from: {args.csv_path}")
    
    total_rows = len(df)
    logger.info(f"Loaded {total_rows} rows from CSV")
    
    if args.limit:
        df = df.head(args.limit)
        logger.info(f"Processing first {args.limit} rows only")
    
    # Group rows by html_path, only include rows NOT yet reviewed
    rows_by_html = {}
    skipped = 0
    for idx, row in df.iterrows():
        # Skip rows already reviewed (resume support)
        if row.get("llm_reviewed") == True:
            skipped += 1
            continue
        html_path = row.get("html_path", "")
        if pd.isna(html_path) or not html_path:
            continue
        if html_path not in rows_by_html:
            rows_by_html[html_path] = []
        rows_by_html[html_path].append(idx)
    
    remaining = sum(len(v) for v in rows_by_html.values())
    logger.info(f"Skipping {skipped} already-reviewed rows, {remaining} rows remaining")
    logger.info(f"Found {len(rows_by_html)} unique HTML files to process")
    
    # Thread-safe counters
    counter_lock = threading.Lock()
    counters = {"processed": 0, "llm_reviewed": 0, "llm_corrected": 0, "qa_passed": 0, "non_quarter_dates": 0}
    
    # Thread-safe DataFrame update lock
    df_lock = threading.Lock()
    
    def process_html_file(html_idx, html_path, row_indices):
        """Process all rows for a single HTML file."""
        local_results = []
        
        try:
            soup = load_html(html_path)
            sections = find_schedule_sections(soup)
            logger.info(f"[{html_idx+1}/{len(rows_by_html)}] {html_path}: {len(sections)} sections, {len(row_indices)} rows")
        except Exception as e:
            logger.error(f"[{html_idx+1}] Failed to load HTML: {e}")
            return local_results
        
        for row_idx in row_indices:
            row = df.loc[row_idx]
            as_at_date = row.get("as_at_date", "?")
            
            try:
                updates = run_qa_on_row(row, soup, sections, logger)
                local_results.append((row_idx, updates))
                
                status = "✅" if updates.get("qa_passed") else "❌"
                corrected = " (LLM corrected)" if updates.get("llm_corrected") else ""
                logger.info(f"  Row {row_idx} [{as_at_date}]: {status}{corrected}")
                
            except Exception as e:
                logger.error(f"  Error row {row_idx}: {e}")
        
        return local_results
    
    # Process HTML files in parallel
    html_items = list(enumerate(rows_by_html.items()))
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        # Submit all HTML files for processing
        futures = {
            executor.submit(process_html_file, idx, path, indices): (idx, path)
            for idx, (path, indices) in html_items
        }
        
        # Collect results as they complete
        for future in as_completed(futures):
            html_idx, html_path = futures[future]
            try:
                results = future.result()
                
                # Apply updates to DataFrame (thread-safe)
                with df_lock:
                    for row_idx, updates in results:
                        for col, val in updates.items():
                            if col in df.columns:
                                df.at[row_idx, col] = val
                            elif val is not None:
                                df.at[row_idx, col] = val
                
                # Update counters (thread-safe)
                with counter_lock:
                    for row_idx, updates in results:
                        counters["processed"] += 1
                        if updates.get("llm_reviewed"):
                            counters["llm_reviewed"] += 1
                        if updates.get("llm_corrected"):
                            counters["llm_corrected"] += 1
                        if updates.get("qa_passed"):
                            counters["qa_passed"] += 1
                        if not updates.get("is_quarter_end", True):
                            counters["non_quarter_dates"] += 1
                    
                    # Checkpoint: save progress every 100 rows
                    # Use integer division to handle batch jumps (e.g., 98->105 still triggers at 100)
                    checkpoint_interval = 100
                    current_checkpoint = counters["processed"] // checkpoint_interval
                    if current_checkpoint > counters.get("last_checkpoint", 0):
                        counters["last_checkpoint"] = current_checkpoint
                        logger.info(f"=== CHECKPOINT: {counters['processed']} rows processed, saving... ===")
                        with df_lock:
                            checkpoint_path = "output/bdc_footnotes_qa_output.csv"
                            df.to_csv(checkpoint_path, index=False)
                        logger.info(f"=== Saved checkpoint to {checkpoint_path} ===")
                        
            except Exception as e:
                logger.error(f"Error processing {html_path}: {e}")
    
    # Extract final counters
    processed = counters["processed"]
    llm_reviewed = counters["llm_reviewed"]
    llm_corrected = counters["llm_corrected"]
    qa_passed = counters["qa_passed"]
    non_quarter_dates = counters["non_quarter_dates"]
    
    # Drop extra footnote columns (>50) but keep marker columns for debugging
    cols_to_drop = []
    for i in range(51, 200):
        if f'footnote_{i}' in df.columns:
            cols_to_drop.append(f'footnote_{i}')
        if f'footnote_{i}_marker' in df.columns:
            cols_to_drop.append(f'footnote_{i}_marker')
    df = df.drop(columns=cols_to_drop, errors='ignore')
    
    # Reorder columns: metadata first, then footnote_1/marker pairs to footnote_50, then special chars
    meta_cols = ['cik', 'file_code', 'as_at_date', 'html_path', 'extraction_method', 
                 'marker_type', 'first_marker_valid', 'num_footnotes', 
                 'llm_reviewed', 'llm_reviewed_at', 'llm_corrected', 'llm_corrected_at',
                 'correction_applied', 'qa_passed', 'is_quarter_end']
    
    # Build footnote columns with their markers
    footnote_cols = []
    for i in range(1, 51):
        footnote_cols.append(f'footnote_{i}')
        footnote_cols.append(f'footnote_{i}_marker')
    
    special_cols = ['footnote_*', 'footnote_**']
    
    # Build final column order
    final_cols = [c for c in meta_cols if c in df.columns]
    final_cols += [c for c in footnote_cols if c in df.columns]
    final_cols += [c for c in special_cols if c in df.columns]
    
    df = df[final_cols]
    
    # Save updated CSV
    output_path = args.csv_path.replace(".csv", "_qa.csv")
    df.to_csv(output_path, index=False)
    logger.info(f"\nSaved QA results to: {output_path}")
    
    # Summary
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Total rows processed:   {processed}")
    logger.info(f"  LLM reviewed:           {llm_reviewed}")
    logger.info(f"  LLM corrected:          {llm_corrected}")
    logger.info(f"  QA passed:              {qa_passed} ({100*qa_passed/max(1,processed):.1f}%)")
    logger.info(f"  Non-quarter-end dates:  {non_quarter_dates}")
    logger.info("=" * 60)
    
    # Learning summary - show what patterns were discovered
    learning_summary = get_learning_summary()
    logger.info("LEARNINGS (patterns discovered from failures)")
    logger.info(f"  Total issues logged:  {learning_summary['total_issues']}")
    logger.info(f"  By type: {learning_summary['by_type']}")
    if learning_summary['page_headers']:
        logger.info(f"  Page headers found:   {learning_summary['unique_page_headers']}")
        for h in learning_summary['page_headers'][:5]:
            logger.info(f"    - {h[:60]}")
    logger.info(f"  Learnings saved to:   output/fixer_learnings.json")
    logger.info("=" * 60)
    
    # Flush Langfuse
    flush_langfuse()
    logger.info("Done.")


if __name__ == "__main__":
    main()
