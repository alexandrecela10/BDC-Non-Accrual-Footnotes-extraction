"""
Data models for the BDC footnote extraction pipeline.

These dataclasses define the structure of data as it flows through the pipeline:
1. FilingMetadata — identifies a filing (which company, which document, where on disk)
2. FootnoteResult — holds the extracted footnotes for one schedule-of-investments date
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict


@dataclass
class FilingMetadata:
    """
    Identifies a single SEC filing on disk.
    
    Think of this as a "label" for a filing — it tells us:
    - Which company filed it (cik)
    - What kind of filing it is (filing_type: 10-K or 10-Q)
    - The accession number (unique SEC document ID)
    - Where the HTML file lives on disk (html_path)
    """
    cik: str                # Central Index Key — SEC's company identifier
    filing_type: str        # "10-K" or "10-Q"
    accession: str          # Unique SEC document ID (e.g., "0001655888-26-000010")
    html_path: str          # Full path to the primary-document.html file


@dataclass
class FootnoteResult:
    """
    Holds the extracted footnotes for one schedule-of-investments date.
    
    A single filing can produce multiple FootnoteResults if it contains
    schedules for multiple dates (e.g., a 10-K with Dec 31 2025 + Dec 31 2024).
    
    Fields:
    - cik: which company
    - file_code: accession number (links back to the source document)
    - as_at_date: the date of the schedule ("As of December 31, 2025")
    - footnotes: ordered dict mapping marker -> definition text
      e.g., {"1": "Certain portfolio company...", "2": "The amortized cost..."}
    - extraction_method: "deterministic" or "llm" (for future fallback)
    """
    cik: str
    file_code: str          # Accession number
    as_at_date: str         # Date string like "December 31, 2025"
    html_path: str = ""     # Path to source HTML file for traceability
    footnotes: Dict[str, str] = field(default_factory=dict)
    extraction_method: str = "deterministic"
    marker_type: str = ""   # 'numeric', 'alpha', 'star', or 'mixed'
    first_marker_valid: bool = True  # False if first marker isn't 1/A/a
    # QA tracking flags — flow into the CSV output
    qa_reviewed: bool = False       # Was this section QA-reviewed at all?
    qa_passed_first: bool = False   # Did it pass the initial QA review?
    fixer_applied: bool = False     # Did the fixer agent make corrections?
    qa_reviewed_twice: bool = False # Was it re-reviewed after fixer corrections?
    qa_passed_final: bool = False   # Did it pass the re-QA (or first QA if no fixer)?
