"""Live end-to-end run from the command line: ticker -> latest 10-K or 10-Q -> pipeline.

Usage (repo root):  python demo/run_live.py MAIN 10-Q
Prints each step, the QA dict and the SEC requests made. demo/observed_runs.txt is the
output of this script for ARCC 10-K, MAIN 10-Q, GBDC 10-Q and TCPC 10-Q on 2026-10-01.
"""
import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar  # noqa: E402
import pipeline  # noqa: E402

ticker, form = sys.argv[1], sys.argv[2]
t0 = time.monotonic()
cik = edgar.ticker_to_cik(ticker)
name, filings = edgar.list_filings(cik)
ref = next(f for f in filings if f.form == form)
raw = edgar.get(ref.document_url, max_mb=edgar.MAX_FILING_MB)
meta = {"cik": ref.cik, "accession": ref.accession, "form": ref.form, "report_date": ref.report_date,
        "company": name, "source": "SEC EDGAR", "fetch_seconds": round(time.monotonic() - t0, 2),
        "document_url": ref.document_url}
r = pipeline.run(raw, meta)
print(ticker, name, ref.accession, ref.form, ref.report_date, ref.document_url)
# ru_maxrss is bytes on macOS, kilobytes on Linux
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print("total s", round(time.monotonic() - t0, 1), "peak RSS MB", round(rss / 1e6 if sys.platform == "darwin" else rss / 1e3))
for s in r.steps:
    print(" ", s.name, s.seconds, s.summary)
print(json.dumps({k: v for k, v in r.qa.items() if k != "footnotes_unreferenced"}, default=str))
print("requests:", [(x["status"], x["bytes"]) for x in edgar.REQUEST_LOG])
