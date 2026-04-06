# BDC Footnotes Extraction — Agentic Workflow Diagram

## High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FILING DISCOVERY & PROCESSING                        │
│                                                                               │
│  Input: Base path to filings directory                                       │
│  Output: List of FilingMetadata objects (cik, filing_type, accession, path)  │
│  Success: ≥1 filing discovered                                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                    DETERMINISTIC FOOTNOTE EXTRACTION                         │
│                                                                               │
│  Input: HTML file (primary-document.html)                                    │
│  Process:                                                                     │
│    1. Parse HTML → find "Consolidated Schedule of Investments" sections      │
│    2. Group sections by as_at_date (handle repeated page headers)            │
│    3. Extract footnote markers & definitions from each section               │
│    4. Auto-load learned skip patterns from fixer_memory.json                 │
│                                                                               │
│  Output: FootnoteResult {                                                    │
│    cik, file_code, as_at_date,                                              │
│    footnotes: {marker → text},                                              │
│    marker_type, first_marker_valid,                                         │
│    extraction_method: "deterministic"                                        │
│  }                                                                            │
│                                                                               │
│  Success Criteria:                                                            │
│    ✓ Found ≥1 schedule section                                              │
│    ✓ Extracted ≥1 footnote marker                                           │
│    ✓ Each footnote text is ≥8 characters                                    │
│    ✓ First marker is valid (1/A/a or *)                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ↓
                          [Optional: --qa flag]
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                      FILE-LEVEL LANGFUSE SPAN                                │
│                      (filing_processing)                                     │
│                                                                               │
│  Input: All FootnoteResult objects for a file_code                           │
│  Tracks: cik, file_code, num_sections, as_at_dates                          │
│  Closes with: date_coverage score (1.0 if ≥2 dates, 0.0 if <2)             │
│  Output: Aggregated metrics per filing                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                         QA REVIEW LAYER 1                                    │
│                    (Section Boundary Verification)                           │
│                                                                               │
│  Input: SectionEvidence {                                                    │
│    section_start_text (first ~500 chars),                                   │
│    section_end_text (last ~500 chars),                                      │
│    extracted_markers, footnote_area_text                                    │
│  }                                                                            │
│                                                                               │
│  LLM Call: Gemini 2.5 Flash                                                 │
│  Prompt: "Is this the Consolidated Schedule of Investments section?"        │
│  Response: {pass: bool, reasoning: str}                                     │
│                                                                               │
│  Langfuse Span: layer1_section_boundary                                     │
│    Input: prompt text                                                        │
│    Output: response JSON                                                     │
│                                                                               │
│  Success Criteria:                                                            │
│    ✓ START contains "Consolidated Schedule of Investments" + date           │
│    ✓ START has column headers (Company, Investment, Cost, Fair Value, etc)  │
│    ✓ END contains "See accompanying notes" or final footnote                │
│    ✓ Verdict.passed = true                                                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                    QA REVIEW LAYERS 2+3 (MERGED)                             │
│              (Completeness + Accuracy in Single LLM Call)                    │
│                                                                               │
│  Input: SectionEvidence {                                                    │
│    extracted_markers,                                                        │
│    footnote_area_text (full raw text, ~15K chars),                          │
│    all extracted footnote definitions                                        │
│  }                                                                            │
│                                                                               │
│  LLM Call: Gemini 2.5 Flash (single call, saves ~3,750 tokens)             │
│  Prompts:                                                                     │
│    Layer 2: "Are all footnote markers present and matched?"                 │
│    Layer 3: "Are footnote definitions complete and accurate?"               │
│  Response: Two LayerVerdict objects                                         │
│                                                                               │
│  Langfuse Span: layer2_3_completeness_accuracy                              │
│    Input: prompt text (includes footnote_area_text once)                    │
│    Output: response JSON with both verdicts                                 │
│                                                                               │
│  Success Criteria (Layer 2 — Completeness):                                 │
│    ✓ All markers in raw text are extracted                                 │
│    ✓ No missing_markers list                                                │
│    ✓ Verdict.passed = true                                                 │
│                                                                               │
│  Success Criteria (Layer 3 — Accuracy):                                     │
│    ✓ No truncated footnotes (text cut off mid-sentence)                    │
│    ✓ No contaminated footnotes (page headers/footers mixed in)             │
│    ✓ No wrong_text (dropped XBRL values, incorrect definitions)            │
│    ✓ Verdict.passed = true                                                 │
│                                                                               │
│  Parent Span: qa_review                                                      │
│    Input: cik, file_code, as_at_date, extracted_markers, section previews   │
│    Output: Full QAVerdict with all layer results + scores                   │
│    Scores: completeness, truncation, overall_quality                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ↓
                        [If Layer 1, 2, or 3 fails]
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                      FIXER AGENT (Stage 1)                                   │
│                 (Targeted Surgical Repairs)                                  │
│                                                                               │
│  Input: FixRequest {                                                         │
│    issues: [{marker, issue_type, detail}],  # from QA verdict               │
│    missing_markers: [list],                  # from completeness layer       │
│    extracted_footnotes: {marker → text},     # current state                │
│    footnote_area_text: str                   # raw evidence                 │
│  }                                                                            │
│                                                                               │
│  Strategy 1: Strip Page Headers (Deterministic)                             │
│    ├─ Input: contaminated markers, known_header_patterns from memory        │
│    ├─ Process: Regex match & remove page headers/footers                    │
│    ├─ Output: cleaned footnote text                                         │
│    ├─ Langfuse Span: fix_strip_page_headers                                 │
│    └─ Success: was_modified = true                                          │
│                                                                               │
│  Strategy 2: Reclassify Sub-Markers (Deterministic)                         │
│    ├─ Input: wrong_marker issues (*, **)                                    │
│    ├─ Process: Move sub-footnotes into parent footnote                      │
│    ├─ Output: updated footnotes dict                                        │
│    ├─ Langfuse Span: fix_reclassify_sub_markers                             │
│    └─ Success: sub-footnote merged into parent                              │
│                                                                               │
│  Strategy 3: Recover Missing XBRL Values (LLM)                              │
│    ├─ Input: wrong_text issues, footnote_area_text                         │
│    ├─ LLM Call: "Find the correct numeric values in raw text"              │
│    ├─ Response: {fixes: [{marker, corrected_text}]}                        │
│    ├─ Langfuse Span: fixer_recover_values (generation)                      │
│    │   Input: prompt with issue descriptions + raw text                     │
│    │   Output: response JSON                                                │
│    └─ Success: corrected_text found and applied                             │
│                                                                               │
│  Strategy 4: Extract Missing Markers (LLM)                                  │
│    ├─ Input: missing_markers list, footnote_area_text                       │
│    ├─ LLM Call: "Find these missing footnote definitions"                  │
│    ├─ Response: {found: [{marker, text}]}                                   │
│    ├─ Langfuse Span: fixer_extract_missing (generation)                     │
│    │   Input: prompt with missing markers + raw text                        │
│    │   Output: response JSON                                                │
│    └─ Success: missing marker text recovered                                │
│                                                                               │
│  Memory Update:                                                               │
│    ├─ Load: output/fixer_memory.json (known_fixes, page_header_patterns)   │
│    ├─ Learn: Add new patterns from successful fixes                         │
│    ├─ Auto-promote: Deterministic extractor loads patterns at import        │
│    └─ Save: Update output/fixer_memory.json                                 │
│                                                                               │
│  Parent Span: fixer_agent                                                    │
│    Input: issues, missing_markers, extracted_markers, num_issues            │
│    Output: FixResult {                                                       │
│      corrected_footnotes: {marker → text},                                  │
│      fixes_applied: [FixAction],                                            │
│      fix_rate: float (0.0-1.0)                                              │
│    }                                                                          │
│    Score: fix_rate (0.0-1.0, where 1.0 = all fixable issues resolved)      │
│                                                                               │
│  Success Criteria:                                                            │
│    ✓ fix_rate > 0 (at least one fix applied)                               │
│    ✓ No truncated issues fixed (those are deferred)                        │
│    ✓ Corrected footnotes are valid (≥8 chars)                              │
│    ✓ Memory updated with new patterns                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ↓
                    [If fixer applied fixes]
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                   TARGETED RE-VERIFY (Stage 2)                               │
│              (Verify Only the Markers the Fixer Touched)                     │
│                                                                               │
│  Input: SectionEvidence {                                                    │
│    fixed_markers: [list of markers fixer touched],                          │
│    corrected_footnotes: {marker → text},                                    │
│    footnote_area_text: str                                                  │
│  }                                                                            │
│                                                                               │
│  LLM Call: Gemini 2.5 Flash (1 call instead of full 2-call re-QA)          │
│  Prompt: "Verify only these specific footnotes are now correct"             │
│  Response: {pass: bool, reasoning: str, issues: [...]}                      │
│                                                                               │
│  Langfuse Span: reqa_fixed_markers (parent)                                 │
│    Input: cik, file_code, fixed_markers, corrected_footnotes                │
│    Output: LayerVerdict result                                              │
│                                                                               │
│  Langfuse Span: verify_fixed_markers (generation)                           │
│    Input: prompt text                                                        │
│    Output: response JSON                                                     │
│                                                                               │
│  Success Criteria:                                                            │
│    ✓ Verdict.passed = true (all fixed markers now correct)                 │
│    ✓ No remaining issues on fixed markers                                   │
│    ✓ Footnotes are complete and not truncated                              │
│                                                                               │
│  Result Flags Updated:                                                       │
│    qa_reviewed_twice = true                                                 │
│    qa_passed_final = verdict.passed                                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                      CSV OUTPUT & SUCCESS METRICS                            │
│                                                                               │
│  Output File: output/bdc_footnotes.csv                                       │
│                                                                               │
│  Columns:                                                                     │
│    ├─ cik, file_code, as_at_date, html_path                                │
│    ├─ extraction_method (deterministic | deterministic+fixer)               │
│    ├─ marker_type (numeric | alpha | star | mixed)                         │
│    ├─ first_marker_valid (true | false)                                    │
│    ├─ num_footnotes                                                         │
│    ├─ qa_reviewed, qa_passed_first, fixer_applied,                         │
│    │  qa_reviewed_twice, qa_passed_final                                    │
│    ├─ footnote_1, footnote_1_marker, footnote_2, footnote_2_marker, ...    │
│    └─ [all extracted footnote text]                                         │
│                                                                               │
│  Success Metrics Logged:                                                     │
│    ├─ Total filings processed                                               │
│    ├─ Total date sub-sections extracted                                     │
│    ├─ Successful sections (with footnotes)                                  │
│    ├─ Failed sections (no footnotes)                                        │
│    ├─ Max footnotes in a single section                                     │
│    ├─ Files meeting date_coverage criteria (≥2 distinct as_at_date)        │
│    └─ Files with insufficient dates (<2)                                    │
│                                                                               │
│  Success Criteria:                                                            │
│    ✓ CSV saved to output/bdc_footnotes.csv                                 │
│    ✓ ≥1 row per filing (at least one section extracted)                    │
│    ✓ All required columns present                                           │
│    ✓ ≥80% of filings have ≥2 distinct as_at_date (date_coverage)           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Detailed Input/Output Contracts

### Stage 1: Deterministic Extraction

| Aspect | Details |
|--------|---------|
| **Input** | `FilingMetadata` with `html_path` to primary-document.html |
| **Processing** | Parse HTML → find schedule sections → extract footnotes |
| **Output** | `FootnoteResult` with `footnotes: {marker → text}` |
| **Success** | ≥1 footnote extracted, first marker valid, no empty definitions |

### Stage 2: QA Review (Layer 1)

| Aspect | Details |
|--------|---------|
| **Input** | `SectionEvidence` with section start/end text + extracted markers |
| **LLM** | Gemini 2.5 Flash, temp=0.0 |
| **Output** | `LayerVerdict` with `passed: bool, reasoning: str` |
| **Success** | `passed = true` (correct section identified) |

### Stage 3: QA Review (Layers 2+3 Merged)

| Aspect | Details |
|--------|---------|
| **Input** | `SectionEvidence` with full footnote_area_text + all definitions |
| **LLM** | Gemini 2.5 Flash, temp=0.0, max_output_tokens=8192 |
| **Output** | Two `LayerVerdict` objects (completeness + accuracy) |
| **Success** | Both `passed = true` (all markers found, no truncation/contamination) |

### Stage 4: Fixer Agent

| Aspect | Details |
|--------|---------|
| **Input** | `FixRequest` with QA issues + current footnotes + raw text |
| **Strategies** | 4 strategies (2 deterministic, 2 LLM) |
| **Output** | `FixResult` with corrected footnotes + fixes applied + fix_rate |
| **Success** | `fix_rate > 0` (at least one issue resolved) |
| **Memory** | Learns patterns, auto-promotes to deterministic extractor |

### Stage 5: Targeted Re-Verify

| Aspect | Details |
|--------|---------|
| **Input** | `SectionEvidence` with only fixed markers + corrected text |
| **LLM** | Gemini 2.5 Flash, temp=0.0 (1 call, not 2) |
| **Output** | `LayerVerdict` for fixed markers only |
| **Success** | `passed = true` (all fixed markers now correct) |

### Stage 6: File-Level Langfuse Tracking

| Aspect | Details |
|--------|---------|
| **Input** | All `FootnoteResult` objects for a `file_code` |
| **Span** | `filing_processing` parent span |
| **Output** | Aggregated metrics: num_distinct_dates, sections_qa_passed, etc |
| **Score** | `date_coverage` (1.0 if ≥2 dates, 0.0 if <2) |
| **Success** | `date_coverage = 1.0` (filing has current + prior period) |

---

## Performance Metrics

### Token Efficiency
- **Deterministic extraction**: 0 tokens (no LLM)
- **QA review**: 2 LLM calls per section (Layer 1 + merged Layer 2+3)
- **Fixer agent**: 0-2 LLM calls (only if QA fails, only for fixable issues)
- **Targeted re-verify**: 1 LLM call (if fixer applied fixes)
- **Total per section**: 2-5 LLM calls (vs 3 before optimization)

### Time Efficiency
- **Deterministic extraction**: ~0.5s per section
- **QA review**: ~10-15s per section (2 LLM calls)
- **Fixer agent**: ~5-10s (if needed, 0-2 calls)
- **Targeted re-verify**: ~5s (if needed, 1 call)
- **Total per section**: ~15-40s

### Projected 84-Filing Run
- **Sections**: ~168 (2 per filing on average)
- **Time**: ~38 minutes (was ~81 min before optimization)
- **LLM calls**: ~714 (was ~1,218 before optimization)
- **Token savings**: ~43%

---

## Error Handling & Fallback

```
QA Layer 1 FAILS
    ↓
[Log error, mark qa_passed_first=false]
    ↓
Try Fixer Agent (if issues detected)
    ├─ Success → Re-verify fixed markers
    └─ Fail → Mark qa_passed_final=false, continue

QA Layer 2 or 3 FAILS
    ↓
[Log specific issues: missing_markers, truncated, contaminated, wrong_text]
    ↓
Try Fixer Agent (if fixable issues exist)
    ├─ Success → Re-verify fixed markers
    └─ Fail → Mark qa_passed_final=false, continue
```

---

## Success Criteria Summary

### Per-Section Success
- ✅ **Extraction**: ≥1 footnote, first marker valid
- ✅ **QA Layer 1**: Section boundary correct
- ✅ **QA Layer 2**: All markers found, none missing
- ✅ **QA Layer 3**: No truncation, contamination, or wrong text
- ✅ **Fixer** (if needed): ≥1 issue fixed, fix_rate > 0
- ✅ **Re-verify** (if fixer applied): All fixed markers now correct

### Per-Filing Success
- ✅ **Date Coverage**: ≥2 distinct `as_at_date` values (current + prior period)
- ✅ **CSV Output**: All sections exported with flags

### Pipeline Success
- ✅ **Overall**: ≥80% of filings have date_coverage = 1.0
- ✅ **Fixer Rate**: ≥90% of non-truncated issues resolved
- ✅ **Token Efficiency**: <50% of baseline token usage
