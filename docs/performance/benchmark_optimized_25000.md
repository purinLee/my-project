# Benchmark `optimized` - 25,000 products / 10,000 requests

**MEASURED on synthetic data (not simulated timings).** Measured 2026-11-26 15:47:59 UTC at commit `295e928` on Windows-10-10.0.19045-SP0 (8 logical CPUs, 16.0 GB RAM), Python 3.10.4, pandas 2.3.3, SQLite 3.37.2. Database file 21.0 MB. Memory = process RSS sampled every 20 ms.

## Steps (wall time and memory)

| Step | Seconds | Peak RSS MB | Peak growth MB |
| --- | ---: | ---: | ---: |
| audit_load_products | 0.292 | 119.5 | 37.0 |
| audit_find_duplicate_pairs | 0.558 | 124.7 | 8.2 |
| audit_detect_duplicates | 0.014 | 120.9 | 0.1 |
| audit_detect_price_anomalies | 0.108 | 128.1 | 7.3 |
| audit_detect_missing_images | 0.443 | 121.3 | 0.1 |
| audit_detect_incomplete_metadata | 0.068 | 121.4 | 0.1 |
| audit_run_audit_total | 1.588 | 153.8 | 32.5 |
| sql_phase_total | 5.159 | 156.3 | 7.0 |
| upload_build_xlsx_15k | 5.274 | 162.2 | 1.6 |
| upload_validate_csv_15k | 0.603 | 190.9 | 28.7 |
| upload_validate_xlsx_15k | 5.401 | 187.7 | 17.1 |
| upload_import_15k_rows | 0.615 | 187.2 | 1.8 |
| export_csv_full_catalog | 0.527 | 174.7 | 8.4 |
| export_xlsx_full_catalog | 11.997 | 179.8 | 5.1 |

## Detector accuracy vs injected ground truth

| Detector | Injected | Flagged | Recall | Precision |
| --- | ---: | ---: | ---: | ---: |
| duplicate | 870 | 870 | 1.0 | 1.0 |
| price_anomaly | 997 | 1035 | 1.0 | 0.963 |
| missing_image | 1988 | 2000 | 0.966 | 0.96 |

## SQL (5 runs each)

| Query | Median ms | Min ms | Max ms | Rows |
| --- | ---: | ---: | ---: | ---: |
| analytics:processing_time_by_type | 17.22 | 16.6 | 21.02 | 8 |
| analytics:rework_by_type | 11.98 | 11.59 | 18.85 | 8 |
| analytics:sla_by_priority | 13.59 | 13.29 | 16.9 | 4 |
| analytics:weekly_volume | 40.86 | 39.22 | 57.41 | 18 |
| analytics:status_funnel | 0.87 | 0.85 | 1.45 | 8 |
| analytics:stage_durations | 278.04 | 267.6 | 286.94 | 6 |
| analytics:backlog_aging | 3.41 | 3.28 | 5.02 | 680 |
| analytics:merchant_leaderboard | 62.25 | 55.06 | 65.53 | 12 |
| analytics:assignee_workload | 0.56 | 0.55 | 0.9 | 9 |
| analytics:catalog_by_merchant | 37.81 | 34.21 | 54.54 | 12 |
| products:category_counts | 71.8 | 64.77 | 93.71 | 12 |
| products:merchant_category_avg_price | 47.03 | 46.42 | 49.38 | 32 |
| products:top_100_by_price | 26.85 | 23.75 | 31.09 | 100 |
| products:title_search_like | 26.78 | 23.56 | 29.93 | 1 |
| products:sku_lookup_unique_index | 0.03 | 0.03 | 0.2 | 1 |
| products:window_top10_per_category | 101.44 | 98.56 | 105.15 | 1 |
| products:join_tier_summary | 38.85 | 36.67 | 50.98 | 3 |
| products:missing_image_count | 58.41 | 54.83 | 59.6 | 1 |
| products:exact_title_duplicates | 111.88 | 105.78 | 120.98 | 1 |
| kpi_summary (single run) | 82.21 | | | |
| reconcile_sql_vs_pandas (single run) | 248.71 | | | |

Index plan for the SKU lookup: `['SEARCH products USING INDEX sqlite_autoindex_products_1 (merchant_id=? AND sku=?)']`

## Cross-checks

`{"missing_images": {"sql": 2000, "audit": 2000, "match": true}, "invalid_prices": {"sql": 3, "audit": 3, "match": true}, "reconcile_all_match": true}`
