# BDC Non-Accrual Footnotes Extraction

Automated extraction of non-accrual footnotes from SEC 10-K/10-Q filings for Business Development Companies (BDCs).

## Architecture

This pipeline uses a **deterministic-first approach** with an **AI reviewer backup** to ensure completeness:

```
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 1: DETERMINISTIC                       │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────────────┐  │
│  │ Download │───▶│ Parse HTML   │───▶│ Extract Footnotes     │  │
│  │ SEC EDGAR│    │ Find Schedule│    │ via Pattern Matching  │  │
│  └──────────┘    └──────────────┘    └───────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 2: AI REVIEWER                         │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐   │
│  │ Completeness │───▶│ Accuracy     │───▶│ LLM Correction   │   │
│  │ Check        │    │ Check        │    │ (if needed)      │   │
│  └──────────────┘    └──────────────┘    └──────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Why Deterministic First?

1. **Speed** — Pattern matching is 100x faster than LLM calls
2. **Cost** — No API costs for the majority of extractions
3. **Reproducibility** — Same input always produces same output
4. **Auditability** — Clear rules that can be inspected and debugged

### AI Reviewer as Backup

The deterministic extractor handles ~95% of cases correctly. For the remaining edge cases, Gemini 2.5 Flash reviews each extraction:

- **Completeness Check** — Are all footnote markers present?
- **Accuracy Check** — Is the extracted text complete (not truncated)?
- **LLM Correction** — If checks fail, the LLM re-extracts from raw HTML

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up environment variables
cp .env.example .env
# Edit .env with your GEMINI_API_KEY

# 3. Download SEC filings
python download_bdc_filings.py

# 4. Run full pipeline
python run_full_pipeline.py
```

## Output

The pipeline produces a CSV with extracted footnotes for each filing:

| Column | Description |
|--------|-------------|
| `cik` | Company identifier |
| `file_code` | SEC accession number |
| `as_at_date` | Filing period date |
| `extraction_method` | `deterministic` or `llm` |
| `qa_passed` | Whether QA checks passed |
| `footnote_1` ... `footnote_50` | Extracted footnote text |

## Project Structure

```
├── download_bdc_filings.py   # Download 10-K/10-Q from SEC EDGAR
├── extract_footnotes.py      # Deterministic footnote extraction
├── run_full_pipeline.py      # Orchestrates full pipeline
├── parsers/
│   └── html_parser.py        # HTML parsing and section detection
├── qa_reviewer/
│   ├── reviewer.py           # LLM-based QA checks
│   ├── self_correct.py       # LLM correction when QA fails
│   └── tracing.py            # Langfuse observability
└── output/                   # Extracted data
```

## Configuration

Update `download_bdc_filings.py` with your SEC identification:

```python
COMPANY_NAME = "Your Name"
EMAIL = "your.email@example.com"
```

Add BDCs by CIK number:

```python
BDC_CIKS = [
    "1655888",  # Blue Owl Capital Corporation
    "1370755",  # BlackRock TCP Capital Corp
]
```

## Requirements

- Python 3.9+
- Gemini API key (for AI reviewer phase)
- ~100GB disk space for SEC filings

---

## Known Limitations

> ⚠️ **Work in Progress**: The current output may include some false positive non-accruals (e.g., footnotes that reference non-accrual status but aren't actual non-accrual designations). Ongoing work is focused on improving the filtering logic to ensure only true non-accrual footnotes are extracted.
