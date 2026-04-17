import argparse

from src.main import (
    _is_mock_mode,
    _looks_mock_demo_url,
    _looks_mock_salon_name,
    _reason_ja,
    build_cli_overrides,
    get_pre_skip_reason,
    get_skip_reason,
    should_skip_exception,
)


def test_mode_only_does_not_enable_verify() -> None:
    args = argparse.Namespace(
        dry_run=False,
        test=False,
        mode="SEMI_AUTO",
        semi_auto_verify=False,
        semi_auto_limit=3,
        no_prompt=False,
        limit=None,
        leads=None,
    )
    overrides = build_cli_overrides(args)

    assert overrides.get("mode") == "SEMI_AUTO"
    assert "semi_auto_verify" not in overrides
    assert "semi_auto_limit" not in overrides
    assert "semi_auto_prompt" not in overrides


def test_verify_flags_and_overrides() -> None:
    args = argparse.Namespace(
        dry_run=False,
        test=False,
        mode="SEMI_AUTO",
        semi_auto_verify=True,
        semi_auto_limit=5,
        no_prompt=True,
        limit=100,
        leads="data/leads.csv",
    )
    overrides = build_cli_overrides(args)

    assert overrides["mode"] == "SEMI_AUTO"
    assert overrides["semi_auto_verify"] is True
    assert overrides["semi_auto_limit"] == 5
    assert overrides["semi_auto_prompt"] is False
    assert overrides["daily_limit"] == 100
    assert overrides["leads_csv_path"] == "data/leads.csv"


def test_skip_reason_domain_suffix_match() -> None:
    settings = {
        "skip_domains": ["hotpepper.jp"],
        "skip_url_keywords": [],
    }
    should_skip, reason = get_skip_reason(
        "https://beauty.hotpepper.jp/kr/slnH000670718/",
        "beauty.hotpepper.jp",
        settings,
    )

    assert should_skip is False
    assert reason == "exclude_clear:portal_domain:hotpepper.jp"


def test_skip_reason_keyword_match() -> None:
    settings = {
        "skip_domains": [],
        "skip_url_keywords": ["rakuten"],
    }
    should_skip, reason = get_skip_reason(
        "https://example.jp/path/rakuten-campaign",
        "example.jp",
        settings,
    )

    assert should_skip is False
    assert reason == "exclude_clear:portal_url:rakuten"


def test_skip_reason_can_be_hard_skipped_with_flag() -> None:
    settings = {
        "skip_domains": ["hotpepper.jp"],
        "skip_url_keywords": [],
        "hard_skip_portals": True,
    }
    should_skip, reason = get_skip_reason(
        "https://beauty.hotpepper.jp/kr/slnH000670718/",
        "beauty.hotpepper.jp",
        settings,
    )

    assert should_skip is True
    assert reason == "exclude_clear:portal_domain:hotpepper.jp"


def test_pre_skip_reason_missing_demo_url_default_false() -> None:
    lead = {"id": "1", "salon_name": "A", "url": "https://example.com", "demo_url": ""}
    reason = get_pre_skip_reason(lead, {}, mode="SEMI_AUTO")
    assert reason == ""


def test_pre_skip_reason_missing_demo_url_only_full_auto_when_enabled() -> None:
    lead = {"id": "1", "salon_name": "A", "url": "https://example.com", "demo_url": ""}
    reason = get_pre_skip_reason(lead, {"skip_on_missing_demo_url": True}, mode="FULL_AUTO")
    assert reason == "missing_demo_url"


def test_pre_skip_reason_missing_demo_url_ignored_in_detect_and_semi() -> None:
    lead = {"id": "1", "salon_name": "A", "url": "https://example.com", "demo_url": ""}
    reason_detect = get_pre_skip_reason(lead, {"skip_on_missing_demo_url": True}, mode="DETECT_ONLY")
    reason_semi = get_pre_skip_reason(lead, {"skip_on_missing_demo_url": True}, mode="SEMI_AUTO")
    assert reason_detect == ""
    assert reason_semi == ""


def test_should_skip_exception_aggressive() -> None:
    assert should_skip_exception("Timeout 30000ms exceeded", aggressive_skip=True) is True
    assert should_skip_exception("unexpected unknown", aggressive_skip=True) is True
    assert should_skip_exception("Timeout 30000ms exceeded", aggressive_skip=False) is False


def test_pre_skip_reason_missing_base_url() -> None:
    lead = {"id": "1", "salon_name": "A", "url": "", "demo_url": "https://demo.example.com"}
    reason = get_pre_skip_reason(lead, {}, mode="SEMI_AUTO")
    assert reason == "missing_base_url"


def test_pre_skip_reason_missing_salon_name() -> None:
    lead = {"id": "1", "salon_name": "", "url": "https://example.com", "demo_url": "https://demo.example.com"}
    reason = get_pre_skip_reason(lead, {}, mode="SEMI_AUTO")
    assert reason == "missing_salon_name"


def test_pre_skip_reason_invalid_base_url() -> None:
    lead = {"id": "1", "salon_name": "A", "url": "学校A公式", "demo_url": ""}
    reason = get_pre_skip_reason(lead, {}, mode="FULL_AUTO")
    assert reason == "invalid_base_url"


def test_mock_placeholder_detectors() -> None:
    assert _looks_mock_salon_name("Mock Salon G") is True
    assert _looks_mock_salon_name("実サロン名") is False

    assert _looks_mock_demo_url("https://example.com/mock") is True
    assert _looks_mock_demo_url("https://kimotostudio12.netlify.app/01100b") is False


def test_mock_mode_flags() -> None:
    assert _is_mock_mode({}) is False
    assert _is_mock_mode({"mock_mode": True}) is True
    assert _is_mock_mode({"test_mode": True}) is True


def test_reason_ja_mapping() -> None:
    assert _reason_ja("no_form_fields", "prepared_review_needed") == "フォームなし"
    assert _reason_ja("invalid_base_url", "prepared_review_needed") == "URL不正"
    assert _reason_ja("sent", "sent") == "送信完了"
