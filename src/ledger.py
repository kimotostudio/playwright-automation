"""Durable submission ledger utilities.

Ledger columns:
timestamp, run_mode, salon_id, salon_name, domain, contact_url, final_step_url, status, reason
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

FIELDNAMES = [
    "timestamp",
    "run_mode",
    "salon_id",
    "salon_name",
    "domain",
    "contact_url",
    "final_step_url",
    "status",
    "reason",
]

DEFAULT_LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "submission_ledger.csv"
)


def _normalize_status_value(status: str, reason: str) -> str:
    value = str(status or "").strip().lower()
    reason_l = str(reason or "").strip().lower()
    if value == "sent":
        return "sent"
    if value.startswith("prepared_") or value in {"prepared_external", "prepared_partial", "prepared_full", "prepared_review_needed"}:
        return value
    if value.startswith("skipped_"):
        return value
    if value == "prepared":
        if "external_form" in reason_l:
            return "prepared_external"
        return "prepared_review_needed"

    if any(token in reason_l for token in ["login", "requires_login", "required_login", "password", "会員", "認証"]):
        return "skipped_login"
    if any(token in reason_l for token in ["bot_protection", "captcha", "cloudflare", "verify you are human", "access denied", "403", "429"]):
        return "skipped_bot_protection"
    if any(token in reason_l for token in ["dead_site", "name_not_resolved", "dns", "connection_refused", "ssl_error", "net::err_name_not_resolved"]):
        return "skipped_dead_site"
    if any(token in reason_l for token in ["requires_address", "missing_required", "unfilled_required", "fill_incomplete", "timeout_fill"]):
        return "prepared_partial"
    return "prepared_review_needed"


def _normalize_entry(entry: dict) -> dict:
    normalized = {k: "" for k in FIELDNAMES}
    for key in FIELDNAMES:
        value = entry.get(key, "")
        normalized[key] = str(value).strip() if value is not None else ""
    if not normalized["timestamp"]:
        normalized["timestamp"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    normalized["status"] = _normalize_status_value(normalized["status"], normalized["reason"])
    return normalized


def read_ledger(path: str = DEFAULT_LEDGER_PATH) -> dict:
    """Read ledger and build indexes by salon_id and domain."""
    rows: List[dict] = []
    by_salon_id: Dict[str, List[dict]] = {}
    by_domain: Dict[str, List[dict]] = {}
    sent_ids = set()

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                normalized = _normalize_entry(row)
                rows.append(normalized)

                salon_id = normalized["salon_id"]
                domain = normalized["domain"].lower()
                status = normalized["status"].lower()

                by_salon_id.setdefault(salon_id, []).append(normalized)
                by_domain.setdefault(domain, []).append(normalized)
                if salon_id and status == "sent":
                    sent_ids.add(salon_id)

    return {
        "path": path,
        "rows": rows,
        "by_salon_id": by_salon_id,
        "by_domain": by_domain,
        "sent_ids": sent_ids,
    }


def ledger_has(salon_id: str, path: str = DEFAULT_LEDGER_PATH) -> bool:
    """True if this salon_id already has a sent record in ledger."""
    target = str(salon_id).strip()
    if not target:
        return False
    data = read_ledger(path)
    return target in data["sent_ids"]


def append_ledger(entry: dict, path: str = DEFAULT_LEDGER_PATH) -> dict:
    """Atomically append one ledger row.

    This performs read -> write temp -> os.replace to avoid partial writes.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    normalized = _normalize_entry(entry)

    existing: List[dict] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            existing = [_normalize_entry(row) for row in reader]

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        if existing:
            writer.writerows(existing)
        writer.writerow(normalized)

    os.replace(tmp_path, path)
    return normalized
