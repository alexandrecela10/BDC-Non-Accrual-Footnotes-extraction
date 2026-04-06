"""
Full BDC Footnotes Pipeline — Deterministic Extraction + Agentic QA Review

This script runs the complete pipeline:
1. Deterministic extraction using learned patterns from extraction_memory.json
2. Agentic QA review + LLM correction for each row
3. Saves results with timestamps and correction summaries

Usage:
    source venv/bin/activate && python run_full_pipeline.py --workers 5
"""

import os
import sys
import json
import logging
import argparse
import time
from datetime import datetime
from pathlib import Path
from collections import OrderedDict

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

from models import FilingMetadata
from parsers.html_parser import discover_filings, load_html, find_schedule_sections
from parsers.footnote_extractor import (
    extract_footnotes_from_section,
    build_text_node_index,
    detect_marker_type,
    check_first_marker_valid,
)
from qa_reviewer.evidence_extractor import extract_evidence
from qa_reviewer.reviewer import review_extraction
from qa_reviewer.self_correct import correct_footnotes
from qa_reviewer.tracing import get_langfuse, flush_langfuse


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FILINGS_BASE_PATH = "BDC Footnotes"
OUTPUT_DIR = "output"
MEMORY_FILE = os.path.join(OUTPUT_DIR, "extraction_memory.json")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "bdc_footnotes_full_pipeline.csv")
CHECKPOINT_FILE = os.path.join(OUTPUT_DIR, "bdc_footnotes_full_pipeline_checkpoint.csv")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    """Configure logging to both file and console."""
    log_dir = os.path.join(OUTPUT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f"full_pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    
    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory Loading
# ---------------------------------------------------------------------------

def load_extraction_memory():
    """Load learned patterns from the consolidated memory file."""
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            memory = json.load(f)
        return memory
    return {"page_header_patterns": [], "known_fixes": [], "learnings": []}


# ---------------------------------------------------------------------------
# Quarter-end date validation
# ---------------------------------------------------------------------------

def is_quarter_end_date(date_str: str) -> bool:
    """Check if a date string represents a valid quarter-end date."""
    if not date_str or date_str in ("NOT_FOUND", "ERROR", "UNKNOWN"):
        return False
    
    date_lower = date_str.lower()
    
    # Quarter-end months and their typical day endings
    quarter_ends = [
        ("march", "31"), ("mar", "31"),
        ("june", "30"), ("jun", "30"),
        ("september", "30"), ("sep", "30"), ("sept", "30"),
        ("december", "31"), ("dec", "31"),
    ]
    
    for month, day in quarter_ends:
        if month in date_lower and day in date_str:
            return True
    
    return False


# ---------------------------------------------------------------------------
# Deterministic Extraction
# ---------------------------------------------------------------------------

def extract_all_filings(logger, memory):
    """
    Run deterministic extraction on all filings.
    Returns a list of result dicts ready for DataFrame conversion.
    """
    logger.info("=" * 60)
    logger.info("PHASE 1: DETERMINISTIC EXTRACTION")
    logger.info("=" * 60)
    
    # Discover all filings
    filings = discover_filings(FILINGS_BASE_PATH)
    logger.info(f"Found {len(filings)} filings to process")
    
    results = []
    
    for idx, filing in enumerate(filings):
        if idx % 100 == 0:
            logger.info(f"Processing filing {idx+1}/{len(filings)}...")
        
        try:
            # Load and parse HTML
            soup = load_html(filing.html_path)
            sections = find_schedule_sections(soup)
            
            if not sections:
                results.append({
                    "cik": filing.cik,
                    "file_code": filing.accession,
                    "as_at_date": "NOT_FOUND",
                    "html_path": filing.html_path,
                    "extraction_method": "deterministic",
                    "num_footnotes": 0,
                })
                continue
            
            # Build text node index once per document
            all_nodes, id_to_pos = build_text_node_index(soup)
            
            # Extract from each section
            for start_elem, end_elem, as_at_date in sections:
                footnotes = extract_footnotes_from_section(
                    start_elem, end_elem, soup, all_nodes, id_to_pos
                )
                
                # Build result row
                row = {
                    "cik": filing.cik,
                    "file_code": filing.accession,
                    "as_at_date": as_at_date,
                    "html_path": filing.html_path,
                    "extraction_method": "deterministic",
                    "marker_type": detect_marker_type(footnotes),
                    "first_marker_valid": check_first_marker_valid(footnotes),
                    "num_footnotes": len(footnotes),
                    "is_quarter_end": is_quarter_end_date(as_at_date),
                    # QA columns (to be filled later)
                    "llm_reviewed": False,
                    "llm_reviewed_at": None,
                    "llm_corrected": False,
                    "llm_corrected_at": None,
                    "correction_applied": None,
                    "qa_passed": False,
                }
                
                # Add footnote columns
                for i, (marker, text) in enumerate(footnotes.items(), start=1):
                    if i <= 50:  # Cap at 50 footnotes
                        row[f"footnote_{i}"] = text
                        row[f"footnote_{i}_marker"] = marker
                
                results.append(row)
        
        except Exception as e:
            logger.error(f"Error processing {filing.accession}: {e}")
            results.append({
                "cik": filing.cik,
                "file_code": filing.accession,
                "as_at_date": "ERROR",
                "html_path": filing.html_path,
                "extraction_method": "deterministic",
                "num_footnotes": 0,
            })
    
    logger.info(f"Extraction complete: {len(results)} rows")
    return results


# ---------------------------------------------------------------------------
# Row-to-footnotes conversion
# ---------------------------------------------------------------------------

def row_to_footnotes(row) -> OrderedDict:
    """Extract footnotes from a DataFrame row into an OrderedDict."""
    footnotes = OrderedDict()
    for i in range(1, 51):
        text_col = f"footnote_{i}"
        marker_col = f"footnote_{i}_marker"
        if text_col in row.index and pd.notna(row.get(text_col)):
            text = str(row[text_col]).strip()
            if text:
                marker = str(row.get(marker_col, str(i))) if pd.notna(row.get(marker_col)) else str(i)
                footnotes[marker] = text
    return footnotes


def footnotes_to_row_updates(footnotes: OrderedDict) -> dict:
    """Convert footnotes dict back to row column updates."""
    updates = {}
    # Clear existing footnote columns
    for i in range(1, 51):
        updates[f"footnote_{i}"] = None
        updates[f"footnote_{i}_marker"] = None
    
    # Fill with new footnotes
    for i, (marker, text) in enumerate(footnotes.items(), start=1):
        if i <= 50:
            updates[f"footnote_{i}"] = text
            updates[f"footnote_{i}_marker"] = marker
    
    return updates


# ---------------------------------------------------------------------------
# Agentic QA Review
# ---------------------------------------------------------------------------

def run_qa_on_row(row, soup, sections, as_at_date, logger):
    """
    Run QA review + LLM correction on a single row.
    Returns a dict of column updates.
    """
    footnotes = row_to_footnotes(row)
    current_time = datetime.now().isoformat()
    
    updates = {
        "llm_reviewed": False,
        "llm_reviewed_at": None,
        "llm_corrected": False,
        "llm_corrected_at": None,
        "correction_applied": None,
        "qa_passed": False,
    }
    
    # Skip rows with no footnotes
    if not footnotes or as_at_date in ("NOT_FOUND", "ERROR", "UNKNOWN"):
        return updates
    
    # Find matching section
    target_section = None
    for start_elem, end_elem, section_date in sections:
        if section_date == as_at_date:
            target_section = (start_elem, end_elem)
            break
    
    if target_section is None:
        return updates
    
    start_elem, end_elem = target_section
    
    # Build evidence for QA
    evidence = extract_evidence(start_elem, end_elem, soup, footnotes, html_path=row.get("html_path", ""))
    
    # Run QA review
    try:
        verdict = review_extraction(
            evidence=evidence,
            cik=str(row["cik"]),
            file_code=str(row["file_code"]),
            as_at_date=as_at_date,
            skip_layer1=True,
        )
        
        updates["llm_reviewed"] = True
        updates["llm_reviewed_at"] = current_time
        updates["qa_passed"] = verdict.overall_pass
        
        # If QA failed, run LLM correction
        if not verdict.overall_pass and evidence.footnote_area_text:
            issues = verdict.accuracy_check.details.get("issues", [])
            missing = verdict.completeness_check.details.get("missing_markers", [])
            
            # Get Langfuse trace (v4 API uses trace() not start_span())
            lf = get_langfuse()
            trace = None
            if lf is not None:
                try:
                    trace = lf.trace(
                        name="llm_targeted_correct",
                        input={"cik": row["cik"], "file_code": row["file_code"], "as_at_date": as_at_date},
                    )
                except Exception:
                    pass  # Tracing is optional, don't fail if API changed
            
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
                updates["num_footnotes"] = len(corrected_footnotes)
                
                # Build correction summary
                added = set(corrected_footnotes.keys()) - set(footnotes.keys())
                removed = set(footnotes.keys()) - set(corrected_footnotes.keys())
                changed = {k for k in set(footnotes.keys()) & set(corrected_footnotes.keys())
                           if footnotes.get(k) != corrected_footnotes.get(k)}
                parts = []
                if added:   parts.append(f"added:{','.join(sorted(added))}")
                if removed: parts.append(f"removed:{','.join(sorted(removed))}")
                if changed: parts.append(f"fixed:{','.join(sorted(changed))}")
                updates["correction_applied"] = "; ".join(parts) if parts else "minor"
                
                # Update footnote columns
                footnote_updates = footnotes_to_row_updates(corrected_footnotes)
                updates.update(footnote_updates)
            
            if trace is not None:
                try:
                    trace.end()
                except Exception:
                    pass  # Tracing cleanup is optional
    
    except Exception as e:
        logger.error(f"QA error for row {row.name}: {e}")
    
    return updates


def run_agentic_qa(df, logger, checkpoint_interval=50):
    """
    Run agentic QA review on all rows in the DataFrame.
    Saves checkpoints every N rows.
    """
    logger.info("=" * 60)
    logger.info("PHASE 2: AGENTIC QA REVIEW + LLM CORRECTION")
    logger.info("=" * 60)
    
    total_rows = len(df)
    reviewed = 0
    corrected = 0
    passed = 0
    
    # Group by html_path to avoid reloading HTML
    rows_by_html = {}
    for idx, row in df.iterrows():
        # Skip already reviewed rows (resume support)
        if row.get("llm_reviewed") == True:
            reviewed += 1
            if row.get("llm_corrected") == True:
                corrected += 1
            if row.get("qa_passed") == True:
                passed += 1
            continue
        
        html_path = row.get("html_path", "")
        if pd.isna(html_path) or not html_path:
            continue
        if html_path not in rows_by_html:
            rows_by_html[html_path] = []
        rows_by_html[html_path].append(idx)
    
    remaining = sum(len(v) for v in rows_by_html.values())
    logger.info(f"Already reviewed: {reviewed}, Remaining: {remaining}")
    
    processed_this_run = 0
    
    for html_idx, (html_path, row_indices) in enumerate(rows_by_html.items()):
        try:
            soup = load_html(html_path)
            sections = find_schedule_sections(soup)
            
            if html_idx % 50 == 0:
                logger.info(f"[{html_idx+1}/{len(rows_by_html)}] {html_path}: {len(sections)} sections")
        except Exception as e:
            logger.error(f"Failed to load {html_path}: {e}")
            continue
        
        for row_idx in row_indices:
            row = df.loc[row_idx]
            as_at_date = row.get("as_at_date", "?")
            
            try:
                updates = run_qa_on_row(row, soup, sections, as_at_date, logger)
                
                # Apply updates to DataFrame
                for col, val in updates.items():
                    df.at[row_idx, col] = val
                
                # Track stats
                if updates.get("llm_reviewed"):
                    reviewed += 1
                if updates.get("llm_corrected"):
                    corrected += 1
                if updates.get("qa_passed"):
                    passed += 1
                
                processed_this_run += 1
                
                # Log progress
                status = "✅" if updates.get("qa_passed") else "❌"
                correction = " (corrected)" if updates.get("llm_corrected") else ""
                logger.info(f"  Row {row_idx} [{as_at_date}]: {status}{correction}")
                
                # Checkpoint
                if processed_this_run % checkpoint_interval == 0:
                    logger.info(f"=== CHECKPOINT: {processed_this_run} rows processed ===")
                    df.to_csv(CHECKPOINT_FILE, index=False)
                    logger.info(f"=== Saved to {CHECKPOINT_FILE} ===")
            
            except Exception as e:
                logger.error(f"Error on row {row_idx}: {e}")
    
    logger.info("=" * 60)
    logger.info("QA SUMMARY")
    logger.info(f"  Total rows:    {total_rows}")
    logger.info(f"  Reviewed:      {reviewed}")
    logger.info(f"  Corrected:     {corrected}")
    logger.info(f"  Passed QA:     {passed} ({100*passed/max(1,reviewed):.1f}%)")
    logger.info("=" * 60)
    
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Full BDC Footnotes Pipeline")
    parser.add_argument("--skip-extraction", action="store_true", 
                        help="Skip extraction, load from existing checkpoint")
    parser.add_argument("--checkpoint-interval", type=int, default=50,
                        help="Save checkpoint every N rows (default: 50)")
    args = parser.parse_args()
    
    logger = setup_logging()
    
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY not set. Create a .env file.")
        return
    
    logger.info("=" * 60)
    logger.info("FULL BDC FOOTNOTES PIPELINE")
    logger.info(f"Memory file: {MEMORY_FILE}")
    logger.info(f"Output file: {OUTPUT_FILE}")
    logger.info("=" * 60)
    
    # Load memory
    memory = load_extraction_memory()
    logger.info(f"Loaded memory: {len(memory.get('page_header_patterns', []))} patterns, "
                f"{len(memory.get('known_fixes', []))} fixes, "
                f"{len(memory.get('learnings', []))} learnings")
    
    # Phase 1: Deterministic Extraction (or load checkpoint)
    if args.skip_extraction and os.path.exists(CHECKPOINT_FILE):
        logger.info(f"Loading from checkpoint: {CHECKPOINT_FILE}")
        df = pd.read_csv(CHECKPOINT_FILE, low_memory=False)
    else:
        results = extract_all_filings(logger, memory)
        df = pd.DataFrame(results)
        # Save initial extraction
        df.to_csv(CHECKPOINT_FILE, index=False)
        logger.info(f"Saved initial extraction to {CHECKPOINT_FILE}")
    
    logger.info(f"DataFrame: {len(df)} rows, {len(df.columns)} columns")
    
    # Phase 2: Agentic QA Review
    df = run_agentic_qa(df, logger, checkpoint_interval=args.checkpoint_interval)
    
    # Save final output
    df.to_csv(OUTPUT_FILE, index=False)
    logger.info(f"Saved final output to {OUTPUT_FILE}")
    
    # Flush Langfuse
    flush_langfuse()
    logger.info("Done.")


if __name__ == "__main__":
    main()
