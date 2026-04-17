from __future__ import annotations

import csv
from pathlib import Path

from src.blocklist import ensure_blocklist_files, seed_blocklist_domains_from_csv


def _read_domain_lines(path: Path) -> set[str]:
    rows: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            value = line.strip().lower()
            if value and not value.startswith("#"):
                rows.add(value)
    return rows


def test_seed_blocklist_domains_from_csv_adds_new_domains(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ensure_blocklist_files(str(data_dir))
    domains_path = data_dir / "blocklist_domains.txt"
    with domains_path.open("a", encoding="utf-8") as f:
        f.write("example.com\n")

    csv_path = tmp_path / "aidnet.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["学校名", "URL"])
        writer.writeheader()
        writer.writerow({"学校名": "A", "URL": "https://www.example.com/contact"})
        writer.writerow({"学校名": "B", "URL": "https://sub.site.jp/"})
        writer.writerow({"学校名": "C", "URL": "青山国際教育学院"})
        writer.writerow({"学校名": "D", "URL": "http://new.example.com:8443/form"})
        writer.writerow({"学校名": "E", "URL": "https://sub.site.jp/inquiry"})

    stats = seed_blocklist_domains_from_csv(str(csv_path), data_dir=str(data_dir))
    domains = _read_domain_lines(domains_path)

    assert stats["status"] == "ok"
    assert stats["url_column"] == "URL"
    assert stats["total_rows"] == 5
    assert stats["nonempty_url_rows"] == 5
    assert stats["invalid_url_rows"] == 1
    assert stats["valid_domain_count"] == 3
    assert stats["added_count"] == 2
    assert "example.com" in domains
    assert "sub.site.jp" in domains
    assert "new.example.com" in domains


def test_seed_blocklist_domains_from_csv_supports_url_alias(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ensure_blocklist_files(str(data_dir))

    csv_path = tmp_path / "aidnet_alias.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "url"])
        writer.writeheader()
        writer.writerow({"name": "A", "url": "https://school.example.jp"})

    stats = seed_blocklist_domains_from_csv(str(csv_path), data_dir=str(data_dir))
    domains = _read_domain_lines(data_dir / "blocklist_domains.txt")

    assert stats["status"] == "ok"
    assert stats["url_column"] == "url"
    assert stats["added_count"] == 1
    assert "school.example.jp" in domains
