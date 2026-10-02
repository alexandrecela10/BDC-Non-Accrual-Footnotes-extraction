"""Entry to exit: one BDC 10-K or 10-Q in, one standardised position table plus footnotes out.

Steps (each returns a Step record the app shows as it runs):
  1 fetch       filing bytes from SEC EDGAR (edgar.py) or an uploaded HTML file
  2 locate      dated Schedule of Investments sections (Parallax section finder)
  3 parse       every table in the chosen section (Parallax table parser)
  4 standardise one row per position, money scaled to dollars, rate split into parts
  5 footnotes   marker -> definition text (BDC Footnotes repo extractor)
  6 resolve     each row's markers -> footnote text and tags
  7 QA          row counts, totals vs the filing's own total row, unresolved markers
  8 export      CSV, JSON and a data dictionary
No language model is called anywhere in this module.
"""
from __future__ import annotations

import io
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd

import footnotes as fn
from soi_parse import choose_section, load_blocks_from_bytes, parse_soi
from vendor_parallax.section import find_sections

RECON_TOLERANCE_PCT = 1.0   # same tolerance as Parallax parse.py

COLUMNS = [
    "filer_name", "cik", "accession", "form", "period_end", "row_id", "source_table_index",
    "issuer", "industry", "instrument", "instrument_bucket", "reference_rate", "spread_bps",
    "all_in_rate_pct", "pik_rate_pct", "floor_pct", "maturity", "par", "par_currency", "cost",
    "fair_value", "footnote_markers", "footnote_texts", "unresolved_markers",
] + [f"fn_{t}" for t in fn.TAG_NAMES] + ["rate_raw", "rate_parse_failed", "units_multiplier", "is_netting_row"]

DATA_DICTIONARY = [
    ("filer_name", "text", "Filer name from SEC EDGAR submissions (public SEC data)."),
    ("cik", "integer", "SEC Central Index Key of the filer."),
    ("accession", "text", "SEC accession number of the filing."),
    ("form", "text", "10-K or 10-Q."),
    ("period_end", "date", "As-of date of the parsed Schedule of Investments section."),
    ("row_id", "text", "table_index:row_index inside the filing HTML. Stable for one filing."),
    ("source_table_index", "integer", "Index of the HTML table the row came from, in document order."),
    ("issuer", "text", "Portfolio company name as printed, footnote markers removed. Carried down from the last named row for tranche rows."),
    ("industry", "text", "Industry column, else the group header row above, else the business description."),
    ("instrument", "text", "Investment type as printed (e.g. First lien senior secured loan)."),
    ("instrument_bucket", "text", "first_lien, unitranche, second_lien, subordinated, equity or other. Regex on instrument, then category header, then issuer."),
    ("reference_rate", "text", "SOFR, LIBOR, PRIME, EURIBOR, SONIA, ... or FIXED. Empty if no rate printed."),
    ("spread_bps", "number", "Contractual spread over the reference rate in basis points. Not an all-in yield."),
    ("all_in_rate_pct", "number", "All-in coupon in percent when printed."),
    ("pik_rate_pct", "number", "PIK component in percent when printed in the rate cell or a PIK column."),
    ("floor_pct", "number", "Reference-rate floor in percent when printed."),
    ("maturity", "text", "Maturity as printed (formats vary by filer)."),
    ("par", "number", "Principal or par in the printed currency, scaled to units (no FX conversion). Empty for equity."),
    ("par_currency", "text", "ISO code read from the cell prefix, USD by default."),
    ("cost", "number", "Amortized cost in US dollars, scaled from thousands or millions."),
    ("fair_value", "number", "Fair value in US dollars, scaled from thousands or millions."),
    ("footnote_markers", "text", "Markers printed on the row or its issuer cell, comma separated, e.g. 3,7,a."),
    ("footnote_texts", "text", "Resolved definition text per marker, formatted (marker) text, joined by ' || '."),
    ("unresolved_markers", "text", "Markers with no definition found in the schedule."),
    ("fn_non_accrual", "boolean", "A resolved footnote mentions non-accrual or default (repo keyword tagger). Candidate, not confirmed."),
    ("fn_pik", "boolean", "A resolved footnote mentions PIK / payment-in-kind."),
    ("fn_restricted", "boolean", "A resolved footnote mentions restricted securities or exemption from registration."),
    ("fn_non_qualifying", "boolean", "A resolved footnote mentions non-qualifying assets (1940 Act section 55(a))."),
    ("fn_affiliate_or_control", "boolean", "A resolved footnote mentions affiliate or control investments."),
    ("fn_level_3", "boolean", "A resolved footnote mentions Level 3 or significant unobservable inputs."),
    ("fn_unfunded_or_revolver", "boolean", "A resolved footnote mentions unfunded, delayed draw or revolver."),
    ("fn_non_income_producing", "boolean", "A resolved footnote mentions non-income producing."),
    ("fn_pledged_collateral", "boolean", "A resolved footnote mentions pledged collateral or a financing subsidiary."),
    ("rate_raw", "text", "Rate cells as printed, joined by ' | '. Kept for audit."),
    ("rate_parse_failed", "boolean", "True when a non-equity row has a rate cell the parser could not read."),
    ("units_multiplier", "number", "1000 when the table says 'in thousands', 1000000 for millions."),
    ("is_netting_row", "boolean", "Portfolio-level 'Unfunded ... Commitments' netting line, kept for reconciliation."),
]


@dataclass
class Step:
    name: str
    seconds: float
    summary: str
    ok: bool = True
    details: dict = field(default_factory=dict)


@dataclass
class Result:
    meta: dict
    steps: list[Step]
    positions: pd.DataFrame
    footnotes: pd.DataFrame
    qa: dict

    def to_json(self) -> str:
        return json.dumps({
            "meta": self.meta, "qa": self.qa,
            "steps": [asdict(s) for s in self.steps],
            "footnotes": self.footnotes.to_dict("records"),
            "positions": json.loads(self.positions.to_json(orient="records")),
        }, indent=1, default=str)

    def positions_csv(self) -> str:
        return self.positions.to_csv(index=False)

    def footnotes_csv(self) -> str:
        return self.footnotes.to_csv(index=False)


def data_dictionary_csv() -> str:
    buf = io.StringIO()
    pd.DataFrame(DATA_DICTIONARY, columns=["column", "type", "definition"]).to_csv(buf, index=False)
    return buf.getvalue()


def _pct_delta(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return round((a - b) / b * 100, 3)


def run(raw: bytes, meta: dict, on_step=None) -> Result:
    """Run steps 2 to 7 on filing bytes. `meta` carries cik, accession, form, report_date, company,
    source and fetch_seconds. `on_step(step)` is called after each step so the app can show progress."""
    steps: list[Step] = []

    def record(step: Step):
        steps.append(step)
        if on_step:
            on_step(step)

    record(Step("1 · Download the report", meta.get("fetch_seconds", 0.0),
                f"{len(raw) / 1e6:.1f} MB from {meta.get('source', 'upload')}",
                details={"bytes": len(raw), "document": meta.get("document_url", "")}))

    period = None
    if meta.get("report_date"):
        try:
            period = date.fromisoformat(meta["report_date"])
        except ValueError:
            period = None

    # 2 locate
    t = time.monotonic()
    blocks = load_blocks_from_bytes(raw)
    sections = find_sections(blocks)
    chosen = choose_section(sections, period)
    as_of = chosen.as_of if chosen is not None else None
    t_locate = time.monotonic() - t
    found = ", ".join(f"{s.as_of} ({len(s.table_blocks)} tables)" for s in sections) or "none"
    record(Step("2 · Find the list of loans", round(t_locate, 2),
                f"Dated sections found: {found}. Parsing {as_of}.", ok=as_of is not None,
                details={"period_from_edgar": meta.get("report_date") or None}))
    if as_of is None:
        empty = pd.DataFrame(columns=COLUMNS)
        qa = {"status": "no_schedule_found"}
        return Result(meta, steps, empty, pd.DataFrame(columns=["marker", "text", "tags", "rows_referencing"]), qa)

    # 5 footnotes are extracted before the tables are parsed (and shown after them), because
    # how the footnotes are lettered decides whether "(A)" in a table cell is a marker.
    t = time.monotonic()
    notes, repo_dates = fn.extract_primary(raw, as_of)
    method = "repo extractor (parsers.footnote_extractor)"
    if not notes:
        notes = fn.extract_fallback(fn.section_strings(blocks, chosen.ranges))
        method = "repo marker rules on the table parser's section range (fallback)"
    notes = {m: fn.strip_page_header(txt, meta.get("company", "")) for m, txt in notes.items()}
    trimmed = []
    for m in list(notes):
        notes[m], cut = fn.trim_runaway(notes[m])
        if cut:
            trimmed.append(m)
    upper = any(len(m) == 1 and m.isupper() for m in notes) and not any(m.isalpha() and m.islower() for m in notes)
    t_notes = time.monotonic() - t

    # 3 parse tables
    t = time.monotonic()
    soi = parse_soi(raw, period, blocks=blocks, upper_markers=upper)
    record(Step("3 · Read every table", round(time.monotonic() - t, 2),
                f"{soi.tables_kept} of {soi.tables_in_section} tables in the section kept "
                f"({soi.tables_continuation} headerless continuation pages). "
                f"Units: {'thousands' if soi.units_multiplier == 1e3 else 'millions' if soi.units_multiplier == 1e6 else 'dollars'}.",
                ok=soi.tables_kept > 0, details={"dropped_rows": soi.dropped, "upper_case_markers": upper}))

    # 4 standardise
    t = time.monotonic()
    added = {"filer_name", "cik", "accession", "form", "period_end", "footnote_texts", "unresolved_markers"}
    raw_cols = [c for c in COLUMNS if c not in added and not c.startswith("fn_")]
    pos = pd.DataFrame(soi.rows) if soi.rows else pd.DataFrame(columns=raw_cols)
    for c, v in (("filer_name", meta.get("company", "")), ("cik", meta.get("cik")),
                 ("accession", meta.get("accession", "")), ("form", meta.get("form", "")),
                 ("period_end", soi.as_of.isoformat())):
        pos[c] = v
    debt = pos[pos.instrument_bucket != "equity"] if len(pos) else pos
    record(Step("4 · Put every loan in the same columns", round(time.monotonic() - t, 2),
                f"{len(pos)} positions ({len(debt)} debt, {len(pos) - len(debt)} equity), one row each.",
                ok=len(pos) > 0))

    record(Step("5 · Read the footnotes", round(t_notes, 2),
                f"{len(notes)} footnote definitions via {method}.", ok=len(notes) > 0,
                details={"method": method, "repo_section_dates": repo_dates, "footnotes_trimmed": trimmed}))

    # 6 resolve markers
    t = time.monotonic()
    tags = {m: fn.tag_footnote(txt) for m, txt in notes.items()}
    # Markers are matched case-insensitively when the exact form is absent ("(a)" vs "(A)").
    lower = {m.lower(): m for m in notes}

    def lookup(mk: str):
        return mk if mk in notes else lower.get(mk.lower())

    texts, unresolved, flags = [], [], {t_: [] for t_ in fn.TAG_NAMES}
    ref_count: dict = {}
    for mks in pos.footnote_markers:
        row_texts, row_unres, row_tags = [], [], set()
        for mk in mks:
            key = lookup(mk)
            if key is None:
                row_unres.append(mk)
                continue
            ref_count[key] = ref_count.get(key, 0) + 1
            row_texts.append(f"({mk}) {notes[key]}")
            row_tags.update(tags[key])
        texts.append(" || ".join(row_texts))
        unresolved.append(",".join(row_unres))
        for t_ in fn.TAG_NAMES:
            flags[t_].append(t_ in row_tags)
    pos["footnote_texts"] = texts
    pos["unresolved_markers"] = unresolved
    for t_ in fn.TAG_NAMES:
        pos[f"fn_{t_}"] = flags[t_]
    all_markers = [mk for mks in pos.footnote_markers for mk in mks]
    distinct = sorted(set(all_markers), key=lambda x: (len(x), x))
    unresolved_distinct = sorted({mk for mk in distinct if lookup(mk) is None}, key=lambda x: (len(x), x))
    pos["footnote_markers"] = pos.footnote_markers.map(lambda m: ",".join(m))
    pos = pos[COLUMNS]
    record(Step("6 · Attach each footnote to its loans", round(time.monotonic() - t, 2),
                f"{len(distinct) - len(unresolved_distinct)} of {len(distinct)} distinct markers resolved; "
                f"{len(all_markers)} marker references on {int((pos.footnote_markers != '').sum())} rows.",
                ok=not unresolved_distinct))

    notes_df = pd.DataFrame([{"marker": m, "text": txt, "tags": ",".join(tags[m]),
                              "rows_referencing": ref_count.get(m, 0)} for m, txt in notes.items()],
                            columns=["marker", "text", "tags", "rows_referencing"])

    # 7 QA
    t = time.monotonic()
    sum_fv = float(pos.fair_value.fillna(0).sum())
    sum_cost = float(pos.cost.fillna(0).sum())
    total = soi.total_row or {}
    fv_delta = _pct_delta(sum_fv + soi.adjustments_fv, total.get("fair_value"))
    cost_delta = _pct_delta(sum_cost, total.get("cost"))
    debt = pos[pos.instrument_bucket != "equity"]
    qa = {
        "status": "ok",
        "rows": len(pos),
        "rows_debt": len(debt),
        "rows_equity": int((pos.instrument_bucket == "equity").sum()),
        "rows_missing_fair_value": int(pos.fair_value.isna().sum()),
        "rows_missing_cost": int(pos.cost.isna().sum()),
        "debt_rows_rate_parse_failed": int(debt.rate_parse_failed.sum()),
        "debt_rows_with_spread": int(debt.spread_bps.notna().sum()),
        "sum_fair_value": round(sum_fv, 0),
        "sum_cost": round(sum_cost, 0),
        "filing_total_row": total.get("text"),
        "filing_total_fair_value": total.get("fair_value"),
        "filing_total_cost": total.get("cost"),
        "fair_value_delta_pct": fv_delta,
        "cost_delta_pct": cost_delta,
        "fair_value_reconciles": None if fv_delta is None else abs(fv_delta) <= RECON_TOLERANCE_PCT,
        "cost_reconciles": None if cost_delta is None else abs(cost_delta) <= RECON_TOLERANCE_PCT,
        "xbrl_fair_value_facts": soi.xbrl_fv_facts,
        "footnotes_defined": len(notes),
        "markers_distinct": len(distinct),
        "markers_resolved": len(distinct) - len(unresolved_distinct),
        "markers_unresolved": unresolved_distinct,
        "footnotes_unreferenced": sorted([m for m in notes if m not in ref_count], key=lambda x: (len(x), x)),
        "rows_non_accrual_candidate": int(pos.fn_non_accrual.sum()),
        "footnote_method": method,
        "footnotes_trimmed": trimmed,
        "tolerance_pct": RECON_TOLERANCE_PCT,
    }
    recon = ("no total row found" if fv_delta is None else
             f"fair value {fv_delta:+.2f}% vs the filing's total ({'pass' if qa['fair_value_reconciles'] else 'fail'})")
    record(Step("7 · Check totals and gaps", round(time.monotonic() - t, 2),
                f"{recon}; {len(unresolved_distinct)} unresolved markers; "
                f"{qa['rows_missing_fair_value']} rows without fair value.",
                ok=bool(qa["fair_value_reconciles"]) and not unresolved_distinct))
    label = "synthetic sample" if meta.get("source") == "synthetic sample" else "public SEC data"
    meta = {**meta, "period_end": soi.as_of.isoformat(), "label": label}
    return Result(meta, steps, pos, notes_df, qa)
