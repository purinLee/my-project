# Benchmark `optimized` - 100,000 products / 50,000 requests

**MEASURED on synthetic data (not simulated timings).** Measured 2026-11-26 15:48:36 UTC at commit `295e928` on Windows-10-10.0.19045-SP0 (8 logical CPUs, 16.0 GB RAM), Python 3.10.4, pandas 2.3.3, SQLite 3.37.2. Database file 109.8 MB. Memory = process RSS sampled every 20 ms.

## Steps (wall time and memory)

| Step | Seconds | Peak RSS MB | Peak growth MB |
| --- | ---: | ---: | ---: |
| generate_database | 42.487 | 348.0 | 267.8 |
| audit_load_products | 1.367 | 301.1 | 57.9 |
| audit_find_duplicate_pairs | 4.786 | 274.8 | 9.3 |
| audit_detect_duplicates | 0.068 | 271.4 | 0.7 |
| audit_detect_price_anomalies | 0.383 | 299.2 | 30.3 |
| audit_detect_missing_images | 1.904 | 271.3 | 1.4 |
| audit_detect_incomplete_metadata | 0.506 | 271.6 | 0.4 |
| audit_run_audit_total | 8.295 | 383.4 | 111.7 |
| sql_phase_total | 26.983 | 416.7 | 56.7 |
| upload_build_xlsx_15k | 5.327 | 373.6 | 2.6 |
| upload_validate_csv_15k | 0.667 | 401.6 | 28.0 |
| upload_validate_xlsx_15k | 5.394 | 377.2 | 5.3 |
| upload_import_15k_rows | 0.595 | 378.0 | 0.8 |
| export_csv_full_catalog | 2.174 | 398.1 | 31.4 |
| export_xlsx_full_catalog | 49.738 | 409.8 | 11.7 |

## Detector accuracy vs injected ground truth

| Detector | Injected | Flagged | Recall | Precision |
| --- | ---: | ---: | ---: | ---: |
| duplicate | 3423 | 3423 | 1.0 | 1.0 |
| price_anomaly | 3915 | 4079 | 0.999 | 0.959 |
| missing_image | 7824 | 7885 | 0.973 | 0.966 |

## SQL (5 runs each)

| Query | Median ms | Min ms | Max ms | Rows |
| --- | ---: | ---: | ---: | ---: |
| analytics:processing_time_by_type | 150.32 | 136.5 | 211.53 | 8 |
| analytics:rework_by_type | 86.05 | 77.95 | 100.08 | 8 |
| analytics:sla_by_priority | 109.12 | 101.58 | 122.36 | 4 |
| analytics:weekly_volume | 284.15 | 278.94 | 331.92 | 18 |
| analytics:status_funnel | 5.33 | 4.42 | 8.73 | 8 |
| analytics:stage_durations | 1348.85 | 1236.32 | 1457.47 | 6 |
| analytics:backlog_aging | 30.55 | 21.98 | 44.76 | 3235 |
| analytics:merchant_leaderboard | 402.14 | 370.38 | 526.12 | 12 |
| analytics:assignee_workload | 6.64 | 6.22 | 11.51 | 9 |
| analytics:catalog_by_merchant | 193.24 | 183.17 | 218.24 | 12 |
| products:category_counts | 346.84 | 314.96 | 385.08 | 12 |
| products:merchant_category_avg_price | 296.89 | 273.43 | 324.17 | 32 |
| products:top_100_by_price | 119.87 | 115.22 | 143.86 | 100 |
| products:title_search_like | 124.66 | 117.04 | 136.84 | 1 |
| products:sku_lookup_unique_index | 0.03 | 0.03 | 0.23 | 1 |
| products:window_top10_per_category | 550.06 | 477.57 | 566.32 | 1 |
| products:join_tier_summary | 207.6 | 185.56 | 227.47 | 3 |
| products:missing_image_count | 280.18 | 255.69 | 284.72 | 1 |
| products:exact_title_duplicates | 511.71 | 465.11 | 534.11 | 1 |
| kpi_summary (single run) | 400.52 | | | |
| reconcile_sql_vs_pandas (single run) | 1136.42 | | | |

Index plan for the SKU lookup: `['SEARCH products USING INDEX sqlite_autoindex_products_1 (merchant_id=? AND sku=?)']`

## Cross-checks

`{"missing_images": {"sql": 7885, "audit": 7885, "match": true}, "invalid_prices": {"sql": 3, "audit": 3, "match": true}, "reconcile_all_match": true}`
