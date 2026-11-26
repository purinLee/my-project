# Benchmark `baseline` - 10,000 products / 5,000 requests

**MEASURED on synthetic data (not simulated timings).** Measured 2026-11-26 15:35:20 UTC at commit `295e928` on Windows-10-10.0.19045-SP0 (8 logical CPUs, 16.0 GB RAM), Python 3.10.4, pandas 2.3.3, SQLite 3.37.2. Database file 9.5 MB. Memory = process RSS sampled every 20 ms.

## Steps (wall time and memory)

| Step | Seconds | Peak RSS MB | Peak growth MB |
| --- | ---: | ---: | ---: |
| generate_database | 3.922 | 109.7 | 29.1 |
| audit_load_products | 0.102 | 103.5 | 3.8 |
| audit_find_duplicate_pairs | 4.063 | 107.3 | 3.9 |
| audit_detect_duplicates | 1.2 | 106.8 | 1.3 |
| audit_detect_price_anomalies | 1.731 | 107.7 | 0.9 |
| audit_detect_missing_images | 0.299 | 106.0 | 0.1 |
| audit_detect_incomplete_metadata | 0.053 | 106.0 | 0.0 |
| audit_run_audit_total | 7.474 | 117.0 | 11.1 |
| sql_phase_total | 1.906 | 120.7 | 4.0 |
| upload_build_xlsx_20k | 4.054 | 124.0 | 2.1 |
| upload_validate_csv_20k | 0.373 | 144.0 | 20.0 |
| upload_validate_xlsx_20k | 3.333 | 141.6 | 12.2 |
| upload_import_20k_rows | 0.351 | 141.7 | 2.3 |
| export_csv_full_catalog | 0.208 | 134.0 | 3.3 |
| export_xlsx_full_catalog | 6.021 | 137.3 | 3.3 |

## Detector accuracy vs injected ground truth

| Detector | Injected | Flagged | Recall | Precision |
| --- | ---: | ---: | ---: | ---: |
| duplicate | 349 | 370 | 1.0 | 0.943 |
| price_anomaly | 402 | 417 | 1.0 | 0.964 |
| missing_image | 799 | 799 | 0.965 | 0.965 |

## SQL (5 runs each)

| Query | Median ms | Min ms | Max ms | Rows |
| --- | ---: | ---: | ---: | ---: |
| analytics:processing_time_by_type | 4.84 | 4.78 | 10.82 | 8 |
| analytics:rework_by_type | 2.44 | 2.32 | 5.61 | 8 |
| analytics:sla_by_priority | 3.21 | 3.05 | 3.46 | 4 |
| analytics:weekly_volume | 15.04 | 14.0 | 17.56 | 18 |
| analytics:status_funnel | 0.47 | 0.46 | 0.89 | 8 |
| analytics:stage_durations | 130.14 | 121.91 | 134.7 | 6 |
| analytics:backlog_aging | 1.61 | 1.49 | 2.63 | 314 |
| analytics:merchant_leaderboard | 3.13 | 3.06 | 10.1 | 12 |
| analytics:assignee_workload | 0.28 | 0.27 | 0.41 | 9 |
| analytics:catalog_by_merchant | 13.73 | 12.85 | 16.8 | 12 |
| products:category_counts | 22.8 | 19.22 | 30.42 | 12 |
| products:merchant_category_avg_price | 16.7 | 15.57 | 22.55 | 32 |
| products:top_100_by_price | 9.67 | 9.59 | 12.4 | 100 |
| products:title_search_like | 9.56 | 9.37 | 12.21 | 1 |
| products:sku_lookup_unique_index | 0.06 | 0.06 | 0.77 | 1 |
| products:window_top10_per_category | 37.14 | 33.6 | 41.76 | 1 |
| products:join_tier_summary | 14.52 | 13.07 | 22.57 | 3 |
| products:missing_image_count | 22.47 | 21.41 | 27.83 | 1 |
| products:exact_title_duplicates | 33.61 | 32.91 | 34.79 | 1 |
| kpi_summary (single run) | 32.15 | | | |
| reconcile_sql_vs_pandas (single run) | 110.67 | | | |

Index plan for the SKU lookup: `['SEARCH products USING INDEX sqlite_autoindex_products_1 (merchant_id=? AND sku=?)']`

## Cross-checks

`{"missing_images": {"sql": 799, "audit": 799, "match": true}, "invalid_prices": {"sql": 3, "audit": 3, "match": true}, "reconcile_all_match": true}`
