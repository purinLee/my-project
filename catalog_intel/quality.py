"""Catalog quality audit: duplicates, price anomalies, missing images, metadata completeness."""
from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from .config import NEAR_DUPLICATE_THRESHOLD, PRICE_MIN_GROUP_SIZE, PRICE_MODIFIED_Z_THRESHOLD
from .db import query_df

ISSUE_COLUMNS = ["product_id", "merchant_id", "sku", "title", "category", "issue_type", "severity", "detail"]
PLACEHOLDER_HINTS = ("placeholder", "no-image", "noimage", "no_image", "coming-soon", "default.")
SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
SEVERITY_PENALTY = {"Critical": 40, "High": 25, "Medium": 12, "Low": 5}


def load_products(conn, include_archived: bool = False) -> pd.DataFrame:
    sql = "SELECT p.*, m.name AS merchant_name FROM products p JOIN merchants m USING (merchant_id)"
    if not include_archived:
        sql += " WHERE p.status != 'archived'"
    return query_df(conn, sql + " ORDER BY p.product_id")


def normalize_title(title: str | None) -> str:
    """Lowercase, strip punctuation and collapse whitespace so cosmetic variants compare equal."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())).strip()


# --- Missing / invalid images -----------------------------------------------
def image_problem(url) -> str | None:
    """Return a reason string when the image reference is unusable, else None."""
    if url is None or (isinstance(url, float) and np.isnan(url)) or not str(url).strip():
        return "No image URL"
    value = str(url).strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Invalid image URL"
    if any(h in value.lower() for h in PLACEHOLDER_HINTS):
        return "Placeholder image"
    return None


def detect_missing_images(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in df.itertuples():
        reason = image_problem(r.image_url)
        if reason:
            rows.append(_issue(r, "Missing Image", "High" if r.status == "active" else "Medium", reason))
    return _frame(rows)


# --- Price anomalies --------------------------------------------------------
def detect_price_anomalies(
    df: pd.DataFrame, z_threshold: float = PRICE_MODIFIED_Z_THRESHOLD, min_group: int = PRICE_MIN_GROUP_SIZE
) -> pd.DataFrame:
    """Flag invalid prices and per-category outliers.

    Outliers use the modified z-score (median/MAD) on log-price per (category, currency): robust to the
    anomalies themselves and symmetric for too-high / too-low prices.
    """
    rows = []
    price = pd.to_numeric(df["price"], errors="coerce")
    for r in df[price.isna() | (price <= 0)].itertuples():
        rows.append(_issue(r, "Invalid Price", "Critical", f"Price is {'missing' if pd.isna(r.price) else r.price}"))
    valid = df[price > 0].copy()
    valid["log_price"] = np.log(price[price > 0])
    for (_cat, _cur), grp in valid.groupby([valid["category"].fillna("(none)"), "currency"]):
        if len(grp) < min_group:
            continue
        med = grp["log_price"].median()
        mad = (grp["log_price"] - med).abs().median()
        if mad == 0:
            z = pd.Series(np.where(grp["log_price"] == med, 0.0, np.inf), index=grp.index)
        else:
            z = 0.6745 * (grp["log_price"] - med) / mad
        median_price = float(np.exp(med))
        flagged = grp.index[z.abs() > z_threshold]
        for r in df.loc[flagged].itertuples():
            idx = r.Index
            ratio = r.price / median_price
            direction = "above" if ratio > 1 else "below"
            factor = ratio if ratio > 1 else 1 / ratio
            sev = "High" if factor >= 5 else "Medium"
            rows.append(_issue(r, "Price Anomaly", sev, f"{r.currency} {r.price:,.2f} is {factor:.1f}x {direction} category median {median_price:,.2f} (z={z[idx]:.1f})"))
    return _frame(rows)


# --- Duplicates -------------------------------------------------------------
def _digits(text: str) -> list[str]:
    return sorted(re.findall(r"\d+", text))


TOKEN_MATCH_RATIO = 0.75


def _similarity(a: str, b: str) -> float:
    """Token-order-insensitive similarity. Titles whose numbers differ (model 500 vs 510, size 9 vs 10,
    256GB vs 512GB) are different products, however similar the rest of the text is. When both titles have the
    same number of words, every differing word pair must itself be a near-match (a typo or plural, not a
    different word such as 'Pro' vs 'Max')."""
    if a == b:
        return 1.0
    if _digits(a) != _digits(b):
        return 0.0
    ta, tb = sorted(a.split()), sorted(b.split())
    if len(ta) == len(tb):
        for x, y in zip(ta, tb):
            if x != y and SequenceMatcher(None, x, y).ratio() < TOKEN_MATCH_RATIO:
                return 0.0
    elif abs(len(ta) - len(tb)) > 1:
        return 0.0
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def find_duplicate_pairs(df: pd.DataFrame, threshold: float = NEAR_DUPLICATE_THRESHOLD) -> pd.DataFrame:
    """Return candidate duplicate pairs within each merchant.

    Blocking keeps this near-linear: only products with the same (merchant, brand, category) AND the same set of
    numbers in the title are compared, because titles whose numbers differ can never match (see `_similarity`).
    """
    # Pure-Python blocking: at 100k rows, iterating pandas groups one by one (or building a DataFrame per block)
    # dominated the runtime, while most blocks hold a single product.
    blocks: dict[tuple, list] = defaultdict(list)
    for r in df[["product_id", "merchant_id", "brand", "category", "sku", "title"]].itertuples(index=False):
        norm = normalize_title(r.title)
        key = (r.merchant_id, r.brand if isinstance(r.brand, str) else "", r.category if isinstance(r.category, str) else "", tuple(_digits(norm)))
        blocks[key].append((r, norm))
    pairs = []
    for members in blocks.values():
        if len(members) < 2:
            continue
        for (a, a_norm), (b, b_norm) in combinations(members, 2):
            sim = _similarity(a_norm, b_norm)
            if sim >= threshold:
                first, second = (a, b) if a.product_id < b.product_id else (b, a)
                pairs.append(
                    {
                        "merchant_id": a.merchant_id, "product_id_a": first.product_id, "sku_a": first.sku, "title_a": first.title,
                        "product_id_b": second.product_id, "sku_b": second.sku, "title_b": second.title,
                        "similarity": round(sim, 3), "match_type": "Exact (normalized)" if sim == 1.0 else "Near duplicate",
                    }
                )
    cols = ["merchant_id", "product_id_a", "sku_a", "title_a", "product_id_b", "sku_b", "title_b", "similarity", "match_type"]
    return pd.DataFrame(pairs, columns=cols).sort_values("similarity", ascending=False, ignore_index=True)


def detect_duplicates(df: pd.DataFrame, pairs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cluster duplicate pairs (union-find) and flag every member except the oldest as the duplicate."""
    pairs = find_duplicate_pairs(df) if pairs is None else pairs
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs.itertuples():
        parent[find(p.product_id_b)] = find(p.product_id_a)
    clusters: dict[int, list[int]] = {}
    for pid in list(parent):
        clusters.setdefault(find(pid), []).append(pid)
    involved = df[df["product_id"].isin(parent)]
    by_id = {r.product_id: r for r in involved.itertuples()}
    rows = []
    for members in clusters.values():
        keep = min(members)
        for pid in sorted(members):
            if pid == keep:
                continue
            rows.append(_issue(by_id[pid], "Duplicate Listing", "High", f"Duplicate of {by_id[keep].sku} ({by_id[keep].title})"))
    return _frame(rows)


# --- Metadata completeness --------------------------------------------------
def detect_incomplete_metadata(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in df.itertuples():
        desc = (r.description or "").strip() if isinstance(r.description, str) else ""
        if len(desc) < 20:
            rows.append(_issue(r, "Incomplete Metadata", "Medium", "Description missing or shorter than 20 characters"))
        if not (isinstance(r.brand, str) and r.brand.strip()):
            rows.append(_issue(r, "Incomplete Metadata", "Medium", "Brand is missing"))
        if len((r.title or "").strip()) < 10:
            rows.append(_issue(r, "Incomplete Metadata", "Low", "Title shorter than 10 characters"))
    return _frame(rows)


def _issue(r, issue_type: str, severity: str, detail: str) -> dict:
    return {
        "product_id": r.product_id, "merchant_id": r.merchant_id, "sku": r.sku, "title": r.title,
        "category": r.category, "issue_type": issue_type, "severity": severity, "detail": detail,
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=ISSUE_COLUMNS)


@dataclass
class AuditResult:
    products: pd.DataFrame
    issues: pd.DataFrame
    duplicate_pairs: pd.DataFrame
    product_scores: pd.DataFrame
    merchant_scores: pd.DataFrame

    @property
    def summary(self) -> dict:
        n = len(self.products)
        flagged = self.issues["product_id"].nunique() if len(self.issues) else 0
        by_type = self.issues["issue_type"].value_counts().to_dict() if len(self.issues) else {}
        return {
            "products_audited": n,
            "products_flagged": int(flagged),
            "clean_rate": round(100 * (n - flagged) / n, 1) if n else 100.0,
            "avg_quality_score": round(float(self.product_scores["quality_score"].mean()), 1) if n else 100.0,
            "issues_total": int(len(self.issues)),
            "by_type": by_type,
        }


def run_audit(
    conn, z_threshold: float = PRICE_MODIFIED_Z_THRESHOLD, dup_threshold: float = NEAR_DUPLICATE_THRESHOLD, timings: dict | None = None
) -> AuditResult:
    """Run every detector. If `timings` is given it is filled with seconds per detector (load, duplicates, price, image, metadata)."""
    t = timings if timings is not None else {}

    def timed(name, fn):
        t0 = time.perf_counter()
        out = fn()
        t[name] = t.get(name, 0.0) + time.perf_counter() - t0
        return out

    products = timed("load", lambda: load_products(conn))
    pairs = timed("duplicates", lambda: find_duplicate_pairs(products, dup_threshold) if len(products) else pd.DataFrame())
    parts = [
        timed("duplicates", lambda: detect_duplicates(products, pairs)), timed("price", lambda: detect_price_anomalies(products, z_threshold)),
        timed("image", lambda: detect_missing_images(products)), timed("metadata", lambda: detect_incomplete_metadata(products)),
    ]
    issues = pd.concat(parts, ignore_index=True) if products.shape[0] else _frame([])
    issues["_o"] = issues["severity"].map(SEVERITY_ORDER)
    issues = issues.sort_values(["_o", "issue_type", "merchant_id"], ignore_index=True).drop(columns="_o")
    return AuditResult(products, issues, pairs, *_scores(products, issues))


def _scores(products: pd.DataFrame, issues: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    penalty = issues.assign(p=issues["severity"].map(SEVERITY_PENALTY)).groupby("product_id")["p"].sum() if len(issues) else pd.Series(dtype=float)
    ps = products[["product_id", "merchant_id", "merchant_name", "sku", "title", "category"]].copy()
    ps["issue_count"] = ps["product_id"].map(issues.groupby("product_id").size() if len(issues) else {}).fillna(0).astype(int)
    ps["quality_score"] = (100 - ps["product_id"].map(penalty).fillna(0)).clip(lower=0)
    ms = ps.groupby(["merchant_id", "merchant_name"]).agg(
        products=("product_id", "count"), avg_quality_score=("quality_score", "mean"),
        flagged_products=("issue_count", lambda s: int((s > 0).sum())),
    ).reset_index()
    ms["clean_rate"] = (100 * (1 - ms["flagged_products"] / ms["products"])).round(1)
    ms["avg_quality_score"] = ms["avg_quality_score"].round(1)
    return ps, ms.sort_values("avg_quality_score", ignore_index=True)
