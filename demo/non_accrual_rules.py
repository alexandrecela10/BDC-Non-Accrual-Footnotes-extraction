"""Fallback copy of classify_text from parsers/non_accrual_tagger.py (this repo).

parsers/non_accrual_tagger.py is untracked in git at the time of writing, so a fresh clone
(and Streamlit Community Cloud) would not have it. footnotes.py imports the repo file first
and uses this copy only when the import fails. Logic copied unchanged.
"""
import re
from typing import List

_NON_ACCRUAL_RE = re.compile(r"non[\s\-]*accrual", re.IGNORECASE)
_IN_DEFAULT_RE = re.compile(
    r"\b(?:in\s+default|payment\s+default|principal\s+default|interest\s+default|in\s+payment\s+default)\b",
    re.IGNORECASE,
)
_MIN_TEXT_LEN = 15


def classify_text(text: str) -> List[str]:
    if not text or len(text.strip()) < _MIN_TEXT_LEN:
        return []
    cats: List[str] = []
    if _NON_ACCRUAL_RE.search(text):
        cats.append("non-accrual")
    if _IN_DEFAULT_RE.search(text):
        cats.append("in default")
    return cats
