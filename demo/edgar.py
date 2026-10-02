"""Polite SEC EDGAR client for the demo: who we are, how fast we ask, what we keep.

Rules, all enforced here so no other module talks to SEC directly:
- Every request sends the User-Agent SEC asks for (name + email).
- At most 5 requests per second per process. SEC's limit is 10; half leaves room
  for several visitors on one Streamlit server.
- Every response is cached on disk, keyed by URL, so the same filing is fetched once.
- A filing larger than MAX_FILING_MB is refused before it is downloaded in full
  (see the justification next to the constant).

Rate-limit and caching ideas follow ~/Documents/Parallax/src/parallax/ingest.py
(EdgarClient, fetch_cached), rewritten smaller for the demo.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import requests

USER_AGENT = "Alexandre Cela alexandrecelap@gmail.com"
MAX_REQUESTS_PER_SECOND = 5

# 25 MB cap (MiB). Measured on the repo's own corpus of 1,805 BDC primary documents
# (download_bdc_filings.py output): median 6.1 MB, 95th percentile 21.1 MB, 99th
# percentile 32.5 MB, max 60.3 MB; 97.5% are at or under 25 MiB. Parsing holds two trees
# (lxml for tables, BeautifulSoup for footnotes). Observed peak process memory: 667 MB on a
# 24.5 MB filing, 804 MB on a 32.5 MB filing. Streamlit Community Cloud guarantees about
# 690 MB (2.7 GB max), so 25 MiB is the largest cap whose observed peak fits the floor.
MAX_FILING_MB = 25

CACHE_DIR = Path(tempfile.gettempdir()) / "bdc_demo_edgar_cache"

ACCESSION_RE = re.compile(r"\b(\d{10})-?(\d{2})-?(\d{6})\b")


class EdgarError(RuntimeError):
    """Any problem fetching from EDGAR, worded for the visitor."""


class FilingTooLarge(EdgarError):
    """The filing exceeds MAX_FILING_MB."""


class _RateLimiter:
    """Sliding one-second window shared by every thread in the process."""

    def __init__(self, per_second: int):
        self.per_second = per_second
        self.stamps: deque = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.stamps and now - self.stamps[0] > 1.0:
                self.stamps.popleft()
            if len(self.stamps) >= self.per_second:
                time.sleep(1.0 - (now - self.stamps[0]) + 0.01)
            self.stamps.append(time.monotonic())


_LIMITER = _RateLimiter(MAX_REQUESTS_PER_SECOND)
REQUEST_LOG: list[dict] = []   # url, status, bytes, cached: shown in the app's fetch step


def _cache_path(url: str) -> Path:
    return CACHE_DIR / hashlib.sha256(url.encode()).hexdigest()[:32]


def get(url: str, max_mb: float | None = None, timeout: int = 60) -> bytes:
    """GET with User-Agent, rate limit, disk cache and an optional size cap."""
    path = _cache_path(url)
    if path.exists():
        data = path.read_bytes()
        REQUEST_LOG.append({"url": url, "status": "cache", "bytes": len(data), "cached": True})
        return data
    _LIMITER.wait()
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                            timeout=timeout, stream=True)
    except requests.RequestException as exc:
        raise EdgarError(f"Could not reach SEC EDGAR: {exc}") from exc
    if resp.status_code == 404:
        raise EdgarError(f"SEC returned 404 for {url}")
    if resp.status_code in (403, 429):
        raise EdgarError(f"SEC refused the request ({resp.status_code}). Try again in a minute.")
    if resp.status_code != 200:
        raise EdgarError(f"SEC returned {resp.status_code} for {url}")
    cap = int(max_mb * 1024 * 1024) if max_mb else None
    declared = resp.headers.get("Content-Length")
    if cap and declared and declared.isdigit() and int(declared) > cap and "gzip" not in resp.headers.get("Content-Encoding", ""):
        resp.close()
        raise FilingTooLarge(f"Filing is {int(declared) / 1e6:.1f} MB; the demo cap is {max_mb} MB.")
    chunks, size = [], 0
    for chunk in resp.iter_content(1 << 16):
        chunks.append(chunk)
        size += len(chunk)
        if cap and size > cap:
            resp.close()
            raise FilingTooLarge(f"Filing is over {max_mb} MB (the demo cap); stopped downloading.")
    data = b"".join(chunks)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    REQUEST_LOG.append({"url": url, "status": resp.status_code, "bytes": len(data), "cached": False})
    return data


def get_json(url: str) -> dict:
    return json.loads(get(url).decode("utf-8"))


# ---------------------------------------------------------------------------
# Resolving what the visitor typed into one filing
# ---------------------------------------------------------------------------

@dataclass
class FilingRef:
    cik: int
    accession: str            # 0001234567-24-000123
    form: str = ""
    report_date: str = ""     # YYYY-MM-DD from EDGAR, "" if unknown
    filing_date: str = ""
    primary_document: str = ""
    company: str = ""

    @property
    def folder_url(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{self.cik}/{self.accession.replace('-', '')}/"

    @property
    def document_url(self) -> str:
        return self.folder_url + self.primary_document

    @property
    def index_url(self) -> str:
        return self.folder_url + f"{self.accession}-index.htm"


def normalise_accession(text: str) -> str | None:
    m = ACCESSION_RE.search(text or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def submissions(cik: int) -> dict:
    return get_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")


def ticker_to_cik(ticker: str) -> int | None:
    data = get_json("https://www.sec.gov/files/company_tickers.json")
    t = ticker.strip().upper()
    for row in data.values():
        if row.get("ticker", "").upper() == t:
            return int(row["cik_str"])
    return None


def list_filings(cik: int, forms: tuple[str, ...] = ("10-K", "10-Q"), limit: int = 20) -> tuple[str, list[FilingRef]]:
    """Recent 10-K and 10-Q filings for one CIK, newest first. Returns (company name, filings)."""
    sub = submissions(cik)
    recent = sub.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in forms:
            continue
        out.append(FilingRef(
            cik=int(cik), accession=recent["accessionNumber"][i], form=form,
            report_date=recent.get("reportDate", [""] * (i + 1))[i] or "",
            filing_date=recent.get("filingDate", [""] * (i + 1))[i] or "",
            primary_document=recent.get("primaryDocument", [""] * (i + 1))[i] or "",
            company=sub.get("name", ""),
        ))
        if len(out) >= limit:
            break
    return sub.get("name", ""), out


def _fill_from_submissions(ref: FilingRef) -> FilingRef:
    """Form, period and primary document from the submissions JSON (recent block only)."""
    try:
        name, filings = list_filings(ref.cik, forms=("10-K", "10-Q", "10-K/A", "10-Q/A"), limit=400)
    except EdgarError:
        return ref
    ref.company = ref.company or name
    for f in filings:
        if f.accession == ref.accession:
            ref.form = ref.form or f.form
            ref.report_date = ref.report_date or f.report_date
            ref.filing_date = ref.filing_date or f.filing_date
            ref.primary_document = ref.primary_document or f.primary_document
    return ref


def _primary_from_index(ref: FilingRef) -> str:
    """Largest .htm in the filing folder whose name doesn't look like an exhibit."""
    idx = get_json(ref.folder_url + "index.json")
    items = idx.get("directory", {}).get("item", [])
    htm = [i for i in items if i.get("name", "").lower().endswith((".htm", ".html"))
           and not re.search(r"(^|[_-])(ex|exhibit)\d|index", i["name"], re.I)]
    if not htm:
        raise EdgarError("No HTML document found in the filing folder.")
    htm.sort(key=lambda i: int(i.get("size") or 0), reverse=True)
    return htm[0]["name"]


def resolve(text: str, cik_hint: str = "") -> FilingRef:
    """Turn a SEC URL, or an accession number (+ optional CIK), into a FilingRef."""
    text = (text or "").strip()
    url_m = re.search(r"/Archives/edgar/data/(\d+)/(\d{18})(?:/([^?#]+))?", text)
    if url_m:
        cik = int(url_m.group(1))
        raw = url_m.group(2)
        acc = f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"
        doc = url_m.group(3) or ""
        if doc.endswith("-index.htm") or doc.endswith("-index.html") or doc.endswith(".txt") or "/" in doc:
            doc = ""
        ref = _fill_from_submissions(FilingRef(cik=cik, accession=acc, primary_document=doc))
    else:
        acc = normalise_accession(text)
        if not acc:
            raise EdgarError("Paste a SEC EDGAR filing URL or an accession number like 0001234567-24-000123.")
        cik_candidates = [int(cik_hint)] if str(cik_hint).strip().isdigit() else []
        cik_candidates.append(int(acc[:10]))   # self-filers: the accession prefix is the CIK
        ref = None
        for cik in cik_candidates:
            cand = _fill_from_submissions(FilingRef(cik=cik, accession=acc))
            if cand.primary_document:
                ref = cand
                break
        if ref is None:
            raise EdgarError("Could not find that accession. Add the filer's CIK, or paste the filing URL.")
    if not ref.primary_document:
        ref.primary_document = _primary_from_index(ref)
    return ref
