# Copied from ~/Documents/Parallax/src/parallax/soi/tables.py (Parallax repo, commit 0ab5eaa),
# by the same author, for the BDC Footnotes demo. Logic unchanged unless a line says
# "DEMO CHANGE". Credit: Parallax M2 Schedule of Investments parser.
"""SOI table identification and row extraction (M2, steps 2 and 3; FM-001, FM-004, FM-011).

A table is an SOI table when one of its first rows carries >= 3 of the header tokens
{company, industry, principal/par, cost, fair value}. "Cost" alone counts (MAIN never says
"amortized cost"). Header cells are mapped to fields by token, never by position. Rows are
aligned to the header from the RIGHT: PSEC drops the leading company/industry cells on
tranche rows, so a row shorter than the header is shifted, not misread. A headerless table
that follows an SOI table in the same section inherits its mapping (continuation, FM-011).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MARKER_RE = re.compile(r"\((\d{1,2}|[a-z]{1,2})\)")
PURE_MARKERS_RE = re.compile(r"^(\s*\((?:\d{1,2}|[a-z]{1,2})\)\s*[*+†‡]*\s*)+$")
# DEMO CHANGE: some filers letter footnotes "(A)" (TCPC). Upper-case markers are off by default
# because "(E)" is also part of legal names ("EQT IX Co-Investment (E) SCSP", ARCC). The demo
# switches them on only when the filing's footnotes are lettered in upper case (soi_parse.py).
_MARKER_LOWER, _PURE_LOWER = MARKER_RE, PURE_MARKERS_RE
_MARKER_UPPER = re.compile(r"\((\d{1,2}|[a-z]{1,2}|[A-Z])\)")
_PURE_UPPER = re.compile(r"^(\s*\((?:\d{1,2}|[a-z]{1,2}|[A-Z])\)\s*[*+†‡]*\s*)+$")


def set_upper_case_markers(on: bool) -> None:
    """DEMO CHANGE: swap the module-level marker patterns. Callers hold soi_parse.PARSE_LOCK."""
    global MARKER_RE, PURE_MARKERS_RE
    MARKER_RE, PURE_MARKERS_RE = (_MARKER_UPPER, _PURE_UPPER) if on else (_MARKER_LOWER, _PURE_LOWER)
ISO_CODES = "EUR|GBP|AUD|CAD|SEK|NOK|DKK|CHF|NZD|JPY|USD"
SYMBOL_TO_ISO = {"$": "USD", "€": "EUR", "£": "GBP", "A$": "AUD", "C$": "CAD"}
# "(1,234)", "$ 12.5", "€ 52,781", "£11,635", "A$1,416", "EUR 5,300" (P0.b: a currency prefix is still a number)
NUM_RE = re.compile(rf"^\(?\s*(?:[AC]?\$|[€£]|(?:{ISO_CODES})\b)?\s*-?[\d,]+(?:\.\d+)?\s*\)?$")
CURRENCY_PREFIX_RE = re.compile(rf"^\(?\s*([AC]?\$|[€£]|(?:{ISO_CODES})\b)\s*(?=-?[\d,])")
CURRENCY_PAREN_RE = re.compile(r"\s*\(\s*[A-Z]{3}\s*[\d,\.]+\s*\)\s*$")   # "19,275 (EUR 16,859)" (TSLX)
DASH_RE = re.compile(r"^\s*(?:[AC]?\$|[€£])?\s*[—–-]\s*$")   # "€ -" is an undrawn EUR line (OBDC)
TOTAL_RE = re.compile(r"^\s*(sub)?total\b|^\s*net\s+(senior|asset|subordinated|equity|other|second|first|unfunded|investments?)", re.I)
ADJUSTMENT_RE = re.compile(r"^\s*unfunded\s+(loan\s+)?commitments?", re.I)   # FSK nets these into its total
TOTAL_INVESTMENTS_RE = re.compile(
    r"^\s*total\s+(portfolio\s+(company\s+)?)?investments?(\s+in\s+securities)?\b", re.I)
TOTAL_INVESTMENTS_EXCLUDE_RE = re.compile(
    r"(before|after|and\s+money|money\s+market|cash|debt|equity|warrant|fund|derivative|hedge|structured|"
    r"joint|senior|subordinated|asset|other|controlled|affiliate|non-|first|second|lien|income)", re.I)


def is_total_investments(text: str) -> bool:
    """'Total investments', 'TOTAL INVESTMENTS—223.1%' (FSK), 'Total Investments in Securities
    (201.24%)*' (HTGC), 'Total Portfolio Company investments, December 31, 2025 (184.3% of net
    assets...)' (MAIN). Not 'Total investments and money market funds', 'Total debt investments'."""
    text = text or ""
    if not TOTAL_INVESTMENTS_RE.match(text):
        return False
    head = re.split(r"[,(—–]|\s-\s", text, maxsplit=1)[0]   # DEMO CHANGE: "Total Investments - 233.8% of Net Assets" (TCPC)
    return not TOTAL_INVESTMENTS_EXCLUDE_RE.search(head)
CATEGORY_RE = re.compile(
    r"^(debt|equity|senior|first[\s-]*lien|second[\s-]*lien|subordinated|unsecured|preferred|common|"
    r"warrants?|structured|non-?control|control|affiliate|non-?affiliate|investments?\b|"
    r"other\b|asset[\s-]based|joint venture|money market|cash|short[\s-]term|total|"
    r"unitranche|one stop|corporate|portfolio|level\b|non-?income|income[\s-]producing|"
    r"revolv|delayed draw|term loan|loan|notes?\b|bonds?\b|convertible|mezzanine|junior|"
    r"lmm|private loan|middle market|external investment|dividend|interest)", re.I)
# TSLX: "($3,676 par, due 9/2029)", "(EUR 208 par, due 4/2030)", "(AUD 60,000 par, ...)" (P0.b)
PAR_IN_TEXT_RE = re.compile(rf"\(\s*(\$|€|£|{ISO_CODES})?\s*([\d,]+(?:\.\d+)?)\s*par\b", re.I)
DUE_IN_TEXT_RE = re.compile(r"\bdue\s+(\d{1,2}/\d{4}|\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]+\s+\d{4})", re.I)

# header token -> field
HEADER_MAP = [
    ("company", re.compile(r"^(portfolio\s+)?company|^issuer|^name\s+of|^portfolio\s+company\s*/")),
    ("footnotes", re.compile(r"^footnotes?$|^notes?$")),   # DEMO CHANGE: TCPC "Notes" column
    ("industry", re.compile(r"^industry|^sector")),
    ("description", re.compile(r"^business\s+description|^description")),
    ("investment", re.compile(r"^investments?$|^investments?\s*/|^type\s+of\s+investment|^investment\s+type|^investment\b(?!.*date)|^security|^instrument")),
    ("reference", re.compile(r"^ref(erence)?\.?\s*rate|^reference$|^ref\.?$|^index$|^base\s+rate|^benchmark")),   # DEMO CHANGE: "Ref"
    ("spread", re.compile(r"^spread|^spread\s+above")),
    ("coupon", re.compile(r"^coupon|^interest\s*rate|^all-?in|^total\s+rate|^rate$|^rate\b|^interest$|^cash\s+interest|^cash$|^current\s+rate|^stated")),
    ("pik", re.compile(r"^pik")),
    ("floor", re.compile(r"^floor")),
    ("maturity", re.compile(r"maturity|^due\b|^expir")),
    ("acquisition", re.compile(r"^(initial\s+)?acquisition|^investment\s+date|^date\s+acquired|^initial")),
    ("shares", re.compile(r"^shares|^units|^number\s+of|^quantity")),
    ("commitment", re.compile(r"^commitment")),
    ("principal", re.compile(r"^principal|^par\b|^par\s|^face|^outstanding\s+principal")),
    ("cost", re.compile(r"^amortized\s*cost|^cost\b|^cost$|^adjusted\s+cost")),
    ("fair_value", re.compile(r"^fair\s*value|^value$|^fairvalue|^market\s+value|^estimated\s+fair")),
    ("pct_net", re.compile(r"%\s*of\s*net|percentage\s*of\s*net|^%\s*of")),
]
SIGNATURE_FIELDS = {"company", "industry", "principal", "cost", "fair_value"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def grid(table) -> list[list[str]]:
    """Rectangular-ish grid: colspan expanded, one string per cell."""
    rows = []
    for tr in table.iter("tr"):
        cells: list[str] = []
        for td in tr:
            if td.tag not in ("td", "th"):
                continue
            txt = _clean(td.text_content())
            try:
                cs = max(1, int(td.get("colspan", "1") or 1))
            except ValueError:
                cs = 1
            cells.append(txt)
            cells.extend([""] * (cs - 1))
        rows.append(cells)
    return rows


def norm_header(s: str) -> str:
    s = MARKER_RE.sub("", s.lower())
    s = re.sub(r"[\$\*\+†‡]", "", s)
    # "SpreadAboveIndex", "PrincipalAmount", "FairValue": split glued CamelCase from the raw html
    return re.sub(r"\s+", " ", s).strip()


def header_field(cell: str) -> str | None:
    h = norm_header(cell)
    if not h:
        return None
    for name, rx in HEADER_MAP:
        if rx.search(h):
            return name
    return None


@dataclass
class Mapping:
    header_row: int
    width: int
    groups: dict[str, list[int]] = field(default_factory=dict)   # field -> columns
    fields_present: set[str] = field(default_factory=set)

    def signature_score(self) -> int:
        return len(SIGNATURE_FIELDS & self.fields_present)


def find_header(rows: list[list[str]], max_rows: int = 10) -> Mapping | None:
    """First row among the first `max_rows` with >= 3 signature fields."""
    for ri, row in enumerate(rows[:max_rows]):
        # glue split header words for matching ("Amortized" / "Cost" are in one cell already)
        fields = {}
        for ci, cell in enumerate(row):
            f = header_field(cell)
            if f:
                fields.setdefault(f, ci)
        present = set(fields)
        header_cols = sorted(ci for ci, c in enumerate(row) if c.strip())
        # GBDC leaves the company column unnamed: leading unnamed columns are the company
        if "company" not in present and header_cols and header_cols[0] > 0 and len(present) >= 3:
            present.add("company")
            fields["company"] = -1
        if len(SIGNATURE_FIELDS & present) >= 3 and "company" in present and \
                ("cost" in present or "fair_value" in present):
            width = len(row)   # the header row's own colspan width, not the table's max (GBDC)
            m = Mapping(header_row=ri, width=width, fields_present=present)
            # groups: from the field's header column to the next non-empty header cell
            for f, c0 in fields.items():
                if c0 == -1:
                    m.groups[f] = list(range(0, header_cols[0]))
                    continue
                nxt = next((c for c in header_cols if c > c0), width)
                m.groups[f] = list(range(c0, nxt))
            return m
    return None


def is_header_like(row: list[str]) -> bool:
    fields = {header_field(c) for c in row if c.strip()}
    fields.discard(None)
    return len(SIGNATURE_FIELDS & fields) >= 3


def is_number(s: str) -> bool:
    s = CURRENCY_PAREN_RE.sub("", _clean(s))
    return bool(s) and bool(NUM_RE.match(s)) and any(ch.isdigit() for ch in s)


def cell_currency(s: str) -> str | None:
    """ISO code of a currency prefix on a numeric cell: '€ 52,781' -> EUR, '£11,635' -> GBP,
    '$ 130' -> USD, 'SEK 1,234' -> SEK, '1,234' -> None (P0.b)."""
    s = CURRENCY_PAREN_RE.sub("", _clean(s))
    m = CURRENCY_PREFIX_RE.match(s)
    if not m:
        return None
    tok = m.group(1)
    return SYMBOL_TO_ISO.get(tok, tok.upper())


def parse_number(s: str) -> float | None:
    """'(1,234)' -> -1234.0; '—', '-', '' -> None; '$ 12.5' -> 12.5; '19,275 (EUR 16,859)' -> 19275;
    '€ 52,781' -> 52781 (the currency is read separately by cell_currency)."""
    s = CURRENCY_PAREN_RE.sub("", _clean(s))
    s = CURRENCY_PREFIX_RE.sub(lambda m: "(" if m.group(0).lstrip().startswith("(") else "", s)
    s = s.replace("$", "").replace(",", "")
    if not s or s in ("—", "-", "–", "N/A", "n/a"):
        return None
    neg = s.startswith("(") or s.startswith("-")
    s = s.strip("()- ").strip()
    if not re.match(r"^\d+(\.\d+)?$", s):
        return None
    v = float(s)
    return -v if neg else v


def group_value(row: list[str], cols: list[int]) -> str:
    parts = []
    for c in cols:
        if c < len(row) and row[c].strip():
            t = row[c].strip()
            if t in ("$", "%", ")", "(", "USD"):
                # keep ')' for negatives: "(1,097" + ")" -> "(1,097)"
                if t == ")" and parts and parts[-1].startswith("("):
                    parts[-1] = parts[-1] + ")"
                elif t == "%" and parts:
                    parts[-1] = parts[-1] + "%"
                continue
            if PURE_MARKERS_RE.match(t):
                continue
            parts.append(t)
    return " ".join(parts)


def first_number(row: list[str], cols: list[int], dash_zero: bool = False) -> float | None:
    """First numeric cell in the column group. With dash_zero, a lone dash reads as 0.0
    (an undrawn revolver's principal, a zero-value warrant): the filing's total foots that way."""
    # prefer cells that cannot be footnote markers ("(8)" is a marker on PSEC, -2 on GBDC)
    for c in cols:
        if c < len(row) and is_number(row[c]) and not PURE_MARKERS_RE.match(row[c].strip()):
            v = parse_number(row[c])
            if v is not None:
                return v
    for c in cols:
        if c < len(row) and is_number(row[c]):
            v = parse_number(row[c])
            if v is not None:
                return v
    # negative split as "(1,097" then ")"
    for c in cols:
        if c < len(row) and re.match(r"^\(\s*[\d,]+(\.\d+)?$", row[c].strip()):
            return parse_number(row[c].strip() + ")")
    if dash_zero:
        for c in cols:
            if c < len(row) and DASH_RE.match(row[c]):
                return 0.0
    return None


def _rstrip_blank(row: list[str]) -> list[str]:
    n = len(row)
    while n > 0 and not row[n - 1].strip():
        n -= 1
    return row[:n]


def align(row: list[str], width: int, groups: dict | None = None) -> list[str]:
    """A row shorter than the header row is either missing LEADING cells (PSEC tranche rows drop
    the company and industry cells: right-align) or TRAILING spacer cells (GBDC continuation
    pages: keep left). Pick the alignment that puts more numeric or dash cells into the money
    column groups; ties keep the row as printed."""
    if len(row) >= width:
        return row
    right = [""] * (width - len(row)) + row
    if not groups:
        return right
    def score(r):
        n = 0
        for f in ("principal", "cost", "fair_value", "pct_net", "shares"):
            cols = groups.get(f, [])
            if any(c < len(r) and (DASH_RE.match(r[c]) or (is_number(r[c]) and not PURE_MARKERS_RE.match(r[c].strip())))
                   for c in cols):
                n += 1
        return n
    sl, sr = score(row), score(right)
    return right if sr > sl else row


def row_markers(row: list[str], m: Mapping) -> list[str]:
    """Footnote markers from the company/investment/footnotes cells and any pure-marker cell."""
    out: list[str] = []
    scan_cols = set(m.groups.get("company", []) + m.groups.get("investment", []) + m.groups.get("footnotes", []))
    # DEMO CHANGE: a "(71)" cell inside a money column is a negative amount (MAIN and GBDC
    # print small negative cost and fair value on unfunded revolvers), not marker 71. Keep it
    # as a marker only when its column group holds another, non-marker number (PSEC layout).
    money_group_of = {c: f for f in ("principal", "cost", "fair_value", "pct_net") for c in m.groups.get(f, [])}

    def money_elsewhere(ci: int) -> bool:
        cols = m.groups.get(money_group_of[ci], [])
        return any(c != ci and c < len(row) and is_number(row[c]) and not PURE_MARKERS_RE.match(row[c].strip())
                   for c in cols)

    for ci, cell in enumerate(row):
        cell = cell.strip()
        if not cell:
            continue
        if ci not in scan_cols and ci in money_group_of and not money_elsewhere(ci):
            continue
        if ci in scan_cols or PURE_MARKERS_RE.match(cell):
            found = MARKER_RE.findall(cell)
            # DEMO CHANGE: a footnotes column may print bare markers, "D/E/N" or "3, 7" (TCPC)
            if not found and ci in m.groups.get("footnotes", []) and \
                    re.fullmatch(r"[A-Za-z0-9]{1,2}(\s*[/,;]\s*[A-Za-z0-9]{1,2})*", cell):
                found = re.split(r"\s*[/,;]\s*", cell)
            out.extend(found)
    seen = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def strip_markers(s: str) -> str:
    s = MARKER_RE.sub("", s)
    s = re.sub(r"[\*†‡]+$|\s*[\*†‡]+\s*$", "", s)
    s = re.sub(r"\+\s*$", "", s)         # GBDC "Company+"
    return _clean(s)


@dataclass
class RowOut:
    kind: str                              # position | total_investments | subtotal | header | group | blank | other
    data: dict = field(default_factory=dict)


def classify_rows(rows: list[list[str]], m: Mapping, state: dict) -> list[RowOut]:
    """Walk data rows, keeping company / industry / category state across rows and tables."""
    out: list[RowOut] = []
    money_fields = [f for f in ("principal", "cost", "fair_value") if f in m.groups]
    for ri, raw in enumerate(rows):
        if ri <= m.header_row and m.header_row >= 0 and ri == m.header_row:
            out.append(RowOut("header"))
            continue
        row = align(raw, m.width, m.groups)
        nonblank = [c for c in row if c.strip()]
        if not nonblank:
            out.append(RowOut("blank"))
            continue
        if is_header_like(row):
            out.append(RowOut("header"))
            continue
        money_num = {f: first_number(row, m.groups[f]) for f in money_fields}
        has_number = any(v is not None for v in money_num.values())
        company_txt = group_value(row, m.groups.get("company", []))
        invest_txt = group_value(row, m.groups.get("investment", [])) if "investment" in m.groups else ""
        first_txt = next((c for c in row if c.strip()), "")
        # a labelled row whose money cells are dashes is a zero position, not a blank
        money = {f: first_number(row, m.groups[f], dash_zero=bool(company_txt or invest_txt)) for f in money_fields}
        has_money = any(v is not None for v in money.values())
        # Total investments row (reconciliation), then other totals/subtotals
        if is_total_investments(company_txt or first_txt) and has_number:
            out.append(RowOut("total_investments", {"cost": money.get("cost"), "fair_value": money.get("fair_value"),
                                                     "text": company_txt or first_txt}))
            continue
        if TOTAL_RE.match(company_txt or first_txt):
            out.append(RowOut("subtotal"))
            continue
        # FSK 2024: a company-level subtotal row that repeats the company name with cost and
        # fair value only (no investment, industry, maturity, rate, footnotes or principal)
        if (has_number and company_txt and not invest_txt and company_txt == state.get("company_raw")
                and money_num.get("principal") is None
                and not any(group_value(row, m.groups[f]) for f in ("industry", "maturity", "footnotes", "coupon",
                                                                    "reference", "spread", "shares") if f in m.groups)):
            out.append(RowOut("subtotal"))
            continue
        if ADJUSTMENT_RE.match(company_txt or first_txt) and has_number:
            out.append(RowOut("adjustment", {"cost": money_num.get("cost"), "fair_value": money_num.get("fair_value"),
                                             "text": company_txt or first_txt}))
            continue
        # Blank company and blank investment with money: a per-company subtotal
        if has_number and not company_txt and not invest_txt:
            shares_txt = group_value(row, m.groups.get("shares", [])) if "shares" in m.groups else ""
            if not shares_txt:
                out.append(RowOut("subtotal"))
                continue
        if not has_money:
            texts = [c for c in nonblank if not PURE_MARKERS_RE.match(c) and not is_number(c)]
            if len(texts) == 1 and len(texts[0]) < 90 and not group_value(row, m.groups.get("maturity", [])):
                t = strip_markers(texts[0])
                if t.endswith(":"):
                    t = t[:-1]
                if CATEGORY_RE.match(t):
                    state["category"] = t
                else:
                    state["industry"] = t
                out.append(RowOut("group", {"text": t}))
                continue
            # A company row with a description but no money (MAIN): set company and description
            if company_txt and not is_number(company_txt):
                state["company"] = strip_markers(company_txt)
                state["company_markers"] = MARKER_RE.findall(company_txt) + [
                    mk for c in row if PURE_MARKERS_RE.match(c.strip()) for mk in MARKER_RE.findall(c)]
                desc = group_value(row, m.groups.get("description", [])) if "description" in m.groups else ""
                ind = group_value(row, m.groups.get("industry", [])) if "industry" in m.groups else ""
                if ind:
                    state["row_industry"] = ind
                elif desc:
                    state["row_industry"] = desc
                else:
                    state["row_industry"] = None
                out.append(RowOut("other", {"text": company_txt}))
                continue
            out.append(RowOut("other", {"text": first_txt}))
            continue
        # Position row
        markers = row_markers(row, m)
        if company_txt and not is_number(company_txt):
            state["company"] = strip_markers(company_txt)
            state["company_raw"] = company_txt
            # P0.a (D-007): a marker printed on the company-name cell belongs to every tranche row
            # of that company in this section (TSLX "ASP Unifrax Holdings, Inc. (12)(13)" then three
            # rows). Markers on the investment cell stay on their own row (MAIN's per-row "(14)").
            state["company_markers"] = MARKER_RE.findall(company_txt)
            ind = group_value(row, m.groups.get("industry", [])) if "industry" in m.groups else ""
            desc = group_value(row, m.groups.get("description", [])) if "description" in m.groups else ""
            state["row_industry"] = ind or desc or None
        else:
            markers = list(dict.fromkeys(state.get("company_markers", []) + markers))
        principal_currency = None
        for c in m.groups.get("principal", []):
            if c < len(row) and (is_number(row[c]) or DASH_RE.match(row[c])) and not PURE_MARKERS_RE.match(row[c].strip()):
                principal_currency = cell_currency(row[c]) or cell_currency(row[c].strip().rstrip("—–-") + "0")
                break
        d = {
            "raw_company_name": state.get("company"),
            # industry: the column (FSK, PSEC) > the group-header row (ARCC, OBDC, TSLX, HTGC, GBDC)
            # > the business description (MAIN has nothing else)
            "industry": (group_value(row, m.groups["industry"]) if "industry" in m.groups and
                         group_value(row, m.groups["industry"]) else None) or state.get("industry") or state.get("row_industry"),
            "investment_type": invest_txt or state.get("category"),
            "category": state.get("category"),
            "footnote_markers": markers,
            "principal": money.get("principal"),
            "principal_currency": principal_currency,
            "cost": money.get("cost"),
            "fair_value": money.get("fair_value"),
            "maturity": group_value(row, m.groups["maturity"]) if "maturity" in m.groups else "",
            "rate_text": "",
            "coupon_text": group_value(row, m.groups["coupon"]) if "coupon" in m.groups else "",
            "pik_text": group_value(row, m.groups["pik"]) if "pik" in m.groups else "",
            "floor_text": group_value(row, m.groups["floor"]) if "floor" in m.groups else "",
            "shares_text": group_value(row, m.groups["shares"]) if "shares" in m.groups else "",
            "row_in_table": ri,
        }
        ref = group_value(row, m.groups["reference"]) if "reference" in m.groups else ""
        spr = group_value(row, m.groups["spread"]) if "spread" in m.groups else ""
        if ref and spr:
            d["rate_text"] = f"{ref} + {spr}" if "+" not in ref and "+" not in spr else f"{ref} {spr}"
        elif spr:
            d["rate_text"] = spr
        elif ref:
            d["rate_text"] = ref
        elif "reference" not in m.groups and "spread" not in m.groups:
            # single free-text rate column ("Interest Rate and Floor", "Rate")
            d["rate_text"] = d["coupon_text"]
            d["coupon_text"] = ""
        if "reference" in m.groups and "spread" not in m.groups:
            # OBDC: "Ref. Rate | Cash | PIK" with no spread column; Cash is the spread when a
            # reference is printed and the fixed rate when it is not
            if ref:
                d["rate_text"] = f"{ref} {d['coupon_text']}".strip()
            else:
                d["rate_text"] = d["coupon_text"]
            d["coupon_text"] = ""
        # TSLX: principal and maturity live in the investment text
        if d["principal"] is None and invest_txt:
            pm = PAR_IN_TEXT_RE.search(invest_txt)
            if pm:
                d["principal"] = parse_number(pm.group(2))
                d["principal_from_text"] = True
                cur = pm.group(1)
                d["principal_currency"] = SYMBOL_TO_ISO.get(cur, cur.upper()) if cur else None
        if not d["maturity"] and invest_txt:
            dm = DUE_IN_TEXT_RE.search(invest_txt)
            if dm:
                d["maturity"] = dm.group(1)
        out.append(RowOut("position", d))
    return out
