"""Report generator: daily summary markdown + CLI --report-only mode."""

import csv
import json
import logging
import os
from collections import Counter
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")


def generate_summary_report(
    results: List[dict],
    stats: dict,
    settings: dict,
    blocked_domains_today: List[str],
    next_lead_id: str,
    unprocessed_count: int,
    results_dir: str,
    next_lead_index: int | None = None,
) -> str:
    """Generate a daily summary markdown report."""
    date_str = datetime.now(JST).strftime("%Y%m%d")
    output_path = os.path.join(results_dir, f"summary_{date_str}.md")

    reason_counts = Counter()
    for row in results:
        reason = (row.get("message", "") or "unknown").strip()
        reason_counts[reason[:80] or "unknown"] += 1

    lines = [
        f"# Daily Summary - {datetime.now(JST).strftime('%Y-%m-%d')}",
        "",
        "## Results",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Processed | {stats.get('total', 0)} |",
        f"| Sent | {stats.get('sent', 0)} |",
        f"| Prepared | {stats.get('prepared', 0)} |",
        f"| Failed | {stats.get('failed', 0)} |",
        f"| Skipped | {stats.get('skipped', 0)} |",
        "",
    ]

    if reason_counts:
        lines.extend(
            [
                "## Top Reasons",
                "",
                "| Reason | Count |",
                "|--------|-------|",
            ]
        )
        for reason, count in reason_counts.most_common(10):
            lines.append(f"| {reason.replace('|', '/')} | {count} |")
        lines.append("")

    lines.append("## Newly Blocked Domains")
    lines.append("")
    if blocked_domains_today:
        for domain in sorted(set(blocked_domains_today)):
            lines.append(f"- {domain}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Next Lead")
    lines.append("")
    lines.append(f"- next_id: {next_lead_id or 'N/A'}")
    if next_lead_index is not None:
        lines.append(f"- next_index: {next_lead_index}")
    lines.append(f"- remaining_unprocessed: {unprocessed_count}")
    lines.append(f"- sent_remaining_today: {stats.get('remaining', 'N/A')}")
    lines.append("")

    if settings.get("dry_run", False):
        lines.append("> dry_run=true (final submit is disabled)")
        lines.append("")

    content = "\n".join(lines)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"[REPORT] Summary saved: {output_path}")
    return output_path


def print_report_from_files(results_dir: str, data_dir: str) -> None:
    """Print latest available summary (or fallback from latest results CSV)."""
    summary_candidates = sorted(
        [
            os.path.join(results_dir, name)
            for name in os.listdir(results_dir)
            if name.startswith("summary_") and name.endswith(".md")
        ],
        reverse=True,
    ) if os.path.exists(results_dir) else []

    if summary_candidates:
        with open(summary_candidates[0], "r", encoding="utf-8") as f:
            print(f.read())
        return

    csv_candidates = sorted(
        [
            os.path.join(results_dir, name)
            for name in os.listdir(results_dir)
            if name.startswith("submissions_") and name.endswith(".csv")
        ],
        reverse=True,
    ) if os.path.exists(results_dir) else []

    if not csv_candidates:
        print("No summary/results found yet.")
        return

    results_csv = csv_candidates[0]

    total = sent = failed = skipped = 0
    prepared = 0
    reasons = Counter()
    with open(results_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            status = row.get("status", "")
            if status == "sent":
                sent += 1
            elif status == "prepared":
                prepared += 1
            elif status == "failed":
                failed += 1
                reasons[row.get("message", "unknown")] += 1
            elif status == "skipped":
                skipped += 1
                reasons[row.get("message", "unknown")] += 1

    # Read state
    state_path = os.path.join(data_dir, "state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

    print(f"=== Latest Report ({os.path.basename(results_csv)}) ===")
    print(f"Total: {total}  Sent: {sent}  Prepared: {prepared}  Failed: {failed}  Skipped: {skipped}")
    if total > 0:
        print(f"Success rate: {sent/total:.1%}")
    print(f"Today's count: {state.get('today_count', '?')}")
    print(f"Total sent (all time): {state.get('total_sent', '?')}")

    if reasons:
        print("\nTop failure reasons:")
        for reason, count in reasons.most_common(5):
            print(f"  [{count}x] {reason[:70]}")

    # Check ledger
    ledger_path = os.path.join(data_dir, "submission_ledger.csv")
    if os.path.exists(ledger_path):
        ledger_count = 0
        with open(ledger_path, "r", encoding="utf-8-sig") as f:
            for _ in csv.DictReader(f):
                ledger_count += 1
        print(f"\nLedger entries: {ledger_count}")


class JsonlHandler(logging.Handler):
    """Logging handler that outputs JSON lines format."""

    def __init__(self, filepath: str):
        super().__init__()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        self.filepath = filepath
        self.file = open(filepath, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": datetime.fromtimestamp(record.created, tz=JST).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
            }
            # Extract structured fields from extra
            for field in ("salon_id", "domain", "step", "status", "reason", "url"):
                val = getattr(record, field, None)
                if val is not None:
                    entry[field] = val

            self.file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.file.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.file.close()
        super().close()
