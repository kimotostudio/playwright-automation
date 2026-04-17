import csv
from pathlib import Path

from src.main import enrich_result_for_outputs, is_prepared_status
from src.review_queue import find_prepared_entry


def test_is_prepared_status_prefix() -> None:
    assert is_prepared_status("prepared")
    assert is_prepared_status("prepared_full")
    assert is_prepared_status("prepared_partial")
    assert not is_prepared_status("skipped_login")


def test_enrich_result_status_normalization() -> None:
    result = enrich_result_for_outputs(
        {
            "status": "skipped",
            "message": "no_contact_page",
            "decision": "",
            "missing_required_fields": [],
            "any_missing_required_fields": [],
            "url": "https://example.com",
            "contact_url": "",
            "final_step_url": "",
        }
    )
    assert result["status"] == "prepared_review_needed"
    assert result["confidence_level"] == "low"
    assert result["missing_required_fields_json"] == "[]"


def test_find_prepared_entry_supports_prepared_variants(tmp_path: Path) -> None:
    queue = tmp_path / "review_queue_20260213.csv"
    fieldnames = [
        "timestamp",
        "salon_id",
        "salon_name",
        "status",
    ]
    rows = [
        {"timestamp": "2026-02-13 10:00:00", "salon_id": "1001", "salon_name": "A", "status": "prepared_full"},
        {"timestamp": "2026-02-13 10:00:01", "salon_id": "1002", "salon_name": "B", "status": "skipped_login"},
    ]
    with queue.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    row, path = find_prepared_entry("1001", results_dir=str(tmp_path))
    assert row is not None
    assert row.get("status") == "prepared_full"
    assert path == str(queue)

