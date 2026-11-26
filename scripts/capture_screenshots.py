"""Capture screenshots of the running app into docs/screenshots (requires `playwright install chromium`).

Usage: python scripts/capture_screenshots.py [--url http://localhost:8501]
Run against a freshly seeded database so the screenshots show unmodified synthetic data.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import pandas as pd
from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from catalog_intel.excel import to_xlsx_bytes  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
PAGES = [("", "01_executive_overview"), ("catalog", "02_catalog_management"), ("audit", "03_quality_audit"),
         ("requests", "04_merchant_requests"), ("sql", "05_sql_analytics"), ("assistant", "06_ai_assistant")]
UPLOAD_CSV = (
    "sku,title,brand,category,price,image_url,description\n"
    "UP-1,Uploaded Demo Blender 900W,Whirlwind,Blenders,59.90,https://images.example-demo.com/u/1.jpg,A demo blender description\n"
    "UP-2,X,,,abc,,\n,Row without a sku,,,5,,\n"
)


def open_page(page: Page, base: str, path: str) -> None:
    page.goto(f"{base}/{path}", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid='stMainBlockContainer']", timeout=60000)
    page.wait_for_timeout(4500)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8501")
    base = ap.parse_args().url.rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        for path, name in PAGES:
            open_page(page, base, path)
            page.screenshot(path=str(OUT / f"{name}.png"))
            print("saved", name)

        # request detail: select the first row in the queue
        open_page(page, base, "requests")
        page.mouse.click(397, 530)
        page.wait_for_timeout(3000)
        page.get_by_text("Update status").first.scroll_into_view_if_needed()
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "04b_request_detail.png"))
        print("saved 04b_request_detail")

        # live classification preview
        open_page(page, base, "requests")
        page.get_by_role("tab", name="New request").click()
        page.wait_for_timeout(1200)
        page.get_by_placeholder("e.g. CSV upload failing with validation errors").fill("CSV upload failing with validation errors")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        page.get_by_placeholder("Describe the issue, affected products and any deadline.").fill(
            "Our bulk CSV upload keeps failing and 300 rows were rejected. Please help asap, campaign launches today.")
        page.keyboard.press("Control+Enter")
        page.wait_for_timeout(3500)
        page.screenshot(path=str(OUT / "04c_new_request_analysis.png"))
        print("saved 04c_new_request_analysis")

        # CSV upload validation (not imported, so the demo data stays untouched)
        csv_path = Path(tempfile.gettempdir()) / "upload_demo.xlsx"
        csv_path.write_bytes(to_xlsx_bytes({"Catalog": pd.read_csv(__import__("io").StringIO(UPLOAD_CSV), dtype=str, keep_default_na=False)}))
        open_page(page, base, "catalog")
        page.get_by_role("tab", name="Upload CSV / Excel").click()
        page.wait_for_timeout(1000)
        page.set_input_files("input[type=file]", str(csv_path))
        page.wait_for_timeout(3500)
        page.screenshot(path=str(OUT / "02b_csv_upload_validation.png"))
        print("saved 02b_csv_upload_validation")

        # SQL verification tab
        open_page(page, base, "sql")
        page.get_by_role("tab", name="Verification").click()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "05b_sql_verification.png"))
        print("saved 05b_sql_verification")

        # assistant conversation
        open_page(page, base, "assistant")
        page.get_by_role("button", name="Which requests are breaching SLA?").click()
        page.wait_for_timeout(3500)
        page.get_by_role("button", name="Give me an executive summary").click()
        page.wait_for_timeout(3500)
        page.screenshot(path=str(OUT / "06b_assistant_answers.png"))
        print("saved 06b_assistant_answers")
        page.get_by_text("LLM usage, latency and estimated cost log").click()
        page.wait_for_timeout(1500)
        page.get_by_text("LLM usage, latency and estimated cost log").scroll_into_view_if_needed()
        page.screenshot(path=str(OUT / "06c_llm_usage_log.png"))
        print("saved 06c_llm_usage_log")
        browser.close()


if __name__ == "__main__":
    main()
