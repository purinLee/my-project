"""Capture screenshots of the running app into docs/screenshots (requires `playwright install chromium`).

Usage: python scripts/capture_screenshots.py [--url http://localhost:8501]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
PAGES = [("", "01_executive_overview"), ("catalog", "02_catalog_management"), ("audit", "03_quality_audit"),
         ("requests", "04_merchant_requests"), ("sql", "05_sql_analytics"), ("assistant", "06_ai_assistant")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8501")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        for path, name in PAGES:
            page.goto(f"{args.url.rstrip('/')}/{path}", wait_until="networkidle")
            page.wait_for_selector("[data-testid='stMainBlockContainer']", timeout=60000)
            page.wait_for_timeout(4000)
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
            print("saved", name)
        browser.close()


if __name__ == "__main__":
    main()
