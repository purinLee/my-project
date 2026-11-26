"""Real-browser check of XLSX upload and download against a running app (Playwright).

Usage: python scripts/e2e_excel_check.py --url http://localhost:8501
Run against a throwaway database (CATALOG_DB_PATH) because the check imports one product.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from catalog_intel.excel import to_xlsx_bytes  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8501")
    base = ap.parse_args().url.rstrip("/")
    tmp = Path(tempfile.mkdtemp())
    upload = tmp / "merchant_upload.xlsx"
    upload.write_bytes(to_xlsx_bytes({"Catalog": pd.DataFrame([
        {"sku": "XLSX-E2E-1", "title": "Excel E2E Uploaded Blender 900W", "price": 59.9, "brand": "Whirlwind", "category": "Blenders",
         "image_url": "https://images.example-demo.com/e2e/1.jpg", "description": "Uploaded through the browser from an xlsx file"},
        {"sku": "XLSX-E2E-2", "title": "X", "price": "abc", "brand": "", "category": "", "image_url": "", "description": ""},
    ])}))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, accept_downloads=True)
        page.goto(f"{base}/catalog")
        page.wait_for_selector("[data-testid='stMainBlockContainer']", timeout=60000)
        page.wait_for_timeout(4000)
        page.get_by_role("tab", name="Upload CSV").click()
        page.wait_for_timeout(800)
        page.set_input_files("input[type=file]", str(upload))
        page.wait_for_timeout(3500)
        body = page.inner_text("body")
        check("xlsx upload accepted and validated", "Rows read" in body and "Rejected" in body)
        page.get_by_role("button", name="Import 1 valid row").click()
        page.wait_for_timeout(3500)
        check("xlsx import flash message", "Imported: 1 inserted" in page.inner_text("body"))

        page.get_by_role("tab", name="Browse catalog").click()
        page.wait_for_timeout(800)
        page.get_by_placeholder("e.g. Auralis or 002-WIR").fill("XLSX-E2E")
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        with page.expect_download(timeout=60000) as dl:
            page.get_by_role("button", name="Export filtered (Excel)").click()
        path = tmp / "catalog_export.xlsx"
        dl.value.save_as(str(path))
        ws = load_workbook(path).active
        values = [[c.value for c in r] for r in ws.iter_rows(min_row=1)]
        skus = [r[values[0].index("sku")] for r in values[1:]]
        check("xlsx download contains imported product", skus == ["XLSX-E2E-1"], f"skus={skus} filename={dl.value.suggested_filename}")

        page.goto(f"{base}/")
        page.wait_for_selector("[data-testid='stMainBlockContainer']", timeout=60000)
        page.wait_for_timeout(5000)
        with page.expect_download(timeout=60000) as dl2:
            page.get_by_role("button", name="Executive report (Excel)").click()
        path2 = tmp / "executive_report.xlsx"
        dl2.value.save_as(str(path2))
        names = load_workbook(path2).sheetnames
        check("executive report xlsx has expected sheets", names == ["KPIs", "SLA by priority", "Processing time", "Rework by type", "Quality findings"], str(names))
        browser.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
