"""Review queue helpers for SEMI_AUTO workflow."""

from __future__ import annotations

import csv
import glob
import os
from datetime import datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")

FIELDNAMES = [
    "timestamp",
    "salon_id",
    "salon_name",
    "domain",
    "contact_url",
    "final_step_url",
    "submit_selector",
    "confirm_selector",
    "screenshot_folder",
    "status",
    "reason",
    "notes",
    "evidence",
    "detected_required_fields",
    "filled_fields",
    "missing_required_fields",
    "validation_errors",
    "decision",
    "confidence_level",
    "stop_state",
    "detected_platform",
    "reopen_in_browser_url",
    "form_root_selector",
    "field_selector_map",
    "last_action",
]


def queue_path(date_str: Optional[str] = None, results_dir: str = DEFAULT_RESULTS_DIR) -> str:
    if not date_str:
        date_str = datetime.now(JST).strftime("%Y%m%d")
    return os.path.join(results_dir, f"review_queue_{date_str}.csv")


def read_queue(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def append_review_entry(entry: dict, results_dir: str = DEFAULT_RESULTS_DIR, date_str: Optional[str] = None) -> tuple[str, bool]:
    """Append one queue entry, idempotent by salon_id per day."""
    path = queue_path(date_str=date_str, results_dir=results_dir)
    rows = read_queue(path)
    salon_id = str(entry.get("salon_id", "")).strip()

    if any(str(row.get("salon_id", "")).strip() == salon_id for row in rows):
        return path, False

    normalized = {key: str(entry.get(key, "")).strip() for key in FIELDNAMES}
    if not normalized["timestamp"]:
        normalized["timestamp"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    rows.append(normalized)
    _write_rows(path, rows)
    return path, True


def update_review_status(
    salon_id: str,
    status: str,
    notes: str,
    results_dir: str = DEFAULT_RESULTS_DIR,
    queue_file: Optional[str] = None,
) -> Optional[str]:
    """Update latest queue row by salon_id."""
    if queue_file:
        candidates = [queue_file]
    else:
        candidates = sorted(glob.glob(os.path.join(results_dir, "review_queue_*.csv")), reverse=True)

    target = str(salon_id).strip()
    for path in candidates:
        rows = read_queue(path)
        if not rows:
            continue
        updated = False
        for idx in range(len(rows) - 1, -1, -1):
            if str(rows[idx].get("salon_id", "")).strip() == target:
                rows[idx]["status"] = status
                rows[idx]["notes"] = notes
                updated = True
                break
        if updated:
            _write_rows(path, rows)
            return path
    return None


def find_prepared_entry(
    salon_id: str,
    results_dir: str = DEFAULT_RESULTS_DIR,
    queue_file: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """Find latest queue row with status prepared for salon_id."""
    if queue_file:
        candidates = [queue_file]
    else:
        candidates = sorted(glob.glob(os.path.join(results_dir, "review_queue_*.csv")), reverse=True)

    target = str(salon_id).strip()
    for path in candidates:
        rows = read_queue(path)
        for idx in range(len(rows) - 1, -1, -1):
            row = rows[idx]
            if str(row.get("salon_id", "")).strip() != target:
                continue
            status_value = str(row.get("status", "")).strip().lower()
            if status_value == "prepared" or status_value.startswith("prepared_"):
                return row, path
    return None, None
