# Automation measurements

> ALL data is synthetic (generated, fictional merchants/products). Results do not describe real catalogs or real people.

Workload: 10 files (5 xlsx), 4,990 rows, injected defects {'duplicate': 140, 'price_anomaly': 190, 'missing_image': 380, 'invalid_rows': 140}.
Machine: Windows-10-10.0.19045-SP0, 8 logical CPUs, 16.0 GB, Python 3.10.4; measured 2026-11-26 17:02:08 UTC.

Legend: **MEASURED** = timed by running the code; **ESTIMATED** = assumption-based (manual work was never timed).

## Time per task (seconds)

| Task | Manual (ESTIMATED) | Python automation (MEASURED, median; review row ESTIMATED) | UI RPA (MEASURED, median; review row ESTIMATED) | Time saved by Python | Speed-up |
| --- | ---: | ---: | ---: | ---: | ---: |
| Collect & validate files | 3095.0 | 1.196 | 52.639 | 99.96% | 2588.23x |
| Import / consolidate | 449.5 | 0.369 | 13.149 | 99.92% | 1216.84x |
| Quality checks (duplicates, price, images) | 6487.0 | 0.314 | 2.571 | 100.0% | 20692.19x |
| Report creation | 600.0 | 0.344 | 3.416 | 99.94% | 1745.2x |
| Excel/CSV export | 120.0 | 0.441 | 1.879 | 99.63% | 272.42x |
| Total (machine time) | 10751.5 | 2.663 | 73.653 | 99.98% | 4037.36x |
| Human review of automated report (ESTIMATED, remains after automation) | 0.0 | 300.0 | 300.0 |  |  |
| Total incl. human review | 10751.5 | 302.663 | 373.653 | 97.18% | 35.52x |

Machine-only pipeline throughput: 6,570,592.5 rows/hour (MEASURED). End-to-end including the estimated 300 s human review: 59,353.1 rows/hour vs manual 1,670.8 rows/hour (both ESTIMATED).
Estimated end-to-end time saved: 97.18% (35.52x, productivity gain 3452.0%). Machine-time-only comparison would be 99.98% / 4037.36x and is NOT the headline because it ignores the human review.
Estimated monthly time saved: 11.61 h at 4 runs/month, 58.05 h at 20 runs/month.

## Detection accuracy vs injected ground truth (MEASURED; the baseline is a simulated Excel-style procedure, not people)

| Defect | Injected | Automation recall / precision | Excel-style baseline recall / precision | Missed (auto vs baseline) |
| --- | ---: | ---: | ---: | ---: |
| duplicate | 140 | 1.0 / 1.0 | 0.7571 / 0.8346 | 0 vs 34 |
| price_anomaly | 190 | 1.0 / 0.9948 | 0.5316 / 0.1913 | 0 vs 89 |
| missing_image | 380 | 1.0 / 1.0 | 0.2368 / 1.0 | 0 vs 290 |
| invalid_rows | 140 |  /  |  /  | 0 vs 109 |

Total omissions: automation 0 vs baseline 522 -> omission reduction 100.0%.

## Scale test (MEASURED, Python automation)

99,992 rows in 8 files (largest 2.97 MB): 33.694 s total, 2967.7 rows/s, peak RSS 440.9 MB. Manual ESTIMATE for the same work: 186,345.2 s.

