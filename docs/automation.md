# Automation: Python pipeline, UI RPA and how the impact was measured

> **All data used here is synthetic** (generated, fictional merchants and products). Results describe this code on that data; they do not describe real catalogs, real analysts or any real company.

## 1. Two kinds of automation, and why

| | **Python automation** (`catalog_intel/automation.py`) | **UI RPA** (`scripts/rpa_ui_bot.py`) |
| --- | --- | --- |
| How it works | Calls the application's own functions directly (no browser) | Drives the real web UI with Playwright: clicks, uploads, downloads |
| Best for | Anything whose logic is reachable from code: validation, import, duplicate/price/image checks, reports, exports | Steps that exist only on a screen (no API/library access), or proving the UI works end to end |
| Speed | Seconds | Minutes (each action waits for the UI) |
| Robustness | High: transactional, idempotent, unit-tested | Lower: depends on page structure; mitigated with waits and retries |
| Used here for | **The production automation** (CLI, dashboard button, scheduler) | UI regression/end-to-end automation and a measured proxy for a manual click-through |

Decision rule used in this project: **use Python wherever the logic is importable; use RPA only for work that can only be done through a screen.** Here everything is importable, so the Python pipeline is the right tool and the RPA bot is deliberately the secondary path. Commercial RPA suites (UiPath, Power Automate) were not used: they add licensing and infrastructure with no benefit when the system is open and scriptable.

## 2. What the Python pipeline does

`run_pipeline` / `run_inbox` (CLI: `scripts/run_automation.py`, dashboard: *Automation Performance -> Run automation*):

1. **Collect** - read CSV/XLSX files from an inbox folder (ignoring `~$` lock files, `.part` files and files still being written), compute SHA-256, route each file to a merchant by file name (`M-001_*.csv`) or a default merchant, skip files already imported. The skip decision is by hash of the file bytes: an unchanged CSV is skipped, but an XLSX re-exported with identical rows has different bytes (embedded timestamp) and is imported again as an upsert (no duplicate rows).
2. **Validate** - format and required fields (SKU, title, price, currency, status, limits); bad rows are rejected with row numbers; unreadable files fail individually.
3. **Import** - one transaction per file together with its hash record (no partial imports; a crash cannot cause a double import); transient SQLite lock errors are retried with back-off.
4. **Quality checks** - duplicate detection, price anomaly detection, missing/invalid/placeholder image detection, metadata completeness, over the target catalog, with per-detector timings.
5. **Report** - HTML and multi-sheet Excel executive reports.
6. **Export** - this run's findings, duplicates, price anomalies, missing images and rejected rows as CSV (and findings as Excel), written atomically (`.part` then rename) to the outbox.
7. **History** - one row in `automation_runs` (counts, per-step seconds, status, error, per-file outcome without row contents).

Imported files move to `processed/`, failed files to `failed/` with an `.error.txt`. Re-running is always safe.

```bash
python scripts/run_automation.py --inbox data/automation/inbox                      # one run
python scripts/run_automation.py --inbox data/automation/inbox --watch --interval 60  # poll a folder
```

Scheduling without extra software: Windows Task Scheduler (`python scripts\run_automation.py --inbox ...` every hour/day) or cron (`0 * * * * python scripts/run_automation.py --inbox ...`). Exit status is `0` for success/nothing to do and `1` when any file failed, so the scheduler can alert.

**Demo protection.** The default target is an isolated *sandbox* database (`data/automation/sandbox.db`). The CLI refuses to write into the demo catalog without `--allow-main-db`; the dashboard defaults to the sandbox and labels the other option as modifying demo data. Run history lives in a table (`automation_runs`) that survives the periodic demo reseed.

## 3. The UI RPA bot

`scripts/rpa_ui_bot.py --url http://localhost:8501 --inbox <dir> --out-dir <dir> [--db app.db]`

Per file: Catalog page -> *Upload CSV / Excel* tab -> choose merchant -> upload -> read the validation counts -> *Import*. Then once: Audit page (wait for the quality audit, read the KPI cards) -> export findings (CSV and Excel) -> Overview page -> download the Excel executive report. Every step is timed and retried (page reload + backoff); a file that cannot be processed is recorded as failed and the bot continues. With `--db` (only meaningful next to the app's database) the run is recorded in `automation_runs` with `mode = rpa_ui`.

The bot imports into whichever database the target app uses, so measurement and tests start a throw-away server with a temporary database.

## 4. How the impact was measured

`scripts/measure_automation.py` builds one **seeded synthetic workload** (default: 10 merchant files, half XLSX, 4,990 rows, with known injected defects: 140 duplicate listings, 190 price anomalies, 380 missing images, 140 invalid rows) and processes the identical files three ways:

| Mode | Basis | How |
| --- | --- | --- |
| Python automation | **MEASURED** | 5 runs, each on a fresh sandbox DB, files read from a real inbox folder; median per step; peak RSS sampled |
| UI RPA | **MEASURED** | 2 runs, each with a fresh throw-away Streamlit server and a headless Chromium; median per step |
| Manual (Excel-assisted analyst) | **ESTIMATED** | unit-time assumptions x workload size - nobody was timed |

Task mapping (identical for all modes): *collect & validate*, *import*, *quality checks*, *report*, *export*. For RPA, "collect & validate" includes page navigation, merchant selection, upload and the validation screen.

**Manual-time assumptions** (seconds; deliberately conservative; editable in `catalog_intel/roi.py` and on the dashboard):

| Assumption | Value |
| --- | ---: |
| Open/check/copy one file | 60 per file |
| Validate fields visually | 0.5 per row |
| Import one file | 20 per file (+ 0.05 per row) |
| Duplicate check | 0.6 per row |
| Price outlier check | 0.4 per row |
| Image check | 0.3 per row |
| Build the report | 600 per run |
| Export deliverables | 120 per run |
| **Human review after automation** (added to the automated side) | 300 per run |

The review line matters: automation does not remove the person, it removes the searching. Counting only machine seconds would show 99.98% saved / 4,000x, which is arithmetically true and practically meaningless, so **the headline compares manual time with automated machine time plus 300 s of estimated review**.

**Errors and omissions.** Both the automation and an *Excel-style baseline procedure* (`catalog_intel/baseline.py`: blank-cell count, "Remove Duplicates" on trimmed case-insensitive titles, a 1.5xIQR fence over the whole price column, blank-image filter) are scored against the injected ground truth. The baseline is a **simulated procedure, not a measurement of people**; it quantifies what simple spreadsheet checks miss that the pipeline finds. Duplicate pairs count as found if either member is flagged.

**Not measured (and therefore not claimed):** real analyst time, real-world defect rates, the cost of fixing the findings (identical before and after, so excluded), cloud-hosted performance, multi-user load.

Results are in [`docs/automation/measurements.md`](automation/measurements.md) and `measurements.json` (the dashboard reads the JSON). `python scripts/measure_automation.py --recompute` rebuilds only the ESTIMATED block after changing assumptions, leaving the MEASURED values untouched. Tests recompute every derived number in the committed JSON from its raw values.

## 5. Verification coverage

- `tests/test_automation.py` - pipeline outputs and history, ground-truth recall/precision, **idempotent repeated runs**, changed-content re-import, per-file failure isolation, **simulated crash mid-import and mid-quality**, lock-error retry, inbox moves and name collisions, crash-after-commit recovery, **database integrity** (`integrity_check`, foreign keys, uniqueness, counts reconcile), history surviving reseed, CLI demo-DB refusal.
- `tests/test_roi.py` - hand-calculated KPI formulas, division-by-zero behaviour, and recomputation of every number in `measurements.json`; history KPIs checked in SQL vs pandas.
- `tests/test_privacy.py` - secret-pattern scan of tracked files, ignored sensitive paths, no emails/phones in synthetic data, no keys or row contents in history/reports, automation makes no network calls.
- `tests/test_rpa.py` - the real bot against a real browser and server: success path, corrupt-file recovery, rerun without duplicates.
- UI smoke tests render the dashboard and run the pipeline from it.
