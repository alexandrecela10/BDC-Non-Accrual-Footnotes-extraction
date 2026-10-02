# Copied from ~/Documents/Parallax/src/parallax/soi/rates.py (Parallax repo, commit 0ab5eaa),
# by the same author, for the BDC Footnotes demo. Logic unchanged unless a line says
# "DEMO CHANGE". Credit: Parallax M2 Schedule of Investments parser.
"""Rate regex (M2, step 4; FM-012). Never guesses: an unmatched non-empty cell is a failure.

Shapes covered (tests in tests/test_rates.py): "SOFR + 5.25%", "S+525", "L + 6.00% (11.25%)",
"12.00% PIK", "1M SOFR + 5.00% (1.00% Floor)", "SOFR (Q) + 6.00%", "SOFR + 5.00% (2.00% PIK)",
"10.50% (7.50% Cash + 3.00% PIK)", "Prime + 2.00%", "9.00%", blank, split columns
("SF +" | "5.25%" | "8.96%"), "3-month SOFR + 7.30%, Floor rate 8.30%", HTGC exit fees,
GBDC "5.32% cash/ 3.50% PIK", FSK "SF + 6.0% PIK 1.0%".

Bare number after "+": below 30 is a percent (5.25 -> 525 bps), 30 or above is already bps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

REF_ALIASES = [
    (r"(?:\d+\s*-?\s*(?:m|mo|month)\s*)?(?:cme\s+)?(?:term\s+|daily\s+|daily\s+simple\s+)?(?:sofr|tsfr|sf|s)", "SOFR"),
    (r"(?:\d+\s*-?\s*(?:m|mo|month)\s*)?(?:libor|l)", "LIBOR"),
    (r"(?:prime|p|us\s*prime)", "PRIME"),
    (r"(?:\d+\s*-?\s*(?:m|mo|month)\s*)?(?:euribor|e)", "EURIBOR"),
    (r"(?:sonia|sn)", "SONIA"),
    (r"(?:\d+\s*-?\s*(?:m|mo|month)\s*)?(?:saron|sa)", "SARON"),
    (r"(?:corra|cr)", "CORRA"),
    (r"(?:bbsw|bb|bbsy)", "BBSW"),
    (r"(?:nibor|n)", "NIBOR"),
    (r"(?:stibor|st)", "STIBOR"),
    (r"(?:cdor|c)", "CDOR"),
    (r"(?:cibor|ci)", "CIBOR"),
    (r"(?:tibor|t)", "TIBOR"),
    (r"(?:base\s+rate|br)", "BASE"),
    (r"(?:ameribor|am)", "AMERIBOR"),
    (r"(?:tonar|tona)", "TONAR"),
    (r"(?:wibor|w)", "WIBOR"),
    (r"(?:bkbm)", "BKBM"),
    (r"(?:ca)", "CA"),
    (r"(?:sr)", "SR"),
    (r"(?:b)", "B"),
]
_REF_GROUP = "|".join(f"(?P<r{i}>\\b{p}\\b)" for i, (p, _) in enumerate(REF_ALIASES))
REF_SPREAD_RE = re.compile(
    rf"(?:{_REF_GROUP})\s*(?:\((?:[a-z]|\d+m?)\))?\s*(?P<sign>[+-])\s*(?P<spread>\d+(?:\.\d+)?)\s*(?P<pct>%)?", re.I)
PCT = r"(\d+(?:\.\d+)?)\s*%"
PAREN_ALLIN_RE = re.compile(rf"\(\s*{PCT}\s*\)")
PIK_AFTER_RE = re.compile(rf"{PCT}\s*(?:pik|payment[- ]in[- ]kind)\b", re.I)
PIK_BEFORE_RE = re.compile(rf"\bpik\b\s*(?:interest|rate|dividend)?\s*(?:of\s*)?{PCT}", re.I)
PIK_WORD_RE = re.compile(r"\bpik\b|payment[- ]in[- ]kind", re.I)
FLOOR_AFTER_RE = re.compile(rf"{PCT}\s*floor\b", re.I)
FLOOR_BEFORE_RE = re.compile(rf"\bfloor\b\s*(?:rate)?\s*(?:of\s*)?{PCT}", re.I)
CASH_PIK_RE = re.compile(rf"{PCT}\s*cash\s*/?\s*{PCT}\s*pik", re.I)
FIXED_START_RE = re.compile(rf"^\s*(?:fixed\s*)?\$?\s*{PCT}")
NA_RE = re.compile(r"^\s*(n/?a|none|—|-|–|not applicable)?\s*$", re.I)


@dataclass
class Rate:
    reference_rate: str | None = None
    spread_bps: float | None = None
    all_in_rate: float | None = None
    is_pik: bool = False
    pik_rate: float | None = None
    floor: float | None = None
    rate_parse_failed: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _norm(s: str | None) -> str:
    s = (s or "").replace(" ", " ").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip()
    # "8.73 %" and "S +" -> tight forms
    s = re.sub(r"(\d)\s+%", r"\1%", s)
    return s


def pct(s: str | None) -> float | None:
    """A lone percent or number cell ("8.96%", "8.96 %", "4.00") -> float, else None."""
    s = _norm(s)
    m = re.match(rf"^\$?\s*{PCT}$", s) or re.match(r"^(\d+(?:\.\d+)?)$", s)
    return float(m.group(1)) if m else None


def parse_rate(text: str | None, all_in_text: str | None = None, pik_text: str | None = None,
               floor_text: str | None = None) -> Rate:
    """Parse one rate cell, plus optional split-column cells (coupon / PIK / floor)."""
    r = Rate()
    t = _norm(text)
    # Split columns first: they are explicit and override the regex where present.
    if all_in_text:
        a = _norm(all_in_text)
        m = CASH_PIK_RE.search(a)
        if m:
            r.all_in_rate = round(float(m.group(1)) + float(m.group(2)), 4)
            r.pik_rate, r.is_pik = float(m.group(2)), True
        else:
            r.all_in_rate = pct(a)
    if pik_text:
        p = pct(pik_text)
        if p is not None and p > 0:
            r.pik_rate, r.is_pik = p, True
        elif PIK_WORD_RE.search(pik_text):
            r.is_pik = True
    if floor_text:
        r.floor = pct(floor_text)

    if NA_RE.match(t):
        return r

    m = REF_SPREAD_RE.search(t)
    if m:
        idx = next(i for i in range(len(REF_ALIASES)) if m.group(f"r{i}"))
        r.reference_rate = REF_ALIASES[idx][1]
        v = float(m.group("spread"))
        r.spread_bps = v * 100 if (m.group("pct") or v < 30) else v
        if m.group("sign") == "-":
            r.spread_bps = -r.spread_bps
        rest = t[m.end():]
        a = PAREN_ALLIN_RE.search(rest)
        if a and r.all_in_rate is None:
            # "(11.25%)" right after the spread is the all-in; "(1.00% Floor)" is not
            after = rest[a.end():a.end() + 12].lower()
            inner = rest[a.start():a.end()].lower()
            if "floor" not in inner and "pik" not in inner and not after.startswith(("floor", " floor")):
                r.all_in_rate = float(a.group(1))
    else:
        cp = CASH_PIK_RE.search(t)
        f = FIXED_START_RE.match(t)
        if cp:
            r.reference_rate = "FIXED"
            r.all_in_rate = r.all_in_rate or round(float(cp.group(1)) + float(cp.group(2)), 4)
            r.pik_rate, r.is_pik = float(cp.group(2)), True
        elif f:
            r.reference_rate = "FIXED"
            if r.all_in_rate is None:
                r.all_in_rate = float(f.group(1))
        else:
            r.rate_parse_failed = True
            return r

    m = CASH_PIK_RE.search(t)
    if m and r.all_in_rate is None:
        r.all_in_rate = round(float(m.group(1)) + float(m.group(2)), 4)
    p = PIK_BEFORE_RE.search(t) or PIK_AFTER_RE.search(t)
    if p:
        r.pik_rate, r.is_pik = float(p.group(1)), True
    elif PIK_WORD_RE.search(t):
        r.is_pik = True
    fl = FLOOR_AFTER_RE.search(t) or FLOOR_BEFORE_RE.search(t)
    if fl and r.floor is None:
        r.floor = float(fl.group(1))
    return r
