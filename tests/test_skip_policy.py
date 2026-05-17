"""Test that skip policy matches CEO requirements: only login/bot/dead skip."""

from __future__ import annotations

from src.main import (
    _normalize_status,
    enrich_result_for_outputs,
    get_pre_skip_reason,
    is_prepared_status,
    PREPARED_STATUSES,
    SKIPPED_STATUSES,
)


# ---------------------------------------------------------------------------
# CEO Rule 1: Only three skip reasons allowed
# ---------------------------------------------------------------------------

def test_normalize_login_reasons_to_skipped_login():
    for reason in ["login", "requires_login", "password", "会員"]:
        status = _normalize_status(status="skipped", reason=reason)
        assert status == "skipped_login", f"'{reason}' should produce skipped_login, got {status}"


def test_normalize_bot_reasons_to_skipped_bot():
    for reason in ["bot_protection", "captcha", "cloudflare", "403", "429", "blocked_domain:example.com", "domain_cooldown_until:xxx"]:
        status = _normalize_status(status="skipped", reason=reason)
        assert status == "skipped_bot_protection", f"'{reason}' should produce skipped_bot_protection, got {status}"


def test_normalize_dead_reasons_to_skipped_dead():
    for reason in ["dead_site", "name_not_resolved", "dns", "connection_refused", "ssl_error"]:
        status = _normalize_status(status="skipped", reason=reason)
        assert status == "skipped_dead_site", f"'{reason}' should produce skipped_dead_site, got {status}"


# ---------------------------------------------------------------------------
# CEO Rule 2: Everything else → prepared_review_needed or prepared_partial
# ---------------------------------------------------------------------------

def test_normalize_non_skip_reasons_to_prepared():
    """Non-login/bot/dead reasons must never produce a skipped status."""
    reasons = [
        "no_contact_page",
        "timeout_contact",
        "timeout_detect_form",
        "no_form_fields",
        "popup_or_download",
        "business_only_filter",
        "iframe_only_form",
        "embedded_or_external_form",
        "no_submit_button",
        "robots_disallowed",
        "domain_attempt_limit",
        "corporate_detected",
    ]
    for reason in reasons:
        status = _normalize_status(status="skipped", reason=reason)
        assert not status.startswith("skipped"), f"'{reason}' should NOT produce a skipped status, got {status}"
        assert status.startswith("prepared"), f"'{reason}' should produce a prepared status, got {status}"


def test_normalize_address_reasons_to_prepared_partial():
    for reason in ["requires_address", "unfilled_required_fields:3", "fill_incomplete:2/5", "timeout_fill"]:
        status = _normalize_status(status="skipped", reason=reason)
        assert status.startswith("prepared"), f"'{reason}' should produce prepared, got {status}"


# ---------------------------------------------------------------------------
# CEO Rule 3: DETECT_ONLY mode — high recall
# ---------------------------------------------------------------------------

def test_enrich_converts_no_form_to_prepared():
    result = enrich_result_for_outputs({
        "status": "skipped",
        "message": "no_form_found",
        "decision": "",
        "missing_required_fields": [],
        "any_missing_required_fields": [],
        "url": "https://example.com",
        "contact_url": "https://example.com/contact",
        "final_step_url": "https://example.com/contact",
    })
    assert result["status"] == "prepared_review_needed"


def test_enrich_converts_timeout_to_prepared():
    result = enrich_result_for_outputs({
        "status": "skipped",
        "message": "timeout_contact",
        "decision": "",
        "missing_required_fields": [],
        "any_missing_required_fields": [],
        "url": "https://example.com",
        "contact_url": "",
        "final_step_url": "",
    })
    assert result["status"] == "prepared_review_needed"


# ---------------------------------------------------------------------------
# CEO Rule 4: Evidence must be present
# ---------------------------------------------------------------------------

def test_enrich_preserves_evidence():
    result = enrich_result_for_outputs({
        "status": "prepared_review_needed",
        "message": "no_contact_page",
        "evidence": "no_obvious_contact_page_but_collected_5_candidate_links",
        "decision": "",
        "missing_required_fields": [],
        "any_missing_required_fields": [],
        "url": "https://example.com",
        "contact_url": "https://example.com",
        "final_step_url": "https://example.com",
    })
    assert result["status"] == "prepared_review_needed"
    assert "evidence" in result
    assert result["evidence"]


def test_enrich_preserves_detected_embedded_platform_and_last_action():
    result = enrich_result_for_outputs({
        "status": "prepared_review_needed",
        "message": "iframe_only_form",
        "evidence": "iframe_form_detected_needs_review:iframes=1; providers=reserva",
        "decision": "prepared_needs_manual",
        "missing_required_fields": [],
        "any_missing_required_fields": [],
        "url": "https://example.com",
        "contact_url": "https://example.com/contact",
        "final_step_url": "https://example.com/contact",
        "detected_platform": "reserva",
        "last_action": "manual_review_embedded_iframe_form",
    })
    assert result["status"] == "prepared_review_needed"
    assert result["detected_platform"] == "reserva"
    assert result["last_action"] == "manual_review_embedded_iframe_form"


# ---------------------------------------------------------------------------
# Pre-skip reason: missing data should be prepared_review_needed (not skipped)
# ---------------------------------------------------------------------------

def test_pre_skip_missing_url_not_blocked_in_high_recall_modes():
    """missing_demo_url should not block SEMI_AUTO or DETECT_ONLY."""
    lead = {"id": "1", "salon_name": "A", "url": "https://example.com", "demo_url": ""}
    assert get_pre_skip_reason(lead, {}, mode="SEMI_AUTO") == ""
    assert get_pre_skip_reason(lead, {}, mode="DETECT_ONLY") == ""


# ---------------------------------------------------------------------------
# Allowed skip/prepared status sets are exhaustive
# ---------------------------------------------------------------------------

def test_prepared_statuses_include_review_needed():
    assert "prepared_review_needed" in PREPARED_STATUSES


def test_skipped_statuses_only_three():
    assert SKIPPED_STATUSES == {"skipped_login", "skipped_bot_protection", "skipped_dead_site"}


def test_is_prepared_status_covers_all_variants():
    for status in ["prepared", "prepared_full", "prepared_partial", "prepared_external", "prepared_review_needed"]:
        assert is_prepared_status(status), f"is_prepared_status should be True for '{status}'"
    for status in ["skipped_login", "skipped_bot_protection", "skipped_dead_site", "sent", "failed"]:
        assert not is_prepared_status(status), f"is_prepared_status should be False for '{status}'"
