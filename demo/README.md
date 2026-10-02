# BDC Footnote Tape (demo)

Any BDC 10-K or 10-Q in. One row per Schedule of Investments position, with every footnote marker resolved to its text, out. Rules only: no language model is called.

## Run it

From the repository root:

```bash
python3.11 -m venv .venv-demo            # any Python 3.10+; the repo's own venv is 3.9
.venv-demo/bin/pip install -r demo/requirements.txt
.venv-demo/bin/streamlit run demo/app.py
```

Open http://localhost:8501. Tests (offline by default, `BDC_DEMO_LIVE=1` adds one real SEC filing):

```bash
.venv-demo/bin/python -m pytest demo/test_demo.py -q -p no:cacheprovider
```

**Streamlit Community Cloud:** main file `demo/app.py`. Cloud reads `demo/requirements.txt` because it sits next to the entrypoint. No secrets are needed.

## Steps shown in the app

| Step | What happens | Code |
|---|---|---|
| 1 Fetch | SEC URL, accession (+ CIK), ticker/CIK picker, upload, or a synthetic sample | `edgar.py` |
| 2 Locate | Dated Schedule of Investments sections; the EDGAR period is picked, else the latest | `vendor_parallax/section.py` |
| 3 Parse | Every table in the section; headers mapped by token; headerless continuation pages inherit the mapping | `vendor_parallax/tables.py`, `soi_parse.py` |
| 4 Standardise | One row per position; money scaled to dollars; rate split into reference, spread, all-in, PIK, floor | `soi_parse.py`, `vendor_parallax/rates.py` |
| 5 Footnotes | Marker -> definition text with the repo's own extractor | `footnotes.py` -> `parsers/html_parser.py`, `parsers/footnote_extractor.py` |
| 6 Resolve | Each row's markers -> text and keyword tags (non-accrual uses `parsers/non_accrual_tagger.py`) | `pipeline.py`, `footnotes.py` |
| 7 QA | Row counts, fair value and cost vs the filing's own total row (pass within 1%), unresolved markers, rate parse failures | `pipeline.py` |
| 8 Export | Positions CSV, footnotes CSV, JSON, data dictionary | `pipeline.py`, `app.py` |

## SEC EDGAR etiquette

- User-Agent `Alexandre Cela alexandrecelap@gmail.com` on every request.
- At most 5 requests per second per process (SEC allows 10).
- Every response is cached on disk (system temp folder), keyed by URL.
- **25 MiB filing cap.** Corpus of 1,805 primary documents in this repo: median 6.1 MB, 95th percentile 21.1 MB, 99th 32.5 MB; 97.5% are at or under 25 MiB. Observed peak process memory: 667 MB on a 24.5 MB filing, 804 MB on a 32.5 MB filing. Community Cloud guarantees about 690 MB.
- 10 filings per visitor session.

## Observed runs (public SEC data, 2026-10-01, local Mac, Python 3.11)

Command: `python demo/run_live.py <TICKER> <FORM>`, cold cache. Full output in `observed_runs.txt`.

| Filing | Size | Positions | Fair value vs filed total | Footnotes | Markers resolved | Non-accrual candidate rows | Time |
|---|---|---|---|---|---|---|---|
| Ares Capital 10-K, 2025-12-31 | 24.5 MB | 1,409 | -0.48% (pass) | 18 | 12 of 12 | 19 | 11.5 s |
| Main Street Capital 10-Q, 2026-06-30 | 13.4 MB | 619 | 0.00% (pass) | 36 | 21 of 21 | 33 | 7.7 s |
| Golub Capital BDC 10-Q, 2026-06-30 | 24.6 MB | 1,812 | 0.00% (pass) | 41 | 31 of 31 | 45 (8 from a "gross reductions" note) | 12.0 s |
| BlackRock TCP Capital 10-Q, 2026-06-30 | 18.9 MB | 319 | 0.00% (pass) | 15 | 12 of 12 | 0 | 7.0 s |

Footnote extraction (BeautifulSoup) took 69% to 72% of each run's wall time. One failure observed: a Blackstone Private Credit Fund 10-K (32.5 MB, local copy, above the cap) parsed 0 positions because its table has no company column.

## Where the code comes from

- `vendor_parallax/` is copied from `~/Documents/Parallax/src/parallax/soi/` (commit 0ab5eaa). Every change is marked `DEMO CHANGE`: upper-case markers, a bare-marker "Notes" column, a "Ref" header, a "Total Investments - x% of Net Assets" total row, and negative amounts in money columns not read as markers.
- `soi_parse.py` adapts `parallax/parse.py` (`parse_filing`, `instrument_bucket`, `xbrl_fair_value_tags`).
- `footnotes.py` calls this repo's extractor unchanged and adds three guards: clip a section at the next dated section, strip the filer's running page header, cut a footnote that runs into the notes or MD&A.
- `non_accrual_rules.py` is a copy of `classify_text`, used only if `parsers/non_accrual_tagger.py` is missing (it is untracked in git).

## Known limits

- Layouts without a company column (issuer printed in an "Investments" column) parse 0 rows. QA shows "At least one position parsed: fail".
- Symbol markers (`*`, `^`, `#`, `†`) are not attached to rows.
- Tags are keyword matches on footnote text. `fn_non_accrual` can fire on a footnote that only mentions non-accrual (e.g. an affiliate table's "gross reductions" note).
- Accuracy against hand-labelled filings is unmeasured.
