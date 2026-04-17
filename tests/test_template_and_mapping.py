import csv
import json
from pathlib import Path

from src.main import load_leads
from src.message_generator import MessageGenerator


def _build_generator(tmp_path: Path) -> MessageGenerator:
    template_path = tmp_path / "template.txt"
    sender_path = tmp_path / "sender.json"

    template_path.write_text(
        "{salon_name} 様\nデモサイト: {demo_url}\n不要と返信で停止\n",
        encoding="utf-8",
    )
    sender_path.write_text(
        json.dumps(
            {
                "name": "木許 裕輔",
                "email": "kimoto.studio21@gmail.com",
                "phone": "08042846455",
                "company": "KIMOTO STUDIO",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return MessageGenerator(str(template_path), str(sender_path), wrap_message=False)


def test_resolve_lead_fields_variants(tmp_path: Path) -> None:
    mg = _build_generator(tmp_path)

    row = {
        "店舗名": "テストサロン",
        "url(デモページ)": "https://demo.example.jp/a",
        "URL": "https://old.example.jp/a",
    }
    resolved = mg.resolve_lead_fields(row)

    assert resolved["salon_name"] == "テストサロン"
    assert resolved["demo_url"] == "https://demo.example.jp/a"
    assert resolved["old_url"] == "https://old.example.jp/a"


def test_generate_message_contains_real_values(tmp_path: Path) -> None:
    mg = _build_generator(tmp_path)
    message = mg.generate("実サロン", "https://real-demo.example.jp")

    assert "実サロン" in message
    assert "https://real-demo.example.jp" in message
    assert "Mock Salon" not in message


def test_generate_subject_uses_sender_info_subject(tmp_path: Path) -> None:
    template_path = tmp_path / "template.txt"
    sender_path = tmp_path / "sender.json"
    template_path.write_text("本文", encoding="utf-8")
    sender_path.write_text(
        json.dumps(
            {
                "name": "山本舞",
                "email": "media.dm@goi-holdings.com",
                "phone": "08032573029",
                "company": "株式会社GOIホールディングス",
                "subject": "おすすめの日本語学校に関する記事掲載につきまして",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mg = MessageGenerator(str(template_path), str(sender_path), wrap_message=False)
    assert mg.generate_subject("任意名") == "おすすめの日本語学校に関する記事掲載につきまして"


def test_load_leads_header_mapping(tmp_path: Path) -> None:
    csv_path = tmp_path / "leads.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "店舗名", "URL", "url(デモページ)"])
        writer.writeheader()
        writer.writerow(
            {
                "id": "9001",
                "店舗名": "店舗A",
                "URL": "https://old.example.jp/9001",
                "url(デモページ)": "https://demo.example.jp/9001",
            }
        )

    leads = load_leads(str(csv_path))
    assert len(leads) == 1
    assert leads[0]["id"] == "9001"
    assert leads[0]["salon_name"] == "店舗A"
    assert leads[0]["url"] == "https://old.example.jp/9001"
    assert leads[0]["demo_url"] == "https://demo.example.jp/9001"


def test_load_leads_header_mapping_fullwidth_variants(tmp_path: Path) -> None:
    csv_path = tmp_path / "leads_fullwidth.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ID", "名称", "url（旧）", "url（デモページ）"])
        writer.writeheader()
        writer.writerow(
            {
                "ID": "9002",
                "名称": "店舗B",
                "url（旧）": "https://old.example.jp/9002",
                "url（デモページ）": "https://demo.example.jp/9002",
            }
        )

    leads = load_leads(str(csv_path))
    assert len(leads) == 1
    assert leads[0]["id"] == "9002"
    assert leads[0]["salon_name"] == "店舗B"
    assert leads[0]["url"] == "https://old.example.jp/9002"
    assert leads[0]["demo_url"] == "https://demo.example.jp/9002"


def test_load_leads_aidnet_style_without_id(tmp_path: Path) -> None:
    csv_path = tmp_path / "aidnet_like.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["学校名", "URL"])
        writer.writeheader()
        writer.writerow({"学校名": "日本語学校A", "URL": "https://school-a.example.jp/"})
        writer.writerow({"学校名": "日本語学校B", "URL": "学校B公式"})

    leads = load_leads(str(csv_path))
    assert len(leads) == 2
    assert leads[0]["id"] == "aidnet-0001"
    assert leads[0]["salon_name"] == "日本語学校A"
    assert leads[0]["url"] == "https://school-a.example.jp/"
    assert leads[1]["id"] == "aidnet-0002"
    assert leads[1]["salon_name"] == "日本語学校B"
    assert leads[1]["url"] == "学校B公式"


def test_wrap_keeps_url_unbroken_and_name_present(tmp_path: Path) -> None:
    template_path = tmp_path / "template_wrap.txt"
    sender_path = tmp_path / "sender_wrap.json"

    template_path.write_text(
        "{salon_name} 様\n\n【参考デモ】・デモサイト: {demo_url} ・補足: 返信歓迎\n\nKIMOTO STUDIO",
        encoding="utf-8",
    )
    sender_path.write_text(
        json.dumps(
            {
                "name": "木許 裕輔",
                "email": "kimoto.studio21@gmail.com",
                "phone": "08042846455",
                "company": "KIMOTO STUDIO",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mg = MessageGenerator(str(template_path), str(sender_path), wrap_message=True, wrap_width=40, debug=True)
    demo_url = "https://kimotostudio12.netlify.app/01100b"
    message = mg.generate("実サロン名", demo_url)

    assert "実サロン名" in message
    assert demo_url in message
    assert "\n\n" in message
    assert "�" not in message


def test_sanitize_message_for_legacy_encodings_preserves_url(tmp_path: Path) -> None:
    template_path = tmp_path / "template_sanitize.txt"
    sender_path = tmp_path / "sender_sanitize.json"

    template_path.write_text(
        "{salon_name} 様\n\n────────────────\nデモ: {demo_url}\n────────────────\n",
        encoding="utf-8",
    )
    sender_path.write_text(
        json.dumps(
            {
                "name": "木許 裕輔",
                "email": "kimoto.studio21@gmail.com",
                "phone": "08042846455",
                "company": "KIMOTO STUDIO",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mg = MessageGenerator(str(template_path), str(sender_path), wrap_message=False)
    demo_url = "https://kimotostudio12.netlify.app/01100b"
    message = mg.generate("検証サロン", demo_url)

    assert "-----" in message
    assert "────────────────" not in message
    assert demo_url in message
