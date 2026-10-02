"""Tests for the BDC Footnote Tape demo.

Offline by default. Set BDC_DEMO_LIVE=1 to also run one real filing from SEC EDGAR.
Run from the repo root:  pytest demo/test_demo.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO))
sys.path.insert(1, str(DEMO.parent))

import edgar  # noqa: E402
import footnotes as fn  # noqa: E402
import pipeline  # noqa: E402
from vendor_parallax.tables import Mapping, row_markers  # noqa: E402

SAMPLE = (DEMO / "fixtures" / "sample_filing.html").read_bytes()


@pytest.fixture(scope="module")
def result():
    return pipeline.run(SAMPLE, {"company": "Example BDC Corp", "report_date": "2025-12-31"})


def test_every_step_reports(result):
    assert [s.name.split(" ")[0] for s in result.steps] == ["1", "2", "3", "4", "5", "6", "7"]


def test_one_row_per_position_with_dollars(result):
    p = result.positions
    assert len(p) == 5
    assert list(p.columns) == pipeline.COLUMNS
    first = p.iloc[0]
    assert first.issuer == "Alpha Example Holdings, LLC"
    assert first.par == 10_000_000 and first.cost == 9_900_000 and first.fair_value == 9_950_000
    assert first.reference_rate == "SOFR" and first.spread_bps == 525
    assert p.iloc[1].issuer == "Alpha Example Holdings, LLC"          # tranche row inherits the issuer
    assert p.iloc[3].instrument_bucket == "equity" and pd_isna(p.iloc[3].par)


def pd_isna(x):
    return x is None or x != x


def test_markers_resolve_to_footnote_text(result):
    p = result.positions
    beta = p[p.issuer == "Beta Example Parent, Inc."].iloc[0]
    assert beta.footnote_markers == "1,2"
    assert "non-accrual status" in beta.footnote_texts
    assert bool(beta.fn_non_accrual) and bool(beta.fn_restricted)
    assert int(p.fn_non_accrual.sum()) == 1


def test_qa_catches_planted_defects(result):
    qa = result.qa
    assert qa["fair_value_reconciles"] is True and qa["fair_value_delta_pct"] == 0.0
    assert qa["cost_reconciles"] is True
    assert qa["markers_unresolved"] == ["9"]                           # planted: no definition for (9)
    assert qa["debt_rows_rate_parse_failed"] == 1                      # planted: "Fixed" + "11.00% PIK"
    assert qa["footnotes_defined"] == 4


def test_exports(result):
    data = json.loads(result.to_json())
    assert data["meta"]["label"] == "public SEC data"
    assert len(data["positions"]) == 5 and len(data["footnotes"]) == 4
    assert result.positions_csv().splitlines()[0].startswith("filer_name,cik,accession")
    dd = pipeline.data_dictionary_csv().splitlines()
    assert len(dd) == len(pipeline.COLUMNS) + 1                        # every column is defined
    assert {r[0] for r in pipeline.DATA_DICTIONARY} == set(pipeline.COLUMNS)


def test_negative_amount_is_not_a_marker():
    # header: Company | Investment | Cost | Fair Value; "(71)" in fair value is -71, not marker 71
    m = Mapping(header_row=0, width=4, groups={"company": [0], "investment": [1], "cost": [2], "fair_value": [3]},
                fields_present={"company", "investment", "cost", "fair_value"})
    assert row_markers(["Example Co (3)", "Revolver", "(40)", "(71)"], m) == ["3"]


def test_footnote_tags_and_cleanup():
    assert fn.tag_footnote("Loan was on non-accrual status as of June 30, 2026.") == ["non_accrual"]
    assert "affiliate_or_control" not in fn.tag_footnote("Represents co-investment made with the Company's affiliates.")
    assert fn.strip_page_header("Non-income producing. Example BDC Corp (Unaudited)", "Example BDC Corp") == "Non-income producing."
    text, cut = fn.trim_runaway("Yield is 3.4%. Table of contents Item 2. MANAGEMENT'S DISCUSSION")
    assert cut and text == "Yield is 3.4%."


def test_edgar_inputs_parse_without_network():
    assert edgar.normalise_accession("0001396440-26-000094") == "0001396440-26-000094"
    assert edgar.normalise_accession("000139644026000094") == "0001396440-26-000094"
    assert edgar.USER_AGENT == "Alexandre Cela alexandrecelap@gmail.com"
    assert edgar.MAX_REQUESTS_PER_SECOND < 10


def test_unknown_layout_returns_zero_rows_not_a_crash():
    html = (b"<html><body><p>Consolidated Schedule of Investments</p><p>As of December 31, 2025</p>"
            b"<table><tr><td>Investments</td><td>Par</td><td>Cost</td><td>Fair Value</td></tr>"
            b"<tr><td>Example Co</td><td>1</td><td>1</td><td>1</td></tr></table></body></html>")
    r = pipeline.run(html, {})
    assert r.qa["rows"] == 0 and r.qa["fair_value_reconciles"] is None
    assert list(r.positions.columns) == pipeline.COLUMNS


def test_no_llm_client_is_imported():
    for mod in list(sys.modules):
        assert not mod.startswith(("google.genai", "google.generativeai", "qa_reviewer")), mod


def test_app_renders_and_runs_offline_sample():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(DEMO / "app.py"), default_timeout=60).run()
    assert not at.exception
    at.radio[0].set_value("Made-up sample (works offline)").run()
    at.button[0].click().run()
    assert not at.exception
    assert any(m.label == "Loans and holdings" and m.value == "5" for m in at.metric)
    assert any(m.label == "Footnote marks explained" and m.value == "4 of 5" for m in at.metric)


@pytest.mark.skipif(os.environ.get("BDC_DEMO_LIVE") != "1", reason="set BDC_DEMO_LIVE=1 to hit SEC EDGAR")
def test_live_filing_reconciles():
    ref = edgar.resolve("https://www.sec.gov/Archives/edgar/data/1396440/000139644026000094/main-20260630.htm")
    raw = edgar.get(ref.document_url, max_mb=edgar.MAX_FILING_MB)
    r = pipeline.run(raw, {"cik": ref.cik, "accession": ref.accession, "form": ref.form,
                           "report_date": ref.report_date, "company": ref.company})
    assert r.qa["rows"] > 100
    assert r.qa["fair_value_reconciles"] is True
