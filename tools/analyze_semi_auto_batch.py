#!/usr/bin/env python3
"""Analyze local SEMI_AUTO artifacts and emit lead-quality feedback."""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
DEFAULT_HANDOFF = PROJECT_ROOT / "data" / "handoff_review_batch.csv"

MEDIA_LISTING_TOKENS = [
    "新聞",
    "情報",
    "掲載",
    "広告",
    "広告掲載",
    "媒体",
    "ポータル",
    "portal",
    "listing",
    "directory",
    "media",
    "advertis",
    "contact-pr",
    "press",
]
SOCIAL_OR_EXTERNAL_CONTACT_HOSTS = [
    "lin.ee",
    "line.me",
    "page.line.me",
    "instagram.com",
    "facebook.com",
    "x.com",
    "twitter.com",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _norm_domain(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        raw = urlparse(raw).netloc
    raw = raw.split("/", 1)[0].split(":", 1)[0].strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def _host(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    return (parsed.hostname or "").lower()


def _same_site(a: str, b: str) -> bool:
    da = _norm_domain(a)
    db = _norm_domain(b)
    return bool(da and db and (da == db or da.endswith(f".{db}") or db.endswith(f".{da}")))


def _contains_any(text: str, tokens: Iterable[str]) -> bool:
    haystack = str(text or "").lower()
    return any(token.lower() in haystack for token in tokens)


def _non_empty_signal(value: str) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "[]", "{}", "none", "null", "nan"}:
        return ""
    return text


def _latest_date() -> str:
    candidates = sorted(RESULTS_DIR.glob("submissions_*.csv"), reverse=True)
    for path in candidates:
        match = re.search(r"submissions_(\d{8})\.csv$", path.name)
        if match:
            return match.group(1)
    return datetime.now(JST).strftime("%Y%m%d")


def _latest_batch_summary(results_dir: Path, date_str: str) -> Path:
    candidates = sorted(results_dir.glob(f"semi_auto_batch_summary_{date_str}_*.md"), reverse=True)
    if candidates:
        return candidates[0]
    return results_dir / f"semi_auto_batch_summary_{date_str}.md"


def _index_handoff(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        lead_id = str(row.get("id") or row.get("lead_id") or "").strip()
        if lead_id:
            indexed[lead_id] = row
    return indexed


def _index_queue(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        lead_id = str(row.get("salon_id", "")).strip()
        if lead_id:
            previous = indexed.get(lead_id)
            if previous is None:
                indexed[lead_id] = row
                continue
            row_time = _parse_timestamp(str(row.get("timestamp", ""))) or datetime.min.replace(tzinfo=JST)
            prev_time = _parse_timestamp(str(previous.get("timestamp", ""))) or datetime.min.replace(tzinfo=JST)
            if row_time >= prev_time:
                indexed[lead_id] = row
    return indexed


def _latest_submissions_for_handoff(
    submissions: list[dict[str, str]],
    handoff_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return the newest submission row for each current handoff lead, preserving batch order."""
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for row in handoff_rows:
        lead_id = str(row.get("id") or row.get("lead_id") or "").strip()
        if lead_id and lead_id not in seen:
            ordered_ids.append(lead_id)
            seen.add(lead_id)
    if not ordered_ids:
        return submissions

    wanted = set(ordered_ids)
    latest: dict[str, dict[str, str]] = {}
    for row in submissions:
        lead_id = str(row.get("salon_id", "")).strip()
        if lead_id not in wanted:
            continue
        previous = latest.get(lead_id)
        if previous is None:
            latest[lead_id] = row
            continue
        row_time = _parse_timestamp(str(row.get("timestamp", ""))) or datetime.min.replace(tzinfo=JST)
        prev_time = _parse_timestamp(str(previous.get("timestamp", ""))) or datetime.min.replace(tzinfo=JST)
        if row_time >= prev_time:
            latest[lead_id] = row
    return [latest[lead_id] for lead_id in ordered_ids if lead_id in latest]


def _split_log_by_lead(log_path: Path) -> dict[str, list[str]]:
    segments: dict[str, list[str]] = {}
    if not log_path.exists():
        return segments
    current = ""
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.search(r"Lead resolved: id=([^,]+),", line)
        if match:
            current = match.group(1).strip()
        if current:
            segments.setdefault(current, []).append(line)
    return segments


def _choose_screenshot(lead_id: str, screenshot_dir: Path) -> str:
    if not screenshot_dir.exists():
        return ""
    candidates = sorted(screenshot_dir.glob(f"{lead_id}_*.png"))
    priorities = [
        "before_submit_or_confirm",
        "after_fill",
        "no_form_fields",
        "before_fill",
    ]
    for token in priorities:
        for path in candidates:
            if token in path.name:
                return str(path)
    if candidates:
        return str(candidates[0])
    root_confirm = SCREENSHOTS_DIR / f"{lead_id}_confirm.png"
    if root_confirm.exists():
        return str(root_confirm)
    return ""


def _status_class(status: str) -> str:
    value = str(status or "").strip()
    if value == "prepared_full":
        return "prepared_full"
    if value.startswith("skipped"):
        return "skipped"
    if value == "failed":
        return "failed"
    return "partial/manual_review"


def _corporate_context(reason: str, handoff: dict[str, str]) -> tuple[str, str, str]:
    match = re.search(r"corporate_skip: '([^']+)' detected", str(reason or ""))
    if not match:
        return "", "", ""
    keyword = match.group(1)
    notes = str(handoff.get("notes", ""))
    source_hint = "unknown_page_text"
    false_positive = "possible" if "solo" in notes and "corporate" not in notes else "unknown"
    if "corporate" in notes:
        source_hint = "lead_finder_notes_and_page_text"
        false_positive = "lower"
    return keyword, source_hint, false_positive


def _lead_finder_recommendation(
    lead_quality: list[str],
    contact_quality: list[str],
    form_quality: list[str],
    feedback: list[str],
) -> tuple[str, str, str]:
    exclude_reasons: list[str] = []
    penalty = 0

    if "bot_or_protection_page" in form_quality:
        exclude_reasons.append("bot_or_protection_page")
    if "listing_or_media_form" in lead_quality:
        exclude_reasons.append("listing_or_media_form")
    if "external_line_or_sns_page" in contact_quality:
        exclude_reasons.append("external_line_or_sns_page")
    if "corporate_or_large_business" in lead_quality:
        exclude_reasons.append("corporate_or_large_business")
    if "iframe_only_form" in form_quality:
        exclude_reasons.append("iframe_only_form")
    if "unsuitable_contact_target" in contact_quality:
        exclude_reasons.append("unsuitable_contact_target")

    if "low_confidence_display_name" in lead_quality:
        penalty -= 15
    if "weak_contact_url" in contact_quality:
        penalty -= 25
    if "external_contact_domain" in contact_quality:
        penalty -= 20
    if "missing_required_fields" in form_quality:
        penalty -= 10
    if "no_form_fields" in form_quality:
        penalty -= 20
    if "manual_review_needed" in form_quality:
        penalty -= 5
    if "review_display_name" in feedback:
        penalty -= 5

    if exclude_reasons:
        return "exclude", ";".join(dict.fromkeys(exclude_reasons)), "-100"
    if penalty < 0:
        return "score_penalty", "", str(penalty)
    return "keep", "", "0"


def _analyze_lead(
    submission: dict[str, str],
    queue_row: dict[str, str],
    handoff: dict[str, str],
    log_lines: list[str],
    screenshot_dir: Path,
) -> dict[str, str]:
    lead_id = str(submission.get("salon_id", "")).strip()
    status = str(submission.get("status", "")).strip()
    reason = str(submission.get("message", "") or queue_row.get("reason", "")).strip()
    domain = (
        str(queue_row.get("domain", "")).strip()
        or str(handoff.get("domain", "")).strip()
        or _norm_domain(submission.get("contact_url", ""))
    )
    display_name = str(handoff.get("display_name") or submission.get("salon_name") or "").strip()
    contact_url = str(submission.get("contact_url") or queue_row.get("contact_url") or handoff.get("contact_url") or "").strip()
    final_url = str(submission.get("final_step_url") or queue_row.get("final_step_url") or "").strip()
    website = str(handoff.get("website") or handoff.get("url") or submission.get("url") or "").strip()
    notes = str(handoff.get("notes", ""))
    log_text = "\n".join(log_lines)
    combined_text = " ".join(
        [
            display_name,
            str(handoff.get("business_name", "")),
            str(handoff.get("salon_name", "")),
            str(handoff.get("original__title", "")),
            notes,
            website,
            contact_url,
            final_url,
        ]
    )

    lead_quality: list[str] = []
    contact_quality: list[str] = []
    form_quality: list[str] = []
    feedback: list[str] = []
    warnings: list[str] = []
    missing_required = _non_empty_signal(
        submission.get("missing_required_fields", "") or queue_row.get("missing_required_fields", "")
    )
    review_notes = str(queue_row.get("notes", "") or submission.get("notes", "")).strip()
    filled_fields = str(queue_row.get("filled_fields", "")).strip()
    detected_required_fields = str(queue_row.get("detected_required_fields", "")).strip()
    contact_target_text = " ".join([combined_text, log_text, review_notes])

    if _contains_any(combined_text, MEDIA_LISTING_TOKENS):
        lead_quality.append("listing_or_media_form")
        contact_quality.append("operator_contact_form")
        contact_quality.append("unsuitable_contact_target")
        feedback.extend(["lower_score_portal_contact", "exclude_or_review_media_domains"])
        warnings.append("prepared target appears to be listing/media/operator form")
    elif _contains_any(contact_target_text, MEDIA_LISTING_TOKENS):
        contact_quality.append("unsuitable_contact_target")
        feedback.extend(["lower_score_unsuitable_contact_target", "improve_contact_url_discovery"])
        warnings.append("contact target appears to be PR/media/operator inquiry rather than local business form")

    if str(handoff.get("name_confidence", "")).lower() == "low" or str(handoff.get("name_warning", "")).strip():
        lead_quality.append("low_confidence_display_name")
        feedback.append("review_display_name")

    if reason.startswith("corporate_skip"):
        lead_quality.append("corporate_or_large_business")
        feedback.append("lower_score_corporate")

    contact_host = _host(contact_url or final_url)
    if any(contact_host == host or contact_host.endswith(f".{host}") for host in SOCIAL_OR_EXTERNAL_CONTACT_HOSTS):
        contact_quality.append("external_line_or_sns_page")
        feedback.append("improve_contact_url_discovery")

    if contact_url and website and not _same_site(contact_url, website):
        contact_quality.append("external_contact_domain")
        feedback.append("improve_contact_url_discovery")

    if "Fallback contact candidate selected" in log_text:
        contact_quality.append("weak_contact_url")
        feedback.append("improve_contact_url_discovery")

    if "bot_protection" in status or "bot_protection" in reason:
        form_quality.append("bot_or_protection_page")
        feedback.extend(["exclude_domain_or_manual_only", "do_not_auto_retry"])
    elif (
        status == "prepared_partial"
        or "unfilled_required_fields" in reason
        or "missing_required" in reason
        or bool(missing_required)
        or "required_fields_missing_or_failed" in review_notes
    ):
        form_quality.append("missing_required_fields")
        feedback.append("improve_required_field_diagnostics")
    elif reason == "no_form_fields":
        form_quality.append("no_form_fields")
        feedback.extend(["improve_contact_url_discovery", "retry_manual_only"])
    elif reason == "iframe_only_form":
        form_quality.append("iframe_only_form")
        contact_quality.append("iframe_only_contact_target")
        feedback.extend(["avoid_iframe_only_contact_targets", "retry_manual_only"])
    elif status == "prepared_full" and not warnings:
        form_quality.append("form_ok")

    if status == "prepared_full" and warnings:
        form_quality.append("manual_review_needed")
        feedback.append("retry_manual_only")

    if not lead_quality and status.startswith("prepared_review_needed"):
        lead_quality.append("manual_review_needed")
    if not contact_quality:
        contact_quality.append("contact_ok" if status == "prepared_full" and not warnings else "manual_review_needed")
    if not form_quality:
        form_quality.append("manual_review_needed")

    corporate_keyword, corporate_source, corporate_false_positive = _corporate_context(reason, handoff)
    required_field_detail = ""
    if "missing_required_fields" in form_quality:
        required_field_detail = (detected_required_fields or missing_required).replace("\n", " ")[:240]

    if "bot_or_protection_page" in form_quality:
        action = "do_not_auto_retry"
    elif status == "prepared_full" and not warnings:
        action = "human_review_before_manual_submit"
    elif "unsuitable_contact_target" in contact_quality:
        action = "manual_review_likely_skip"
    elif "listing_or_media_form" in lead_quality:
        action = "manual_review_likely_skip"
    elif "missing_required_fields" in form_quality:
        action = "manual_complete_or_patch_required_field_detection"
    elif "iframe_only_form" in form_quality:
        action = "manual_review_likely_skip"
    else:
        action = "manual_review"

    lead_finder_action, lead_finder_exclusion_reason, lead_finder_score_penalty = _lead_finder_recommendation(
        lead_quality,
        contact_quality,
        form_quality,
        feedback,
    )

    return {
        "lead_id": lead_id,
        "domain": domain,
        "display_name": display_name,
        "final_status": status,
        "status_class": _status_class(status),
        "reason": reason,
        "lead_quality_issue": ";".join(dict.fromkeys(lead_quality)),
        "contact_quality_issue": ";".join(dict.fromkeys(contact_quality)),
        "form_quality_issue": ";".join(dict.fromkeys(form_quality)),
        "recommended_action": action,
        "feedback_for_lead_finder": ";".join(dict.fromkeys(feedback)) or "none",
        "lead_finder_recommended_action": lead_finder_action,
        "lead_finder_exclusion_reason": lead_finder_exclusion_reason,
        "lead_finder_score_penalty": lead_finder_score_penalty,
        "prepared_full_quality_warning": "yes" if status == "prepared_full" and warnings else "no",
        "quality_warning_detail": "; ".join(warnings),
        "screenshot_path": _choose_screenshot(lead_id, screenshot_dir),
        "contact_url": contact_url,
        "final_step_url": final_url,
        "detected_platform": str(submission.get("detected_platform", "") or queue_row.get("detected_platform", "")),
        "stop_state": str(submission.get("stop_state", "") or queue_row.get("stop_state", "")),
        "filled_fields": filled_fields,
        "missing_required_fields": missing_required,
        "detected_required_fields": detected_required_fields,
        "required_field_detail": required_field_detail,
        "review_notes": review_notes,
        "kpi_outcome": "usable_prepared" if status == "prepared_full" and not warnings else _status_class(status),
        "corporate_keyword": corporate_keyword,
        "corporate_context_source": corporate_source,
        "corporate_false_positive_risk": corporate_false_positive,
    }


def _markdown_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    widths = [max(len(str(row[idx])) for row in rows) for idx in range(len(rows[0]))]
    lines = []
    for ridx, row in enumerate(rows):
        lines.append("| " + " | ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(row)) + " |")
        if ridx == 0:
            lines.append("| " + " | ".join("-" * widths[idx] for idx in range(len(row))) + " |")
    return lines


def _write_kpi_csv(path: Path, analyses: list[dict[str, str]]) -> None:
    fields = [
        "lead_id",
        "domain",
        "final_status",
        "kpi_outcome",
        "reason",
        "lead_quality_issue",
        "contact_quality_issue",
        "form_quality_issue",
        "lead_finder_recommended_action",
        "lead_finder_score_penalty",
    ]
    _write_csv(path, analyses, fields)


def _write_report(
    path: Path,
    analyses: list[dict[str, str]],
    inputs: dict[str, Path | str],
    feedback_path: Path,
    kpi_path: Path,
    analysis_scope: str,
) -> None:
    status_counts = Counter(row["final_status"] for row in analyses)
    reason_counts = Counter(row["reason"] for row in analyses)
    lead_issue_counts = Counter(
        issue for row in analyses for issue in row["lead_quality_issue"].split(";") if issue
    )
    contact_issue_counts = Counter(
        issue for row in analyses for issue in row["contact_quality_issue"].split(";") if issue
    )
    form_issue_counts = Counter(
        issue for row in analyses for issue in row["form_quality_issue"].split(";") if issue
    )
    feedback_counts = Counter(
        item for row in analyses for item in row["feedback_for_lead_finder"].split(";") if item and item != "none"
    )
    lead_finder_action_counts = Counter(row["lead_finder_recommended_action"] for row in analyses)
    prepared_full = [row for row in analyses if row["final_status"] == "prepared_full"]
    prepared_full_warnings = [row for row in prepared_full if row["prepared_full_quality_warning"] == "yes"]
    useful_prepared_full = len(prepared_full) - len(prepared_full_warnings)
    manual_review_rows = [
        row
        for row in analyses
        if row["final_status"] in {"prepared_partial", "prepared_external", "prepared_review_needed"}
    ]
    skipped_rows = [row for row in analyses if row["final_status"].startswith("skipped")]

    lines = [
        f"# SEMI_AUTO Batch Analysis - {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "## Inputs",
        "",
    ]
    for label, input_path in inputs.items():
        lines.append(f"- {label}: {input_path}")
    lines.append(f"- analysis_scope: {analysis_scope}")
    lines.append(f"- lead_quality_feedback: {feedback_path}")
    lines.append(f"- kpi_summary: {kpi_path}")
    lines.extend(
        [
            "",
            "## Batch Result",
            "",
            f"- total_rows: {len(analyses)}",
            f"- sent_rows: {status_counts.get('sent', 0)}",
            f"- prepared_full: {status_counts.get('prepared_full', 0)}",
            f"- prepared_full_without_quality_warning: {useful_prepared_full}",
            f"- prepared_full_quality_warnings: {len(prepared_full_warnings)}",
            f"- prepared_partial: {status_counts.get('prepared_partial', 0)}",
            f"- prepared_review_needed: {status_counts.get('prepared_review_needed', 0)}",
            f"- manual_review_or_partial: {len(manual_review_rows)}",
            f"- skipped_bot_protection: {status_counts.get('skipped_bot_protection', 0)}",
            f"- skipped_total: {len(skipped_rows)}",
            "",
            "## Counts By Status",
            "",
        ]
    )
    for status, count in sorted(status_counts.items()):
        lines.append(f"- {status}: {count}")

    lines.extend(["", "## Counts By Reason", ""])
    for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {reason}: {count}")

    lines.extend(["", "## Quality Signals", ""])
    lines.append("### Lead Quality")
    for issue, count in sorted(lead_issue_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {issue}: {count}")
    lines.append("")
    lines.append("### Contact Quality")
    for issue, count in sorted(contact_issue_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {issue}: {count}")
    lines.append("")
    lines.append("### Form Quality")
    for issue, count in sorted(form_issue_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {issue}: {count}")

    lines.extend(["", "## Prepared Full Quality Warnings", ""])
    if prepared_full_warnings:
        for row in prepared_full_warnings:
            lines.append(f"- {row['lead_id']}: {row['quality_warning_detail']}")
    else:
        lines.append("- none")

    lines.extend(["", "## Prepared Full Leads", ""])
    if prepared_full:
        table = [["lead_id", "why_prepared_full", "filled_fields", "stop_state", "warning"]]
        for row in prepared_full:
            table.append(
                [
                    row["lead_id"],
                    "required fields filled and stopped before submit/confirm",
                    row.get("filled_fields", ""),
                    row.get("stop_state", ""),
                    row.get("quality_warning_detail", "") or "none",
                ]
            )
        lines.extend(_markdown_table(table))
    else:
        lines.append("- none")

    lines.extend(["", "## Prepared Review Needed / Partial Causes", ""])
    if manual_review_rows:
        table = [["lead_id", "status", "reason", "form_issue", "contact_issue", "notes"]]
        for row in manual_review_rows:
            table.append(
                [
                    row["lead_id"],
                    row["final_status"],
                    row["reason"],
                    row["form_quality_issue"],
                    row["contact_quality_issue"],
                    row.get("review_notes", ""),
                ]
            )
        lines.extend(_markdown_table(table))
    else:
        lines.append("- none")

    lines.extend(["", "## Skipped Causes", ""])
    if skipped_rows:
        table = [["lead_id", "status", "reason", "form_issue", "recommended_action"]]
        for row in skipped_rows:
            table.append(
                [
                    row["lead_id"],
                    row["final_status"],
                    row["reason"],
                    row["form_quality_issue"],
                    row["recommended_action"],
                ]
            )
        lines.extend(_markdown_table(table))
    else:
        lines.append("- none")

    lines.extend(["", "## Lead-Finder Feedback Signals", ""])
    if feedback_counts:
        for signal, count in sorted(feedback_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {signal}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Lead-Finder Recommended Actions", ""])
    for action, count in sorted(lead_finder_action_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {action}: {count}")

    lines.extend(["", "## Required Field Diagnostics", ""])
    required_rows = [row for row in analyses if "missing_required_fields" in row["form_quality_issue"]]
    if required_rows:
        for row in required_rows:
            detail = row["required_field_detail"] or row["missing_required_fields"]
            lines.append(f"- {row['lead_id']}: {detail}")
    else:
        lines.append("- none")

    lines.extend(["", "## Per-Lead Summary", ""])
    table = [
        [
            "lead_id",
            "status",
            "reason",
            "lead_issue",
            "contact_issue",
            "form_issue",
            "action",
            "lead_finder_action",
        ]
    ]
    for row in analyses:
        table.append(
            [
                row["lead_id"],
                row["final_status"],
                row["reason"],
                row["lead_quality_issue"],
                row["contact_quality_issue"],
                row["form_quality_issue"],
                row["recommended_action"],
                row["lead_finder_recommended_action"],
            ]
        )
    lines.extend(_markdown_table(table))

    lines.extend(
        [
            "",
            "## Recommended Next Patches",
            "",
            "- Use lead_quality_feedback with the review-batch runner to exclude bot-protected, iframe-only, corporate, and unsuitable contact-target rows.",
            "- Add local fixture coverage before changing any real form-fill behavior.",
            "- Feed `lead_quality_feedback` back into lead-finder scoring to down-rank media/listing domains, weak contact URLs, LINE/SNS-only contacts, and corporate-style leads.",
            "",
            "## Safety",
            "",
            "- Analysis used local artifacts only.",
            "- No browser run, website access, form submit, message send, FULL_AUTO run, commit, or push was performed by this tool.",
            "",
        ]
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze local SEMI_AUTO batch artifacts.")
    parser.add_argument("--date", default="", help="Batch date in YYYYMMDD format. Defaults to latest submissions CSV.")
    parser.add_argument("--handoff", default=str(DEFAULT_HANDOFF), help="Local handoff review batch CSV.")
    parser.add_argument("--results-dir", default=str(RESULTS_DIR), help="Results directory.")
    parser.add_argument("--screenshots-dir", default=str(SCREENSHOTS_DIR), help="Screenshots directory.")
    parser.add_argument("--output", default="", help="Analysis markdown output path.")
    parser.add_argument("--feedback-output", default="", help="Lead-quality feedback CSV output path.")
    parser.add_argument("--kpi-output", default="", help="Per-lead KPI CSV output path.")
    parser.add_argument(
        "--all-submissions",
        action="store_true",
        help="Analyze all rows in the daily submissions CSV instead of the latest row for each handoff lead.",
    )
    args = parser.parse_args()

    date_str = args.date or _latest_date()
    results_dir = Path(args.results_dir)
    screenshots_dir = Path(args.screenshots_dir) / date_str
    submissions_path = results_dir / f"submissions_{date_str}.csv"
    review_queue_path = results_dir / f"review_queue_{date_str}.csv"
    summary_path = results_dir / f"summary_{date_str}.md"
    batch_summary_path = _latest_batch_summary(results_dir, date_str)
    log_path = results_dir / "logs" / f"{date_str}.log"
    handoff_path = Path(args.handoff)
    output_path = Path(args.output) if args.output else results_dir / f"semi_auto_batch_analysis_{date_str}.md"
    feedback_path = (
        Path(args.feedback_output)
        if args.feedback_output
        else results_dir / f"lead_quality_feedback_{date_str}.csv"
    )
    kpi_path = Path(args.kpi_output) if args.kpi_output else results_dir / f"semi_auto_kpi_{date_str}.csv"

    submissions = _read_csv(submissions_path)
    if not submissions:
        print(f"ERROR no submissions rows found: {submissions_path}")
        return 1
    handoff_rows = _read_csv(handoff_path)
    analysis_scope = "all_daily_submissions"
    if not args.all_submissions and handoff_rows:
        submissions = _latest_submissions_for_handoff(submissions, handoff_rows)
        analysis_scope = "latest_submission_per_handoff_lead"
    queue_by_id = _index_queue(_read_csv(review_queue_path))
    handoff_by_id = _index_handoff(handoff_rows)
    logs_by_id = _split_log_by_lead(log_path)

    analyses = [
        _analyze_lead(
            submission=row,
            queue_row=queue_by_id.get(str(row.get("salon_id", "")).strip(), {}),
            handoff=handoff_by_id.get(str(row.get("salon_id", "")).strip(), {}),
            log_lines=logs_by_id.get(str(row.get("salon_id", "")).strip(), []),
            screenshot_dir=screenshots_dir,
        )
        for row in submissions
    ]

    feedback_fields = [
        "lead_id",
        "domain",
        "display_name",
        "final_status",
        "reason",
        "lead_quality_issue",
        "contact_quality_issue",
        "form_quality_issue",
        "recommended_action",
        "feedback_for_lead_finder",
        "lead_finder_recommended_action",
        "lead_finder_exclusion_reason",
        "lead_finder_score_penalty",
        "prepared_full_quality_warning",
        "quality_warning_detail",
        "screenshot_path",
        "contact_url",
        "final_step_url",
        "detected_platform",
        "stop_state",
        "filled_fields",
        "missing_required_fields",
        "detected_required_fields",
        "required_field_detail",
        "review_notes",
        "kpi_outcome",
        "corporate_keyword",
        "corporate_context_source",
        "corporate_false_positive_risk",
    ]
    _write_csv(feedback_path, analyses, feedback_fields)
    _write_kpi_csv(kpi_path, analyses)
    _write_report(
        output_path,
        analyses,
        inputs={
            "submissions": submissions_path,
            "review_queue": review_queue_path,
            "handoff": handoff_path,
            "daily_summary": summary_path,
            "batch_summary": batch_summary_path,
            "log": log_path,
            "screenshots": screenshots_dir,
        },
        feedback_path=feedback_path,
        kpi_path=kpi_path,
        analysis_scope=analysis_scope,
    )

    status_counts = Counter(row["final_status"] for row in analyses)
    warning_count = sum(1 for row in analyses if row["prepared_full_quality_warning"] == "yes")
    print(f"analysis_report={output_path}")
    print(f"lead_quality_feedback={feedback_path}")
    print(f"kpi_summary={kpi_path}")
    print(f"analysis_scope={analysis_scope}")
    print(f"rows={len(analyses)}")
    print(f"status_counts={dict(sorted(status_counts.items()))}")
    print(f"prepared_full_quality_warnings={warning_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
