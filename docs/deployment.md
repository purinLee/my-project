# Deployment notes (Streamlit Community Cloud)

> The demo contains **synthetic data only**. No secrets are required to run it.

## Resource limits (from Streamlit's documentation, as of February 2024, "may change at any time without notice")

| Resource | Limit |
| --- | --- |
| CPU | 0.078 cores minimum, 2 cores maximum |
| Memory | 690 MB minimum, 2.7 GB maximum |
| Storage | up to 50 GB, **ephemeral** |
| Idle | apps without traffic for 12 hours go to sleep and wake on the next visit |

Source: https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app

**What this app needs (measured locally, not on Cloud):** the demo app (about 1,900 products) is light; the 100,000-product audit peaks at about 417 MB of process memory and the 100,000-row automation scale test at about 441 MB (see `docs/performance/SUMMARY.md` and `docs/automation/measurements.md`). That is under the 690 MB guaranteed minimum but not by a wide margin, and Cloud CPU is slower than the 8-core PC used for the timings. Large (100k-row) runs are therefore a local/CLI workload; the hosted demo is meant for the seeded demo data and small synthetic automation runs (default 4 files x 200 rows, about 0.6 s locally).

## Data persistence

SQLite files live on the app's container filesystem, which **does not survive reboots, redeploys or being put to sleep and rebuilt**. This has *not* been verified on a deployed app yet (it can only be verified after deployment by rebooting the app); the documented behaviour above is what to expect. The app is designed to tolerate it:

- the demo database is recreated and re-seeded automatically on the first request (and refreshed when older than 3 days);
- the automation sandbox is a separate file and can be reset from the dashboard;
- run history, uploaded data and the LLM call log are lost on a rebuild - acceptable for a demo, **not** for real use.

If real persistence is needed, options in increasing effort:

| Option | Notes |
| --- | --- |
| Keep SQLite, accept resets | Current. Zero cost; demo-grade. |
| Hosted libSQL/Turso | SQLite-compatible; smallest code change (swap the connection layer). |
| Managed PostgreSQL (Supabase, Neon, ...) | Most robust; needs SQL dialect changes (`julianday`, `strftime`, `INSERT OR`/`ON CONFLICT`) and a connection secret. |
| Snapshot to object storage | Periodically upload `catalog.db`; restore at startup. Simple but single-writer. |

Never commit a real database; `*.db`, `.env*` and `.streamlit/secrets.toml` are git-ignored and a test enforces it.

## Deploy checklist

1. Review `git status` / `git diff`, run `python -m pytest -q`, confirm nothing sensitive is tracked (`tests/test_privacy.py`).
2. Push to GitHub.
3. share.streamlit.io -> *Create app* -> repository, branch `main`, **Main file path `app.py`**, Python 3.10-3.12.
4. Dependencies come from `requirements.txt` (`streamlit>=1.52`, pandas, plotly, openpyxl). Playwright and pytest are in `requirements-dev.txt` and are **not** installed on Cloud.
5. Secrets: none required. Optional LLM keys go in *App settings -> Secrets* (never in the repository).
6. After deploy: open the URL, check every page, run an automation in the sandbox, download an Excel file, upload an XLSX; check the Cloud logs for errors and memory warnings.
