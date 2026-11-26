"""Python automation CLI: process every CSV/XLSX in an inbox folder.

    python scripts/run_automation.py --inbox data/automation/inbox            # one run (e.g. from Task Scheduler / cron)
    python scripts/run_automation.py --inbox data/automation/inbox --watch --interval 60

Per run: collect -> validate -> import -> duplicate / price / image / metadata checks -> HTML + Excel report -> CSV/Excel
exports in the outbox. Imported files move to <inbox>/processed, bad files to <inbox>/failed (+ .error.txt). Re-running is
safe: files already imported (same SHA-256) are skipped. Name files `M-001_anything.csv` to route them to a merchant, or
pass --default-merchant.

Exit status: 0 = success / nothing to do, 1 = some or all files failed, 2 = bad arguments.
Default target is an isolated sandbox database; the demo catalog is only touched with --allow-main-db.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog_intel.automation import ensure_sandbox, run_inbox  # noqa: E402
from catalog_intel.config import ROOT, get_db_path  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--outbox", default=None, help="default: <inbox>/../outbox")
    ap.add_argument("--db", default=str(ROOT / "data" / "automation" / "sandbox.db"), help="target catalog database (default: isolated sandbox)")
    ap.add_argument("--allow-main-db", action="store_true", help="required to write into the demo catalog database")
    ap.add_argument("--history-db", default=None, help="where run history is recorded (default: the target database)")
    ap.add_argument("--default-merchant", default=None)
    ap.add_argument("--min-age", type=float, default=2.0, help="ignore files modified in the last N seconds (still being copied)")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()

    db = Path(args.db).resolve()
    if db == get_db_path().resolve() and not args.allow_main_db:
        print("Refusing to write into the demo catalog database without --allow-main-db.", file=sys.stderr)
        return 2
    ensure_sandbox(db)
    Path(args.inbox).mkdir(parents=True, exist_ok=True)

    def once() -> int:
        r = run_inbox(args.inbox, db, out_dir=args.outbox, min_age_seconds=args.min_age, default_merchant=args.default_merchant,
                      trigger="cli", target_label="main" if db == get_db_path().resolve() else "sandbox", history_db=args.history_db)
        print(json.dumps({"run_id": r.run_id, "status": r.status, "seconds": r.seconds_total, "steps": r.step_seconds, "totals": r.totals,
                          "findings_this_run": r.findings_run_scope, "error": r.error}, indent=2))
        return 0 if r.status in ("success", "nothing_to_do") else 1

    if not args.watch:
        return once()
    print(f"Watching {args.inbox} every {args.interval}s (Ctrl+C to stop)")
    code = 0
    try:
        while True:
            code = once() or code
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return code


if __name__ == "__main__":
    raise SystemExit(main())
