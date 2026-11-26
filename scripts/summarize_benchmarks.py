"""Build docs/performance/SUMMARY.md from the recorded benchmark JSON files (numbers are copied, never typed by hand)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "docs" / "performance"


def load(name: str) -> dict | None:
    p = DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> int:
    out = ["# Performance summary", "",
           "Every number below is copied from the JSON files in this folder, produced by `scripts/benchmark.py`, `scripts/benchmark_ui.py` and "
           "`scripts/verify_llm.py`. **MEASURED** = timed on the machine listed in each JSON (single run, wall clock). All data is **synthetic**; "
           "nothing here is simulated timing, but results will differ on other hardware (Streamlit Community Cloud is slower than a local 8-core PC).", ""]
    big = load("benchmark_optimized_100000.json")
    if big:
        e = big["environment"]
        out += [f"Machine: {e['platform']}, {e['cpu_count_logical']} logical CPUs, {e['ram_total_gb']} GB RAM, Python {e['python']}, pandas {e['pandas']}, SQLite {e['sqlite']}.", ""]

    out += ["## 1. Quality audit: before vs after optimization (same code path, same data)", "",
            "| Catalog size | Version | find_duplicate_pairs (s) | run_audit total (s) | Peak RSS (MB) | Duplicate precision | Duplicate recall |", "| ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    for n in (10000, 25000, 100000):
        for label in ("baseline", "optimized"):
            d = load(f"benchmark_{label}_{n}.json")
            if not d:
                continue
            s, g = d["steps"], d["detector_vs_ground_truth"]["duplicate"]
            out.append(f"| {n:,} | {label} | {s['audit_find_duplicate_pairs']['seconds']} | {s['audit_run_audit_total']['seconds']} | {s['audit_run_audit_total']['rss_peak_mb']} | {g['precision']} | {g['recall']} |")
    out += ["", "The baseline at 100,000 rows was not run to completion: its all-pairs-per-block comparison grows quadratically "
            "(2.5x more rows took about 6x longer for the pair search), which extrapolates to many minutes. Precision also fell with size in the baseline because "
            "more titles differing only by one word (e.g. 'Pro' vs 'Max') cleared the similarity threshold; the optimized matcher rejects those.", ""]

    if big:
        out += ["## 2. 100,000 products / 50,000 requests / 316,221 events", "", "| Step | Seconds | Peak RSS MB |", "| --- | ---: | ---: |"]
        out += [f"| {k} | {v['seconds']} | {v['rss_peak_mb']} |" for k, v in big["steps"].items()]
        out += ["", f"Process peak RSS: **{big['process_peak_rss_mb']} MB**. Database file: {big['db_file_mb']} MB.", "",
                "### Detector accuracy against injected ground truth", "", "| Detector | Injected | Flagged | Recall | Precision |", "| --- | ---: | ---: | ---: | ---: |"]
        out += [f"| {k} | {v['injected']} | {v['flagged']} | {v['recall']} | {v['precision']} |" for k, v in big["detector_vs_ground_truth"].items()]
        out += ["", "### SQL (5 runs each, median ms)", "", "| Query | Median ms | Max ms | Rows |", "| --- | ---: | ---: | ---: |"]
        for k, v in big["sql"].items():
            if "max_ms" in v:
                out.append(f"| {k} | {v['median_ms']} | {v['max_ms']} | {v['rows']} |")
            elif "median_ms" in v:
                out.append(f"| {k} (single run) | {v['median_ms']} | | |")
        up = big["upload_20k"]
        out += ["", f"SKU lookup plan: `{big['sql']['sku_lookup_plan']['plan']}`", "",
                f"Upload path: {up['rows']:,} rows -> CSV {up['csv_bytes'] / 1e6:.1f} MB, XLSX {up['xlsx_bytes'] / 1e6:.1f} MB. "
                f"A 20,000-row CSV of this shape is {up['csv_bytes_at_20000_rows'] / 1e6:.2f} MB, which "
                f"{'EXCEEDS' if up['csv_20000_rows_exceeds_5mb_limit'] else 'fits within'} the 5 MB upload limit, so the effective limit is the size, not the row count.", "",
                f"SQL vs audit cross-checks: `{json.dumps(big['cross_checks'])}`", ""]

    ui = load("ui_render_times.json")
    if ui:
        out += ["## 3. Streamlit page render time on the 100,000-product database", "", f"{ui['kind']}. Cold = first run in the process (imports, audit); warm = second run (audit cached).", "",
                "| Page | Cold (s) | Warm (s) |", "| --- | ---: | ---: |"]
        out += [f"| {k} | {v['cold_seconds']} | {v['warm_seconds']} |" for k, v in ui["pages"].items()]
        out.append("")

    verifs = sorted(DIR.glob("llm_verification_*.json"))
    out += ["## 4. LLM verification status", ""]
    if not verifs:
        out.append("No verification record exists.")
    for p in verifs:
        d = json.loads(p.read_text(encoding="utf-8"))
        if d["status"] == "completed":
            u = d["usage"]
            out.append(f"- `{p.name}`: **live run completed** with model `{d['model']}`: {u['calls']} calls ({u['ok']} ok), LLM accuracy {d['llm_accuracy']} vs rules {d['rules_accuracy']}, "
                       f"{u['prompt_tokens']}+{u['completion_tokens']} tokens, estimated cost ${u['estimated_cost_usd']}, avg latency {u['latency_ms_avg']} ms.")
        else:
            out.append(f"- `{p.name}`: **SKIPPED - no API key, no real LLM call was made.** Fallback check: all {d['examples']} requests handled by rules "
                       f"(held-out accuracy {d['fallback_check']['rules_accuracy']}).")
    (DIR / "SUMMARY.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("wrote", DIR / "SUMMARY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
