# Benchmark `optimized` - 10,000 products / 5,000 requests

**MEASURED on synthetic data (not simulated timings).** Measured 2026-11-26 15:47:38 UTC at commit `295e928` on Windows-10-10.0.19045-SP0 (8 logical CPUs, 16.0 GB RAM), Python 3.10.4, pandas 2.3.3, SQLite 3.37.2. Database file 9.5 MB. Memory = process RSS sampled every 20 ms.

## Steps (wall time and memory)

| Step | Seconds | Peak RSS MB | Peak growth MB |
| --- | ---: | ---: | ---: |
| audit_load_products | 0.094 | 96.9 | 15.4 |
| audit_find_duplicate_pairs | 0.2 | 100.7 | 3.8 |
| audit_detect_duplicates | 0.009 | 100.8 | 0.1 |
| audit_detect_price_anomalies | 0.07 | 103.9 | 3.2 |
| audit_detect_missing_images | 0.189 | 101.8 | 0.1 |
| audit_detect_incomplete_metadata | 0.027 | 101.8 | 0.0 |
| audit_run_audit_total | 0.614 | 114.4 | 12.6 |
| sql_phase_total | 2.041 | 115.8 | 2.5 |
| upload_build_xlsx_15k | 3.242 | 120.8 | 0.1 |
| upload_validate_csv_15k | 0.384 | 139.2 | 18.5 |
| upload_validate_xlsx_15k | 3.947 | 139.2 | 12.5 |
| upload_import_15k_rows | 0.443 | 139.6 | 2.4 |
| export_csv_full_catalog | 0.204 | 133.5 | 7.3 |
| export_xlsx_full_catalog | 5.276 | 132.8 | 2.7 |

## Detector accuracy vs injected ground truth

| Detector | Injected | Flagged | Recall | Precision |
| --- | ---: | ---: | ---: | ---: |
| duplicate | 349 | 349 | 1.0 | 1.0 |
| price_anomaly | 402 | 417 | 1.0 | 0.964 |
| missing_image | 799 | 799 | 0.965 | 0.965 |

## SQL (5 runs each)

| Query | Median ms | Min ms | Max ms | Rows |
| --- | ---: | ---: | ---: | ---: |
| analytics:processing_time_by_type | 5.06 | 4.91 | 9.25 | 8 |
| analytics:rework_by_type | 2.46 | 2.29 | 2.81 | 8 |
| analytics:sla_by_priority | 3.31 | 3.03 | 5.34 | 4 |
| analytics:weekly_volume | 14.48 | 13.7 | 19.41 | 18 |
| analytics:status_funnel | 0.46 | 0.44 | 0.83 | 8 |
| analytics:stage_durations | 132.15 | 126.79 | 149.07 | 6 |
| analytics:backlog_aging | 1.68 | 1.56 | 2.45 | 314 |
| analytics:merchant_leaderboard | 3.08 | 2.95 | 6.93 | 12 |
| analytics:assignee_workload | 0.27 | 0.26 | 0.44 | 9 |
| analytics:catalog_by_merchant | 17.43 | 13.12 | 18.9 | 12 |
| products:category_counts | 19.97 | 18.66 | 22.65 | 12 |
| products:merchant_category_avg_price | 20.3 | 13.7 | 25.06 | 32 |
| products:top_100_by_price | 9.72 | 9.47 | 13.14 | 100 |
| products:title_search_like | 9.24 | 9.06 | 12.71 | 1 |
| products:sku_lookup_unique_index | 0.03 | 0.03 | 0.16 | 1 |
| products:window_top10_per_category | 38.91 | 33.75 | 145.19 | 1 |
| products:join_tier_summary | 14.27 | 14.03 | 19.66 | 3 |
| products:missing_image_count | 24.53 | 21.69 | 28.86 | 1 |
| products:exact_title_duplicates | 33.85 | 32.63 | 38.69 | 1 |
| kpi_summary (single run) | 21.27 | | | |
| reconcile_sql_vs_pandas (single run) | 102.09 | | | |

Index plan for the SKU lookup: `['SEARCH products USING INDEX sqlite_autoindex_products_1 (merchant_id=? AND sku=?)']`

## Cross-checks

`{"missing_images": {"sql": 799, "audit": 799, "match": true}, "invalid_prices": {"sql": 3, "audit": 3, "match": true}, "reconcile_all_match": true}`
