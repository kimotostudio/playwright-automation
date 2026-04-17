from src.target_classifier import (
    EXCLUDE_MEDICAL,
    EXCLUDE_PORTAL,
    TARGET_EXCLUDE_CLEAR,
    TARGET_GOOD_SPIRITUAL_SOLO,
    classify_lead,
)


def test_medical_clinic_is_exclude_clear() -> None:
    result = classify_lead(
        {
            "name": "ゆうメンタルクリニック",
            "url": "https://www.yu-mentalclinic.com/",
            "domain": "www.yu-mentalclinic.com",
        }
    )
    assert result["target_label"] == TARGET_EXCLUDE_CLEAR
    assert EXCLUDE_MEDICAL in result["exclude_reason"]


def test_portal_domain_has_portal_listing_reason() -> None:
    result = classify_lead(
        {
            "name": "癒楽(ユラ)",
            "url": "https://beauty.hotpepper.jp/kr/slnH000670718/",
            "domain": "beauty.hotpepper.jp",
        }
    )
    assert EXCLUDE_PORTAL in result["exclude_reason"]


def test_counseling_office_is_exclude_clear() -> None:
    result = classify_lead(
        {
            "name": "志岐カウンセリングオフィス",
            "url": "https://www.shiki-counseling.com/goaisastu",
            "domain": "www.shiki-counseling.com",
        }
    )
    assert result["target_label"] == TARGET_EXCLUDE_CLEAR
    assert EXCLUDE_MEDICAL in result["exclude_reason"]


def test_spiritual_healing_salon_is_good_target() -> None:
    result = classify_lead(
        {
            "name": "岡野式神気ヒーリングサロン",
            "url": "https://shinki-healing.com/",
            "domain": "shinki-healing.com",
        }
    )
    assert result["target_label"] == TARGET_GOOD_SPIRITUAL_SOLO
    assert result["target_score"] > 0

