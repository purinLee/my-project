"""Excel-style baseline checks: what an analyst can do with built-in spreadsheet features, written as code.

These are SIMULATED PROCEDURES, not measurements of people. They exist so detection quality (omissions) can be compared
on identical data and ground truth: blank-cell counts, "Remove Duplicates" on a trimmed/case-insensitive title, an IQR
outlier fence on the whole price column, and blank-image filtering. Their results are exact for the procedure, but they do
not say how any particular person would perform.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

REQUIRED = ["sku", "title", "price"]


def _blank(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip() == ""


def naive_invalid_rows(raw: pd.DataFrame) -> int:
    """Rows a COUNTBLANK-style check catches: any required cell empty. Misses non-numeric/negative prices and 1-char titles."""
    return int(sum(_blank(raw[c]) for c in REQUIRED).gt(0).sum())


def naive_duplicates(raw: pd.DataFrame) -> set[tuple[str, str]]:
    """Excel 'Remove Duplicates' on Title (case-insensitive, whitespace trimmed), keeping the first row per merchant."""
    key = raw["merchant_id"] + "|" + raw["title"].fillna("").str.lower().map(lambda t: re.sub(r"\s+", " ", t).strip())
    dup = key.duplicated(keep="first") & ~_blank(raw["title"])
    return set(zip(raw.loc[dup, "merchant_id"], raw.loc[dup, "sku"]))


def naive_price_outliers(raw: pd.DataFrame) -> set[tuple[str, str]]:
    """1.5 x IQR fence over the whole price column (sorting one column), ignoring categories."""
    price = pd.to_numeric(raw["price"], errors="coerce")
    ok = price.notna() & (price > 0)
    q1, q3 = np.percentile(price[ok], [25, 75])
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    flag = ok & ((price < lo) | (price > hi))
    return set(zip(raw.loc[flag, "merchant_id"], raw.loc[flag, "sku"]))


def naive_missing_images(raw: pd.DataFrame) -> set[tuple[str, str]]:
    """Filter blank image cells. Misses placeholder images and malformed URLs."""
    flag = _blank(raw["image_url"]) & ~_blank(raw["sku"])
    return set(zip(raw.loc[flag, "merchant_id"], raw.loc[flag, "sku"]))


def score_sets(truth: set, flagged: set) -> dict:
    tp = len(truth & flagged)
    return {"injected": len(truth), "flagged": len(flagged), "true_positives": tp, "missed": len(truth) - tp,
            "recall": round(tp / len(truth), 4) if truth else None, "precision": round(tp / len(flagged), 4) if flagged else None}


def score_pairs(pairs: set[tuple[str, str, str]], flagged: set) -> dict:
    """A duplicate pair counts as found if either member is flagged (which of the two is kept is arbitrary)."""
    members = {(m, s) for m, a, b in pairs for s in (a, b)}
    found = sum(((m, a) in flagged) or ((m, b) in flagged) for m, a, b in pairs)
    relevant_flags = len(flagged & members)
    return {"injected": len(pairs), "flagged": len(flagged), "true_positives": found, "missed": len(pairs) - found,
            "recall": round(found / len(pairs), 4) if pairs else None, "precision": round(relevant_flags / len(flagged), 4) if flagged else None}
