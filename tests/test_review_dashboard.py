from pathlib import Path

from tools.review_dashboard import normalize_col, resolve_column, screenshot_priority


def test_normalize_col_handles_japanese_and_symbols() -> None:
    assert normalize_col(" 店名 ") == "店名"
    assert normalize_col("final_step_url") == "finalstepurl"
    assert normalize_col("URL(デモ)") == "urlデモ"


def test_resolve_column_with_aliases() -> None:
    cols = ["ID", "店舗名", "final_step_url", "status"]
    assert resolve_column(cols, ["salon_id", "id", "ID"]) == "ID"
    assert resolve_column(cols, ["salon_name", "店名", "店舗名"]) == "店舗名"
    assert resolve_column(cols, ["url", "URL", "final_step_url"]) == "final_step_url"


def test_screenshot_priority_order() -> None:
    p4 = Path("1000_04_on_confirmation_page.png")
    p3 = Path("1000_03_before_submit_or_confirm.png")
    p2 = Path("1000_02_after_fill.png")
    p1 = Path("1000_01_before_fill.png")
    ordered = sorted([p1, p3, p2, p4], key=screenshot_priority)
    assert ordered == [p4, p3, p2, p1]
