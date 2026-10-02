"""BDC Footnote Tape: any BDC 10-K or 10-Q in, one row per position with its footnotes out.

Streamlit Community Cloud entrypoint: demo/app.py (requirements in demo/requirements.txt).
Run locally from the repo root:  streamlit run demo/app.py

Rules-only. No language model is called, so a visit costs nothing beyond SEC bandwidth.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
for p in (DEMO_DIR, DEMO_DIR.parent):          # demo modules, then the repo's parsers/
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import edgar  # noqa: E402
import pipeline  # noqa: E402

SESSION_RUNS = 10   # filings per visitor session: keeps SEC traffic and server memory bounded
SAMPLE_PATH = DEMO_DIR / "fixtures" / "sample_filing.html"
REPO_URL = "https://github.com/alexandrecela10/BDC-Non-Accrual-Footnotes-extraction"

# Four real filings from different BDCs, tested end to end (see demo/README.md).
EXAMPLES = {
    "Ares Capital 10-K, period 2025-12-31": "https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm",
    "Main Street Capital 10-Q, period 2026-06-30": "https://www.sec.gov/Archives/edgar/data/1396440/000139644026000094/main-20260630.htm",
    "Golub Capital BDC 10-Q, period 2026-06-30": "https://www.sec.gov/Archives/edgar/data/1476765/000147676526000048/gbdc-20260630.htm",
    "BlackRock TCP Capital 10-Q, period 2026-06-30 (lettered footnotes)": "https://www.sec.gov/Archives/edgar/data/1370755/000119312526336794/tcpc-20260630.htm",
}

st.set_page_config(page_title="BDC Footnote Tape", layout="wide")
st.title("BDC Footnote Tape")
st.caption("Any BDC 10-K or 10-Q in. One row per position, with its footnotes resolved, out. "
           "Rules only: no language model is called.")

with st.sidebar:
    st.subheader("About")
    st.write("A BDC (business development company) is a listed lender that files its loan book with the SEC "
             "as a Schedule of Investments. This demo turns that schedule into a standard table.")
    st.write(f"Filing size cap: {edgar.MAX_FILING_MB} MB. Runs per visit: {SESSION_RUNS}. "
             f"SEC requests: at most {edgar.MAX_REQUESTS_PER_SECOND} per second, cached.")
    st.write("Output from real filings is **public SEC data**. Tags such as non-accrual are keyword matches "
             "on footnote text, not confirmed designations.")
    st.markdown(f"[Code]({REPO_URL})")

st.session_state.setdefault("runs", 0)

# ---------------------------------------------------------------------------
# Entry: pick a filing
# ---------------------------------------------------------------------------
st.header("1. Pick a filing")
mode = st.radio("Input", ["Example filing", "SEC URL or accession number", "Ticker or CIK",
                          "Upload HTML", "Offline sample (synthetic)"], horizontal=True)

choice = None   # ("url", text, cik_hint) | ("ref", FilingRef) | ("bytes", data, name) | ("sample",)
if mode == "Example filing":
    label = st.selectbox("Filing (public SEC data)", list(EXAMPLES))
    choice = ("url", EXAMPLES[label], "")
elif mode == "SEC URL or accession number":
    text = st.text_input("Filing URL or accession number",
                         placeholder="https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>.htm")
    cik_hint = st.text_input("CIK (only needed for an accession filed by an agent)", value="")
    if text.strip():
        choice = ("url", text, cik_hint)
elif mode == "Ticker or CIK":
    who = st.text_input("Ticker or CIK", placeholder="e.g. MAIN or 1396440")
    if who.strip():
        try:
            cik = int(who) if who.strip().isdigit() else edgar.ticker_to_cik(who)
            if cik is None:
                st.error("Ticker not found in SEC's ticker list. Try the CIK.")
            else:
                name, filings = edgar.list_filings(cik)
                if not filings:
                    st.warning("No 10-K or 10-Q in this filer's recent submissions.")
                else:
                    labels = [f"{f.form} period {f.report_date or '?'} filed {f.filing_date} ({f.accession})" for f in filings]
                    pick = st.selectbox(f"{name}: recent filings (public SEC data)", labels)
                    choice = ("ref", filings[labels.index(pick)])
        except edgar.EdgarError as exc:
            st.error(str(exc))
elif mode == "Upload HTML":
    up = st.file_uploader("Primary document of a 10-K or 10-Q (.htm)", type=["htm", "html"])
    if up is not None:
        data = up.getvalue()
        if len(data) > edgar.MAX_FILING_MB * 1024 * 1024:
            st.error(f"File is {len(data) / 1e6:.1f} MB; the cap is {edgar.MAX_FILING_MB} MB.")
        else:
            choice = ("bytes", data, up.name)
else:
    st.info("A five-row synthetic schedule with fictional companies. Two defects are planted so QA has "
            "something to catch: one undefined marker and one unreadable rate cell.")
    choice = ("sample",)

go = st.button("Run", type="primary", disabled=choice is None)


def fetch(choice) -> tuple[bytes, dict]:
    """Step 1: bytes plus filing metadata."""
    t = time.monotonic()
    if choice[0] == "sample":
        return SAMPLE_PATH.read_bytes(), {"source": "synthetic sample", "company": "Example BDC Corp",
                                          "form": "10-Q", "report_date": "2025-12-31", "fetch_seconds": 0.0}
    if choice[0] == "bytes":
        return choice[1], {"source": f"upload ({choice[2]})", "fetch_seconds": 0.0}
    ref = choice[1] if choice[0] == "ref" else edgar.resolve(choice[1], choice[2])
    raw = edgar.get(ref.document_url, max_mb=edgar.MAX_FILING_MB)
    return raw, {"cik": ref.cik, "accession": ref.accession, "form": ref.form, "report_date": ref.report_date,
                 "company": ref.company, "source": "SEC EDGAR", "document_url": ref.document_url,
                 "index_url": ref.index_url, "fetch_seconds": round(time.monotonic() - t, 2)}


if go:
    if st.session_state.runs >= SESSION_RUNS:
        st.error(f"Demo limit: {SESSION_RUNS} filings per visit. Reload the page later to start a new visit.")
    else:
        st.session_state.runs += 1
        st.header("2. Pipeline")
        with st.status("Running", expanded=True) as status:
            try:
                status.update(label="1 · Fetch")
                raw, meta = fetch(choice)

                def show(step):
                    icon = "✅" if step.ok else "⚠️"
                    st.write(f"{icon} **{step.name}** ({step.seconds:.2f} s): {step.summary}")

                t0 = time.monotonic()
                result = pipeline.run(raw, meta, on_step=show)
                result.meta["pipeline_seconds"] = round(time.monotonic() - t0, 2)
                st.session_state.result = result
                status.update(label=f"Done in {meta.get('fetch_seconds', 0) + result.meta['pipeline_seconds']:.1f} s",
                              state="complete")
            except edgar.EdgarError as exc:
                status.update(label="Stopped", state="error")
                st.error(str(exc))
            except Exception as exc:  # noqa: BLE001 the visitor sees a message, not a traceback
                status.update(label="Stopped", state="error")
                st.error(f"Could not parse this filing: {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# Exit: QA, tables, downloads
# ---------------------------------------------------------------------------
res = st.session_state.get("result")
if res is not None:
    meta, qa = res.meta, res.qa
    st.header("3. Result")
    who = meta.get("company") or "Uploaded filing"
    label = "synthetic sample" if meta.get("source") == "synthetic sample" else "public SEC data"
    st.write(f"**{who}** · {meta.get('form', '')} · period {meta.get('period_end', '?')} · "
             f"accession {meta.get('accession', 'n/a')} · _{label}_")
    if meta.get("index_url"):
        st.markdown(f"[Open the filing on SEC EDGAR]({meta['index_url']})")

    if qa.get("status") != "ok":
        st.warning("No Schedule of Investments section with a date was found in this document.")
    else:
        st.subheader("QA")
        c = st.columns(5)
        c[0].metric("Positions", qa["rows"], help="One row per position in the parsed schedule.")
        recon = qa["fair_value_delta_pct"]
        c[1].metric("Fair value vs filing total", "no total row" if recon is None else f"{recon:+.2f}%",
                    help=f"Sum of parsed fair value vs the filing's own total row. Pass within {qa['tolerance_pct']}%.")
        c[2].metric("Markers resolved", f"{qa['markers_resolved']} of {qa['markers_distinct']}")
        c[3].metric("Footnotes found", qa["footnotes_defined"])
        c[4].metric("Non-accrual candidates", qa["rows_non_accrual_candidate"],
                    help="Rows whose footnote text mentions non-accrual or default (keyword match).")
        checks = pd.DataFrame([
            ("At least one position parsed", qa["rows"] > 0,
             "0 rows usually means a table layout the parser doesn't know (e.g. no company column)"
             if qa["rows"] == 0 else f"{qa['rows']} rows"),
            ("Fair value reconciles to the filing's total row", qa["fair_value_reconciles"],
             f"parsed {qa['sum_fair_value']:,.0f} vs filed {qa['filing_total_fair_value'] or 0:,.0f}"),
            ("Cost reconciles to the filing's total row", qa["cost_reconciles"],
             f"parsed {qa['sum_cost']:,.0f} vs filed {qa['filing_total_cost'] or 0:,.0f}"),
            ("Every marker has a definition", not qa["markers_unresolved"],
             ", ".join(qa["markers_unresolved"]) or "none unresolved"),
            ("Every row has a fair value", qa["rows_missing_fair_value"] == 0, f"{qa['rows_missing_fair_value']} missing"),
            ("Every debt rate cell parsed", qa["debt_rows_rate_parse_failed"] == 0,
             f"{qa['debt_rows_rate_parse_failed']} of {qa['rows_debt']} debt rows failed"),
        ], columns=["Check", "Pass", "Detail"])
        checks["Pass"] = checks["Pass"].map({True: "pass", False: "fail", None: "n/a"})
        st.dataframe(checks, hide_index=True, width="stretch")
        st.caption(f"Footnotes defined but not referenced by any row: {', '.join(qa['footnotes_unreferenced']) or 'none'}. "
                   f"Inline XBRL fair-value facts for this date: {qa['xbrl_fair_value_facts']} (includes subtotals; "
                   f"shown for context, not a check). Footnote method: {qa['footnote_method']}.")

        st.subheader("Positions")
        view = res.positions
        only_na = st.checkbox("Only non-accrual candidates")
        if only_na:
            view = view[view.fn_non_accrual]
        st.dataframe(view, hide_index=True, width="stretch", height=360)

        st.subheader("Footnotes")
        st.dataframe(res.footnotes, hide_index=True, width="stretch", height=260)

        st.subheader("One position, traced")
        if len(res.positions):
            opts = [f"{r.row_id} · {r.issuer} · {r.instrument}" for r in res.positions.itertuples()]
            pick = st.selectbox("Position", opts)
            row = res.positions.iloc[opts.index(pick)]
            st.write({k: row[k] for k in ("issuer", "industry", "instrument", "reference_rate", "spread_bps",
                                          "maturity", "par", "cost", "fair_value", "footnote_markers")})
            for part in filter(None, str(row["footnote_texts"]).split(" || ")):
                st.markdown(f"- {part}")
            if row["unresolved_markers"]:
                st.warning(f"Unresolved markers: {row['unresolved_markers']}")

    st.header("4. Download")
    stem = (meta.get("accession") or "filing").replace("/", "-")
    d = st.columns(4)
    d[0].download_button("Positions CSV", res.positions_csv(), f"{stem}_positions.csv", "text/csv")
    d[1].download_button("Footnotes CSV", res.footnotes_csv(), f"{stem}_footnotes.csv", "text/csv")
    d[2].download_button("Everything JSON", res.to_json(), f"{stem}.json", "application/json")
    d[3].download_button("Data dictionary CSV", pipeline.data_dictionary_csv(), "data_dictionary.csv", "text/csv")
    with st.expander("Data dictionary"):
        st.dataframe(pd.DataFrame(pipeline.DATA_DICTIONARY, columns=["column", "type", "definition"]),
                     hide_index=True, width="stretch")
