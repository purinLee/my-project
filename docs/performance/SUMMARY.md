# Performance summary

Every number below is copied from the JSON files in this folder, produced by `scripts/benchmark.py`, `scripts/benchmark_ui.py` and `scripts/verify_llm.py`. **MEASURED** = timed on the machine listed in each JSON (single run, wall clock). All data is **synthetic**; nothing here is simulated timing, but results will differ on other hardware (Streamlit Community Cloud is slower than a local 8-core PC).

Machine: Windows-10-10.0.19045-SP0, 8 logical CPUs, 16.0 GB RAM, Python 3.10.4, pandas 2.3.3, SQLite 3.37.2.

## 1. Quality audit: before vs after optimization (same code path, same data)

| Catalog size | Version | find_duplicate_pairs (s) | run_audit total (s) | Peak RSS (MB) | Duplicate precision | Duplicate recall |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 10,000 | baseline | 4.063 | 7.474 | 117.0 | 0.943 | 1.0 |
| 10,000 | optimized | 0.2 | 0.614 | 114.4 | 1.0 | 1.0 |
| 25,000 | baseline | 24.658 | 29.468 | 152.4 | 0.831 | 1.0 |
| 25,000 | optimized | 0.558 | 1.588 | 153.8 | 1.0 | 1.0 |
| 100,000 | optimized | 4.786 | 8.295 | 383.4 | 1.0 | 1.0 |

The baseline at 100,000 rows was not run to completion: its all-pairs-per-block comparison grows quadratically (2.5x more rows took about 6x longer for the pair search), which extrapolates to many minutes. Precision also fell with size in the baseline because more titles differing only by one word (e.g. 'Pro' vs 'Max') cleared the similarity threshold; the optimized matcher rejects those.

## 2. 100,000 products / 50,000 requests / 316,221 events

| Step | Seconds | Peak RSS MB |
| --- | ---: | ---: |
| generate_database | 42.487 | 348.0 |
| audit_load_products | 1.367 | 301.1 |
| audit_find_duplicate_pairs | 4.786 | 274.8 |
| audit_detect_duplicates | 0.068 | 271.4 |
| audit_detect_price_anomalies | 0.383 | 299.2 |
| audit_detect_missing_images | 1.904 | 271.3 |
| audit_detect_incomplete_metadata | 0.506 | 271.6 |
| audit_run_audit_total | 8.295 | 383.4 |
| sql_phase_total | 26.983 | 416.7 |
| upload_build_xlsx_15k | 5.327 | 373.6 |
| upload_validate_csv_15k | 0.667 | 401.6 |
| upload_validate_xlsx_15k | 5.394 | 377.2 |
| upload_import_15k_rows | 0.595 | 378.0 |
| export_csv_full_catalog | 2.174 | 398.1 |
| export_xlsx_full_catalog | 49.738 | 409.8 |

Process peak RSS: **416.7 MB**. Database file: 109.8 MB.

### Detector accuracy against injected ground truth

| Detector | Injected | Flagged | Recall | Precision |
| --- | ---: | ---: | ---: | ---: |
| duplicate | 3423 | 3423 | 1.0 | 1.0 |
| price_anomaly | 3915 | 4079 | 0.999 | 0.959 |
| missing_image | 7824 | 7885 | 0.973 | 0.966 |

### SQL (5 runs each, median ms)

| Query | Median ms | Max ms | Rows |
| --- | ---: | ---: | ---: |
| analytics:processing_time_by_type | 150.32 | 211.53 | 8 |
| analytics:rework_by_type | 86.05 | 100.08 | 8 |
| analytics:sla_by_priority | 109.12 | 122.36 | 4 |
| analytics:weekly_volume | 284.15 | 331.92 | 18 |
| analytics:status_funnel | 5.33 | 8.73 | 8 |
| analytics:stage_durations | 1348.85 | 1457.47 | 6 |
| analytics:backlog_aging | 30.55 | 44.76 | 3235 |
| analytics:merchant_leaderboard | 402.14 | 526.12 | 12 |
| analytics:assignee_workload | 6.64 | 11.51 | 9 |
| analytics:catalog_by_merchant | 193.24 | 218.24 | 12 |
| products:category_counts | 346.84 | 385.08 | 12 |
| products:merchant_category_avg_price | 296.89 | 324.17 | 32 |
| products:top_100_by_price | 119.87 | 143.86 | 100 |
| products:title_search_like | 124.66 | 136.84 | 1 |
| products:sku_lookup_unique_index | 0.03 | 0.23 | 1 |
| products:window_top10_per_category | 550.06 | 566.32 | 1 |
| products:join_tier_summary | 207.6 | 227.47 | 3 |
| products:missing_image_count | 280.18 | 284.72 | 1 |
| products:exact_title_duplicates | 511.71 | 534.11 | 1 |
| kpi_summary (single run) | 400.52 | | |
| reconcile_sql_vs_pandas (single run) | 1136.42 | | |

SKU lookup plan: `['SEARCH products USING INDEX sqlite_autoindex_products_1 (merchant_id=? AND sku=?)']`

Upload path: 15,000 rows -> CSV 4.1 MB, XLSX 0.8 MB. A 20,000-row CSV of this shape is 5.43 MB, which EXCEEDS the 5 MB upload limit, so the effective limit is the size, not the row count.

SQL vs audit cross-checks: `{"missing_images": {"sql": 7885, "audit": 7885, "match": true}, "invalid_prices": {"sql": 3, "audit": 3, "match": true}, "reconcile_all_match": true}`

## 3. Streamlit page render time on the 100,000-product database

MEASURED server-side script time (AppTest), excludes browser rendering. Cold = first run in the process (imports, audit); warm = second run (audit cached).

| Page | Cold (s) | Warm (s) |
| --- | ---: | ---: |
| Executive Overview | 21.56 | 2.53 |
| Catalog Management | 2.0 | 1.87 |
| Catalog Quality Audit | 2.12 | 2.18 |
| Merchant Requests | 2.56 | 2.4 |
| SQL Operations Analytics | 3.51 | 3.67 |
| AI Operations Assistant | 0.27 | 0.24 |

## 4. LLM verification status

- `llm_verification_20261126T155905Z_skipped_no_api_key.json`: **SKIPPED - no API key, no real LLM call was made.** Fallback check: all 16 requests handled by rules (held-out accuracy 1.0).
