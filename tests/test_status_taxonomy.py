import csv
import tempfile
import unittest
from pathlib import Path

from src.main import enrich_result_for_outputs, is_prepared_status
from src.review_queue import append_review_entry, find_prepared_entry, read_queue


class StatusTaxonomyTests(unittest.TestCase):
    def test_is_prepared_status_prefix(self) -> None:
        self.assertTrue(is_prepared_status("prepared"))
        self.assertTrue(is_prepared_status("prepared_full"))
        self.assertTrue(is_prepared_status("prepared_partial"))
        self.assertFalse(is_prepared_status("skipped_login"))

    def test_enrich_result_status_normalization(self) -> None:
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
        self.assertEqual(result["status"], "prepared_review_needed")
        self.assertEqual(result["confidence_level"], "low")
        self.assertEqual(result["missing_required_fields_json"], "[]")

    def test_find_prepared_entry_supports_prepared_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.get("status"), "prepared_full")
            self.assertEqual(path, str(queue))

    def test_append_review_entry_can_allow_explicit_retry_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "timestamp": "2026-02-13 10:00:00",
                "salon_id": "retry-1",
                "salon_name": "Retry Test",
                "status": "prepared_review_needed",
            }

            path, added = append_review_entry(entry, results_dir=tmp, date_str="20260213")
            self.assertTrue(added)

            _, added = append_review_entry(entry, results_dir=tmp, date_str="20260213")
            self.assertFalse(added)
            self.assertEqual(len(read_queue(path)), 1)

            _, added = append_review_entry(entry, results_dir=tmp, date_str="20260213", allow_duplicate=True)
            self.assertTrue(added)
            self.assertEqual(len(read_queue(path)), 2)


if __name__ == "__main__":
    unittest.main()
