# Benchmark `baseline` - 25,000 products / 10,000 requests

**MEASURED on synthetic data (not simulated timings).** Measured 2026-11-26 16:00:14 UTC at commit `295e928` on Windows-10-10.0.19045-SP0 (8 logical CPUs, 16.0 GB RAM), Python 3.10.4, pandas 2.3.3, SQLite 3.37.2. Database file 21.0 MB. Memory = process RSS sampled every 20 ms.

## Steps (wall time and memory)

| Step | Seconds | Peak RSS MB | Peak growth MB |
| --- | ---: | ---: | ---: |
| audit_load_products | 0.303 | 120.4 | 37.8 |
| audit_find_duplicate_pairs | 24.658 | 124.5 | 7.2 |
| audit_detect_duplicates | 1.998 | 121.0 | 2.8 |
| audit_detect_price_anomalies | 2.056 | 125.5 | 6.7 |
| audit_detect_missing_images | 0.451 | 119.5 | 0.2 |
| audit_detect_incomplete_metadata | 0.073 | 119.5 | 0.0 |
| audit_run_audit_total | 29.468 | 152.4 | 32.9 |
| sql_phase_total | 5.365 | 155.0 | 7.1 |
| upload_build_xlsx_15k | 5.096 | 162.4 | 2.0 |
| upload_validate_csv_15k | 0.589 | 191.3 | 28.9 |
| upload_validate_xlsx_15k | 5.298 | 190.3 | 18.4 |
| upload_import_15k_rows | 0.597 | 188.8 | 0.6 |
| export_csv_full_catalog | 0.507 | 178.8 | 8.7 |
| export_xlsx_full_catalog | 12.318 | 185.3 | 6.5 |

## Detector accuracy vs injected ground truth

| Detector | Injected | Flagged | Recall | Precision |
| --- | ---: | ---: | ---: | ---: |
| duplicate | 870 | 1047 | 1.0 | 0.831 |
| price_anomaly | 997 | 1035 | 1.0 | 0.963 |
| missing_image | 1988 | 2000 | 0.966 | 0.96 |

## SQL (5 runs each)

| Query | Median ms | Min ms | Max ms | Rows |
| --- | ---: | ---: | ---: | ---: |
| analytics:processing_time_by_type | 21.1 | 17.05 | 22.64 | 8 |
| analytics:rework_by_type | 12.36 | 11.61 | 13.79 | 8 |
| analytics:sla_by_priority | 18.02 | 13.21 | 19.89 | 4 |
| analytics:weekly_volume | 40.41 | 37.84 | 46.09 | 18 |
| analytics:status_funnel | 0.89 | 0.84 | 1.44 | 8 |
| analytics:stage_durations | 297.65 | 281.93 | 391.33 | 6 |
| analytics:backlog_aging | 3.61 | 3.33 | 19.77 | 680 |
| analytics:merchant_leaderboard | 59.76 | 56.75 | 64.9 | 12 |
| analytics:assignee_workload | 0.55 | 0.54 | 0.89 | 9 |
| analytics:catalog_by_merchant | 44.27 | 37.57 | 46.6 | 12 |
| products:category_counts | 71.39 | 64.41 | 77.87 | 12 |
| products:merchant_category_avg_price | 46.42 | 46.06 | 49.76 | 32 |
| products:top_100_by_price | 27.65 | 23.97 | 32.86 | 100 |
| products:title_search_like | 26.57 | 23.62 | 27.71 | 1 |
| products:sku_lookup_unique_index | 0.03 | 0.03 | 0.19 | 1 |
| products:window_top10_per_category | 104.85 | 100.42 | 109.53 | 1 |
| products:join_tier_summary | 40.66 | 37.21 | 42.28 | 3 |
| products:missing_image_count | 59.71 | 56.3 | 67.98 | 1 |
| products:exact_title_duplicates | 111.96 | 110.15 | 122.65 | 1 |
| kpi_summary (single run) | 77.35 | | | |
| reconcile_sql_vs_pandas (single run) | 246.64 | | | |

Index plan for the SKU lookup: `['SEARCH products USING INDEX sqlite_autoindex_products_1 (merchant_id=? AND sku=?)']`

## Cross-checks

`{"missing_images": {"sql": 2000, "audit": 2000, "match": true}, "invalid_prices": {"sql": 3, "audit": 3, "match": true}, "reconcile_all_match": true}`
