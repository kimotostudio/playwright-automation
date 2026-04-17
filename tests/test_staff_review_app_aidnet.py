from __future__ import annotations

import csv
from pathlib import Path

from src.staff_review_app import _load_aidnet_rows


def test_load_aidnet_rows_maps_url_and_domain(tmp_path: Path) -> None:
    csv_path = tmp_path / "aidnet.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["都道府県", "学校名", "URL", "問い合わせ日", "ワーカー名"])
        writer.writeheader()
        writer.writerow(
            {
                "都道府県": "東京都",
                "学校名": "学校A",
                "URL": "https://www.school-a.example.jp/contact",
                "問い合わせ日": "2月16日",
                "ワーカー名": "担当A",
            }
        )
        writer.writerow(
            {
                "都道府県": "福岡県",
                "学校名": "学校B",
                "URL": "学校B公式",
                "問い合わせ日": "2月17日",
                "ワーカー名": "担当B",
            }
        )

    df = _load_aidnet_rows(str(csv_path))

    assert len(df) == 2
    assert df.iloc[0]["id"] == "aidnet-0001"
    assert df.iloc[0]["name"] == "学校A"
    assert df.iloc[0]["contact_url"] == "https://www.school-a.example.jp/contact"
    assert df.iloc[0]["domain"] == "school-a.example.jp"
    assert df.iloc[0]["reason"] == "aidnet_domain_list"
    assert "prefecture=東京都" in str(df.iloc[0]["evidence"])

    assert df.iloc[1]["id"] == "aidnet-0002"
    assert df.iloc[1]["name"] == "学校B"
    assert df.iloc[1]["contact_url"] == ""
    assert df.iloc[1]["reason"] == "aidnet_non_url_entry"
    assert "raw_url=学校B公式" in str(df.iloc[1]["evidence"])
