"""Operational report utility for submissions CSV.

Counts prepared_* as prepared metric.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict


def _is_prepared_status(status: str) -> bool:
    value = (status or "").strip().lower()
    return value == "prepared" or value.startswith("prepared_")


def _is_skipped_status(status: str) -> bool:
    value = (status or "").strip().lower()
    return value == "skipped" or value.startswith("skipped_")


def _find_latest_submissions(results_dir: Path) -> Path | None:
    files = sorted(results_dir.glob("submissions_*.csv"))
    return files[-1] if files else None


def build_report(csv_path: Path) -> Dict[str, object]:
    counts = Counter()
    reasons = Counter()
    prepared_breakdown = Counter()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            status = str(row.get("status", "")).strip().lower()
            reason = str(row.get("message", "")).strip() or "unknown"
            counts["total"] += 1

            if status == "sent":
                counts["sent"] += 1
            elif _is_prepared_status(status):
                counts["prepared"] += 1
                prepared_breakdown[status or "prepared"] += 1
            elif status == "failed":
                counts["failed"] += 1
            elif _is_skipped_status(status):
                counts["skipped"] += 1
            else:
                counts["other"] += 1

            reasons[reason[:80]] += 1

    return {
        "csv_path": str(csv_path),
        "counts": dict(counts),
        "prepared_breakdown": dict(prepared_breakdown),
        "top_reasons": reasons.most_common(15),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Operations report from submissions CSV.")
    parser.add_argument("--results-dir", default="results", help="Results directory path")
    parser.add_argument("--csv", default="", help="Optional submissions CSV path override")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    csv_path = Path(args.csv) if args.csv else _find_latest_submissions(results_dir)
    if not csv_path or not csv_path.exists():
        print("No submissions CSV found.")
        return

    report = build_report(csv_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    counts = report["counts"]
    print(f"CSV: {report['csv_path']}")
    print(
        "Total={total} Sent={sent} Prepared={prepared} Failed={failed} Skipped={skipped} Other={other}".format(
            total=counts.get("total", 0),
            sent=counts.get("sent", 0),
            prepared=counts.get("prepared", 0),
            failed=counts.get("failed", 0),
            skipped=counts.get("skipped", 0),
            other=counts.get("other", 0),
        )
    )
    print("\nPrepared breakdown:")
    for key, value in sorted(report["prepared_breakdown"].items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {key}: {value}")
    print("\nTop reasons:")
    for reason, value in report["top_reasons"]:
        print(f"  [{value}] {reason}")


if __name__ == "__main__":
    main()

