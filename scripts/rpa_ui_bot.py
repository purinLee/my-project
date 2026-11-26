"""UI-driven RPA bot (Playwright): performs the catalog workflow THROUGH THE BROWSER, exactly like a human operator.

    python scripts/rpa_ui_bot.py --url http://localhost:8501 --inbox path/to/inbox --out-dir path/to/outbox [--db app.db]

Per file: Catalog page -> choose merchant -> upload CSV/XLSX -> read validation result -> click Import.
Once: Audit page (wait for the quality audit, read the KPI cards) -> export findings (CSV + Excel) -> Overview page ->
download the Excel executive report. Every step is timed, retried on failure and recorded (JSON, and `automation_runs`
when --db is given and the bot runs next to the app's database).

When to use RPA instead of Python automation: only when the system offers no API/library access and the work can only be
done through its screens. Here the app's logic IS importable, so `catalog_intel.automation` is the right tool; this bot
exists to automate the UI path, to prove the UI works end to end, and to give a measured proxy of the manual
click-through workflow (minus human reading/thinking time). See docs/automation.md.

SAFETY: the bot imports files into whatever database the target app uses. Never point it at a demo you want to keep
unchanged; use a throwaway CATALOG_DB_PATH (scripts/measure_automation.py does this).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog_intel.automation import FileOutcome, RunResult, merchant_from_filename, record_run, sha256_hex  # noqa: E402

TIMEOUT_MS = 120_000


def wait_ready(page: Page) -> None:
    page.wait_for_selector("[data-testid='stMainBlockContainer']", timeout=TIMEOUT_MS)


def goto(page: Page, base: str, path: str) -> None:
    page.goto(f"{base}/{path}", wait_until="domcontentloaded")
    wait_ready(page)
    page.wait_for_timeout(600)


def kpi_value(page: Page, label: str) -> int | None:
    card = page.locator(".kpi", has_text=label).first
    if card.count() == 0:
        return None
    m = re.search(r"\n\s*([\d,]+)", card.inner_text())
    return int(m.group(1).replace(",", "")) if m else None


def metrics(page: Page) -> dict[str, int]:
    out = {}
    for t in page.locator("[data-testid='stMetric']").all_inner_texts():
        parts = [p.strip() for p in t.splitlines() if p.strip()]
        if len(parts) >= 2 and parts[1].replace(",", "").isdigit():
            out[parts[0]] = int(parts[1].replace(",", ""))
    return out


def process_file(page: Page, base: str, path: Path, default_merchant: str | None, timings: dict[str, float]) -> FileOutcome:
    data = path.read_bytes()
    o = FileOutcome(path.name, sha256_hex(data), merchant_from_filename(path.name, default_merchant))
    t_start = time.perf_counter()  # navigation + merchant selection + upload + validation all count as "collect & validate"
    goto(page, base, "catalog")
    page.get_by_role("tab", name="Upload CSV / Excel").click()
    page.wait_for_timeout(400)
    if o.merchant_id and o.merchant_id != "M-001":
        page.get_by_label("Target merchant").click()
        page.get_by_role("option", name=re.compile(f"^{o.merchant_id} ")).click()
        page.wait_for_timeout(400)
    page.set_input_files("input[type=file]", str(path))
    page.locator("text=Rows read").or_(page.locator("text=Upload rejected")).first.wait_for(timeout=TIMEOUT_MS)
    if page.get_by_text("Upload rejected").count():
        timings["collect_validate"] += time.perf_counter() - t_start
        o.status, o.error = "failed", page.get_by_text("Upload rejected").first.inner_text()[:200]
        return o
    page.locator("[data-testid='stMetric']", has_text="Rejected").first.wait_for(timeout=30_000)  # Streamlit streams widgets one by one
    m = metrics(page)
    o.rows_total, o.rows_valid, o.rows_rejected = m.get("Rows read", 0), m.get("Valid", 0), m.get("Rejected", 0)
    if o.rows_valid == 0:
        timings["collect_validate"] += time.perf_counter() - t_start
        o.status, o.error = "failed", "No valid rows to import."
        return o
    btn = page.get_by_role("button", name=re.compile(r"^Import \d+ valid row"))
    btn.first.wait_for(timeout=30_000)  # the preview (and the button) render slightly after the metrics
    timings["collect_validate"] += time.perf_counter() - t_start
    t0 = time.perf_counter()
    btn.click()
    page.wait_for_selector("text=Imported:", timeout=TIMEOUT_MS)
    timings["import"] += time.perf_counter() - t0
    msg = page.get_by_text(re.compile(r"Imported: \d+ inserted")).first.inner_text()
    ins = re.search(r"(\d+) inserted, (\d+) updated", msg)
    o.status, o.inserted, o.updated = "imported", int(ins.group(1)), int(ins.group(2))
    return o


def run_bot(base: str, inbox: Path, out_dir: Path, default_merchant: str | None, headless: bool, retries: int) -> RunResult:
    files = sorted(p for p in inbox.glob("*") if p.is_file() and p.suffix.lower() in (".csv", ".xlsx"))
    timings = {"collect_validate": 0.0, "import": 0.0, "quality": 0.0, "report": 0.0, "export": 0.0}
    outcomes: list[FileOutcome] = []
    findings = {"duplicates": 0, "price_anomalies": 0, "missing_images": 0, "incomplete_metadata": 0}
    error = None
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    t_run = time.perf_counter()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, accept_downloads=True)
        try:
            for f in files:
                for attempt in range(retries + 1):
                    try:
                        o = process_file(page, base, f, default_merchant, timings)
                        break
                    except (PWTimeout, Exception) as exc:  # noqa: BLE001 - retry the whole file on UI flakiness
                        o = FileOutcome(f.name, sha256_hex(f.read_bytes()), merchant_from_filename(f.name, default_merchant), "failed", error=f"{type(exc).__name__}: {str(exc)[:150]}")
                        page.wait_for_timeout(500 * (attempt + 1))
                outcomes.append(o)
            if any(o.status == "imported" for o in outcomes):
                t0 = time.perf_counter()
                goto(page, base, "audit")
                page.wait_for_selector("text=Findings by type and severity", timeout=TIMEOUT_MS)
                timings["quality"] = time.perf_counter() - t0
                findings.update(duplicates=kpi_value(page, "Duplicates") or 0, price_anomalies=kpi_value(page, "Price anomalies") or 0,
                                missing_images=kpi_value(page, "Missing images") or 0)
                t0 = time.perf_counter()
                page.get_by_role("tab", name="All findings").click()
                page.wait_for_timeout(500)
                for label, name in (("Export findings (CSV)", "quality_findings.csv"), ("Export findings (Excel)", "quality_findings.xlsx")):
                    with page.expect_download(timeout=TIMEOUT_MS) as dl:
                        page.get_by_role("button", name=label).click()
                    dl.value.save_as(str(out_dir / name))
                timings["export"] = time.perf_counter() - t0
                t0 = time.perf_counter()
                goto(page, base, "")
                with page.expect_download(timeout=TIMEOUT_MS) as dl:
                    page.get_by_role("button", name="Executive report (Excel)").click()
                dl.value.save_as(str(out_dir / "executive_report.xlsx"))
                timings["report"] = time.perf_counter() - t0
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            browser.close()
    totals = {
        "files_total": len(outcomes), "files_imported": sum(o.status == "imported" for o in outcomes), "files_failed": sum(o.status == "failed" for o in outcomes),
        "files_skipped": 0, "rows_total": sum(o.rows_total for o in outcomes), "rows_valid": sum(o.rows_valid for o in outcomes),
        "rows_rejected": sum(o.rows_rejected for o in outcomes), "rows_inserted": sum(o.inserted for o in outcomes), "rows_updated": sum(o.updated for o in outcomes),
    }
    status = "failed" if error or (outcomes and totals["files_imported"] == 0) else "partial" if totals["files_failed"] else "success" if outcomes else "nothing_to_do"
    findings["incomplete_metadata"] = 0
    return RunResult(None, status, "rpa_ui", started, round(time.perf_counter() - t_run, 3), {k: round(v, 3) for k, v in timings.items()}, {},
                     outcomes, totals, findings, dict(findings), error)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8501")
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--default-merchant", default=None)
    ap.add_argument("--db", default=None, help="record the run in this database's automation_runs (only when running next to the app DB)")
    ap.add_argument("--json", default=None, help="write the run summary here")
    ap.add_argument("--headed", action="store_true", help="show the browser (default: headless)")
    ap.add_argument("--retries", type=int, default=2)
    args = ap.parse_args()
    result = run_bot(args.url.rstrip("/"), Path(args.inbox), Path(args.out_dir), args.default_merchant, not args.headed, args.retries)
    if args.db:
        result.run_id = record_run(args.db, result, "rpa", "ui", "rpa bot")
    summary = result.summary()
    print(json.dumps({k: summary[k] for k in ("status", "seconds_total", "step_seconds", "totals", "error")}, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return 0 if result.status in ("success", "nothing_to_do") else 1


if __name__ == "__main__":
    raise SystemExit(main())
