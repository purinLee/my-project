import numpy as np
import pandas as pd
import pytest

from catalog_intel.quality import (
    detect_missing_images, detect_price_anomalies, find_duplicate_pairs, image_problem, normalize_title, run_audit,
)


def make_df(rows):
    base = {"merchant_id": "M-1", "brand": "B", "category": "Cat", "currency": "USD", "status": "active", "image_url": "https://x.io/a.jpg", "description": "d" * 30}
    out = []
    for i, r in enumerate(rows, start=1):
        out.append({**base, "product_id": i, "sku": f"S{i}", "title": f"Title number {i}", "price": 10.0, **r})
    return pd.DataFrame(out)


def test_normalize_title():
    assert normalize_title("  ACME  Pro-500!! ") == "acme pro 500"
    assert normalize_title(None) == ""


@pytest.mark.parametrize(
    "url,expected",
    [(None, "No image URL"), ("", "No image URL"), ("  ", "No image URL"), (np.nan, "No image URL"), ("not-a-url", "Invalid image URL"),
     ("ftp://x.io/a.jpg", "Invalid image URL"), ("https://x.io/placeholder/no-image.png", "Placeholder image"), ("https://x.io/a.jpg", None)],
)
def test_image_problem(url, expected):
    assert image_problem(url) == expected


def test_detect_missing_images_severity_by_status():
    df = make_df([{"image_url": None}, {"image_url": "", "status": "draft"}, {}])
    out = detect_missing_images(df)
    assert list(out["product_id"]) == [1, 2]
    assert list(out["severity"]) == ["High", "Medium"]


def test_price_anomaly_detects_high_and_low_outliers():
    prices = [100, 102, 98, 101, 99, 103, 97, 100, 104, 96, 1000, 8]
    df = make_df([{"price": p} for p in prices])
    out = detect_price_anomalies(df)
    assert set(out["product_id"]) == {11, 12}
    assert out["detail"].str.contains("above|below").all()


def test_price_anomaly_flags_invalid_prices_and_small_groups_are_skipped():
    df = make_df([{"price": 0}, {"price": -5}, {"price": None}, {"price": 10}])
    out = detect_price_anomalies(df)
    assert set(out["issue_type"]) == {"Invalid Price"} and len(out) == 3
    assert detect_price_anomalies(make_df([{"price": 1}, {"price": 1000}])).empty


def test_price_anomaly_groups_by_category():
    rows = [{"price": 10 + i % 3} for i in range(10)] + [{"category": "Other", "price": 5000 + i} for i in range(10)]
    assert detect_price_anomalies(make_df(rows)).empty


def test_duplicate_pairs_exact_near_and_numeric_guard():
    df = make_df([
        {"title": "Acme Pro 500 Headphones - Black"},
        {"title": "ACME  Pro 500 Headphones - Black!"},
        {"title": "Acme Pro 500 Headphone - Black"},
        {"title": "Acme Pro 510 Headphones - Black"},
        {"title": "Acme Pro 500 Headphones - Black", "merchant_id": "M-2"},
    ])
    pairs = find_duplicate_pairs(df)
    got = {(r.product_id_a, r.product_id_b): r.match_type for r in pairs.itertuples()}
    assert got[(1, 2)] == "Exact (normalized)"
    assert got[(1, 3)] == "Near duplicate"
    assert not any(4 in k for k in got), "different model number must not be a duplicate"
    assert not any(5 in k for k in got), "duplicates are only detected within one merchant"


def test_audit_recall_against_injected_ground_truth(conn, seeded):
    stats = seeded[1]
    audit = run_audit(conn)
    iss = audit.issues

    def found(*types):
        d = iss[iss["issue_type"].isin(types)]
        return set(zip(d["merchant_id"], d["sku"]))

    dup, price, img = found("Duplicate Listing"), found("Price Anomaly", "Invalid Price"), found("Missing Image")
    assert len(stats.injected_duplicate_skus & dup) / len(stats.injected_duplicate_skus) >= 0.95
    assert len(stats.injected_price_anomaly_skus & price) / len(stats.injected_price_anomaly_skus) >= 0.90
    assert len(stats.injected_missing_image_skus & img) / len(stats.injected_missing_image_skus) >= 0.90
    # precision: the detectors should not flood users with false positives
    assert len(stats.injected_duplicate_skus & dup) / len(dup) >= 0.90
    assert len(stats.injected_price_anomaly_skus & price) / len(price) >= 0.80
    assert len(stats.injected_missing_image_skus & img) / len(img) >= 0.90


def test_audit_scores_and_summary_are_consistent(conn):
    a = run_audit(conn)
    s = a.summary
    assert s["products_audited"] == len(a.products) == len(a.product_scores)
    assert s["issues_total"] == sum(s["by_type"].values())
    assert a.product_scores["quality_score"].between(0, 100).all()
    flagged = (a.product_scores["issue_count"] > 0).sum()
    assert s["products_flagged"] == flagged
    assert a.merchant_scores["products"].sum() == len(a.products)


def test_audit_counts_match_sql(conn):
    """Independent SQL check of the missing-image count (no pandas / python URL parsing involved)."""
    a = run_audit(conn)
    sql_count = conn.execute(
        """SELECT COUNT(*) FROM products WHERE status != 'archived' AND (
             image_url IS NULL OR TRIM(image_url) = '' OR image_url NOT LIKE 'http%://%'
             OR LOWER(image_url) LIKE '%placeholder%' OR LOWER(image_url) LIKE '%no-image%')"""
    ).fetchone()[0]
    assert (a.issues["issue_type"] == "Missing Image").sum() == sql_count
