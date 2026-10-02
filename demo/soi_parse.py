"""Locate the Schedule of Investments and parse every table into one row per position.

Adapted from ~/Documents/Parallax/src/parallax/parse.py (parse_filing, instrument_bucket,
is_netting_row, xbrl_fair_value_tags), commit 0ab5eaa, same author. Changes for the demo:
- works on bytes in memory (no filings index, no parquet cache, no ticker list)
- picks the section by EDGAR period, else the latest dated section
- keeps the reconciliation rule as is (sum of fair value vs the filing's own
  "Total investments" row, pass within 1%) and adds the same check on cost
- drops the non-accrual legend heuristic and the Gemini fallback: footnotes are
  resolved by the BDC Footnotes repo's own extractor instead (see footnotes.py)
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date

from lxml import html as LH

from vendor_parallax.rates import parse_rate
from vendor_parallax.section import UNITS_RE, find_sections, iter_blocks, nearby_units
from vendor_parallax import tables as _tables
from vendor_parallax.tables import Mapping, classify_rows, find_header, grid, is_number

# Upper-case markers are a module-level switch in the vendored table parser; one parse at a
# time holds it so two visitors on the same server can't flip it under each other.
PARSE_LOCK = threading.Lock()

# ---- copied from parallax/parse.py -------------------------------------------------

NETTING_NAME_RE = re.compile(r"^Unfunded\b.*Commitments$")


def is_netting_row(raw_company_name: str | None) -> bool:
    return bool(raw_company_name) and bool(NETTING_NAME_RE.match(str(raw_company_name).strip()))


BUCKET_RULES = [
    ("unitranche", re.compile(r"unitranche|one[\s-]stop", re.I)),
    ("second_lien", re.compile(r"second[\s-]*lien|2nd[\s-]*lien", re.I)),
    ("first_lien", re.compile(r"first[\s-]*lien|1st[\s-]*lien|senior\s+secured|secured\s+debt|senior\s+debt|"
                              r"senior\s+loan|term\s+loan|revolv|delayed\s+draw|senior\s+term|first-?lien", re.I)),
    ("subordinated", re.compile(r"subordinated|mezzanine|junior|unsecured\s+debt|unsecured\s+note|"
                                r"unsecured|second\s+secured|last[\s-]out", re.I)),
    ("equity", re.compile(r"equity|common|preferred|units?\b|shares?\b|warrant|stock|membership|"
                          r"partnership|\binterests?\b(?!\s*rate)|class\s+[a-z]\b|llc\s+interest|"
                          r"limited\s+partner|option|convertible\s+preferred", re.I)),
]


def instrument_bucket(investment_type: str | None, category: str | None, company: str | None = None) -> str:
    inv = investment_type or ""
    for name, rx in BUCKET_RULES:
        if rx.search(inv):
            return name
    text = f"{investment_type or ''} || {category or ''}"
    for name, rx in BUCKET_RULES:
        if rx.search(text):
            return name
    if company and BUCKET_RULES[-1][1].search(company.split(",")[-1] if "," in company else ""):
        return "equity"
    return "other"


XBRL_CTX_RE = re.compile(rb'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', re.S)
XBRL_INSTANT_RE = re.compile(rb"<xbrli:instant>(\d{4}-\d{2}-\d{2})</xbrli:instant>")
XBRL_FV_RE = re.compile(rb'<ix:nonFraction[^>]*name="us-gaap:InvestmentOwnedAtFairValue"[^>]*>', re.S)
XBRL_ATTR_RE = re.compile(rb'(contextRef|scale)="([^"]*)"')


def xbrl_fair_value_tags(raw: bytes, period_end: date) -> int:
    """Count inline XBRL InvestmentOwnedAtFairValue facts dated period_end. An independent
    signal of how many fair-value cells the filer tagged; not every filer tags them."""
    target = period_end.isoformat().encode()
    ctx_ids = set()
    for cid, body in XBRL_CTX_RE.findall(raw):
        m = XBRL_INSTANT_RE.search(body)
        if m and m.group(1) == target:
            ctx_ids.add(cid)
    n = 0
    for tag in XBRL_FV_RE.findall(raw):
        attrs = dict(XBRL_ATTR_RE.findall(tag))
        if attrs.get(b"contextRef") in ctx_ids:
            n += 1
    return n

# ---- demo code -----------------------------------------------------------------------


@dataclass
class SoiResult:
    sections: list[dict]                 # every dated schedule section found
    as_of: date | None                   # the section parsed
    rows: list[dict] = field(default_factory=list)
    tables_in_section: int = 0
    tables_kept: int = 0
    tables_continuation: int = 0
    units_multiplier: float | None = None
    total_row: dict | None = None        # the filing's own "Total investments" row (scaled)
    adjustments_fv: float = 0.0
    xbrl_fv_facts: int | None = None
    dropped: dict = field(default_factory=dict)


def load_blocks_from_bytes(raw: bytes):
    return iter_blocks(LH.fromstring(raw))


def choose_section(sections, period: date | None):
    """The section dated `period` if EDGAR gave one, else the latest dated section."""
    dated = [s for s in sections if s.as_of]
    if period:
        for s in dated:
            if s.as_of == period:
                return s
    return max(dated, key=lambda s: s.as_of) if dated else (sections[0] if sections else None)


def parse_soi(raw: bytes, period: date | None = None, blocks=None, upper_markers: bool = False) -> SoiResult:
    """Steps 2 to 4 of the demo: locate the schedule, parse every table, standardise rows.
    upper_markers: read "(A)"-style markers too (only when the filing letters its footnotes so)."""
    with PARSE_LOCK:
        _tables.set_upper_case_markers(upper_markers)
        try:
            return _parse_soi(raw, period, blocks)
        finally:
            _tables.set_upper_case_markers(False)


def _parse_soi(raw: bytes, period: date | None, blocks) -> SoiResult:
    blocks = blocks if blocks is not None else load_blocks_from_bytes(raw)
    sections = find_sections(blocks)
    found = [{"as_of": s.as_of.isoformat() if s.as_of else None, "tables": len(s.table_blocks),
              "header": s.header_text[:80]} for s in sections]
    section = choose_section(sections, period)
    if section is None:
        return SoiResult(sections=found, as_of=None)

    doc_units = None
    for b in blocks[:4000]:
        if b.kind == "text":
            m = UNITS_RE.search(b.text)
            if m:
                doc_units = 1e3 if m.group(1).lower() == "thousands" else 1e6
                break
    section_units = section.units_multiplier or doc_units

    state: dict = {}
    mapping = None
    rows_out: list[dict] = []
    total_row = None
    adjustments: list[dict] = []
    tables_kept = tables_cont = 0
    dropped = {"subtotal": 0, "group": 0, "other": 0, "header": 0}
    for bi in section.table_blocks:
        if total_row is not None:
            break   # rows after "Total investments" (cash, money market funds) are not positions
        tbl = blocks[bi].table
        g = grid(tbl)
        if not g:
            continue
        m = find_header(g)
        is_cont = False
        if m is None:
            if mapping is None:
                continue
            if sum(1 for r in g if sum(is_number(c) for c in r) >= 2) == 0:
                continue
            m = Mapping(header_row=-1, width=mapping.width, groups=mapping.groups,
                        fields_present=mapping.fields_present)
            is_cont = True
        table_units = nearby_units(blocks, bi, back=8, fwd=0) or section_units
        outs = classify_rows(g, m, state)
        positions = [o for o in outs if o.kind == "position"]
        if is_cont:
            fv_ok = sum(1 for o in positions if o.data.get("fair_value") is not None)
            if not positions or fv_ok < 0.5 * len(positions):
                continue
            tables_cont += 1
        else:
            mapping = m
        tables_kept += 1
        for o in outs:
            if o.kind == "position":
                d = o.data
                d["soi_table_index"] = blocks[bi].index
                d["units_multiplier"] = table_units
                rows_out.append(d)
            elif o.kind == "total_investments" and total_row is None:
                total_row = {"cost": o.data.get("cost"), "fair_value": o.data.get("fair_value"),
                             "units": table_units, "text": o.data.get("text")}
                break
            elif o.kind == "adjustment":
                adjustments.append({**o.data, "units": table_units})
            elif o.kind in dropped:
                dropped[o.kind] += 1

    mult_default = section_units or 1.0
    records = []
    for d in rows_out:
        mult = d.get("units_multiplier") or mult_default
        bucket = instrument_bucket(d.get("investment_type"), d.get("category"), d.get("raw_company_name"))
        rate = parse_rate(d.get("rate_text"), d.get("coupon_text") or None, d.get("pik_text") or None,
                          d.get("floor_text") or None)
        principal = None if bucket == "equity" else d.get("principal")
        records.append({
            "row_id": f"{d['soi_table_index']}:{d['row_in_table']}",
            "source_table_index": d["soi_table_index"],
            "issuer": d.get("raw_company_name"),
            "industry": d.get("industry"),
            "instrument": d.get("investment_type"),
            "instrument_bucket": bucket,
            "reference_rate": rate.reference_rate,
            "spread_bps": rate.spread_bps,
            "all_in_rate_pct": rate.all_in_rate,
            "pik_rate_pct": rate.pik_rate,
            "floor_pct": rate.floor,
            "maturity": d.get("maturity") or None,
            "par": principal * mult if principal is not None else None,
            "par_currency": (d.get("principal_currency") or "USD") if principal is not None else None,
            "cost": d["cost"] * mult if d.get("cost") is not None else None,
            "fair_value": d["fair_value"] * mult if d.get("fair_value") is not None else None,
            "footnote_markers": list(d.get("footnote_markers") or []),
            "rate_raw": " | ".join(x for x in (d.get("rate_text"), d.get("coupon_text"), d.get("pik_text"),
                                               d.get("floor_text")) if x),
            "rate_parse_failed": bool(rate.rate_parse_failed and bucket != "equity"),
            "units_multiplier": mult,
            "is_netting_row": is_netting_row(d.get("raw_company_name")),
        })

    total = None
    if total_row and total_row.get("fair_value") is not None:
        u = total_row["units"] or mult_default
        total = {"text": total_row["text"], "fair_value": total_row["fair_value"] * u,
                 "cost": total_row["cost"] * u if total_row.get("cost") is not None else None}
    adj_fv = sum((a["fair_value"] or 0) * (a["units"] or mult_default) for a in adjustments)
    return SoiResult(
        sections=found, as_of=section.as_of, rows=records,
        tables_in_section=len(section.table_blocks), tables_kept=tables_kept,
        tables_continuation=tables_cont, units_multiplier=section_units,
        total_row=total, adjustments_fv=adj_fv,
        xbrl_fv_facts=xbrl_fair_value_tags(raw, section.as_of) if section.as_of else None,
        dropped=dropped,
    )
