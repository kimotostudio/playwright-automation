#!/usr/bin/env python3
"""Validate and optionally adapt demo-generator handoff CSVs for Playwright."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.blocklist import extract_domain


SALON_NAME_ALIASES = [
    "display_name",
    "表示名",
    "店名",
    "名称",
    "サロン名",
    "店舗名",
    "salon_name",
    "business_name",
    "brand_name",
    "company_name",
    "name",
]
OLD_URL_ALIASES = [
    "url(旧)",
    "url（旧）",
    "url旧",
    "url(old)",
    "URL",
    "old_url",
    "url",
    "website",
    "reference_url",
    "contact_url",
    "contact_page",
]
CONTACT_URL_ALIASES = ["contact_url", "contact_page", "original__contact_url", "original__form_url"]
WEBSITE_URL_ALIASES = [
    "url(旧)",
    "url（旧）",
    "url旧",
    "url(old)",
    "URL",
    "old_url",
    "url",
    "website",
    "reference_url",
    "original__url",
]
DEMO_URL_ALIASES = [
    "url(デモ)",
    "url(デモページ)",
    "url（デモページ）",
    "url（デモ）",
    "urlデモ",
    "demo_url",
    "url_demo",
    "demo_path",
    "demo",
]
LEAD_ID_ALIASES = ["id", "ID", "lead_id", "leadid", "管理番号"]
NON_WEB_SCHEMES = ("tel:", "mailto:", "line:", "sms:", "javascript:", "data:")


ADAPTED_FIELDNAMES = [
    "id",
    "display_name",
    "salon_name",
    "url",
    "contact_url",
    "demo_url",
    "domain",
    "url_status",
    "url_source",
    "demo_url_status",
    "name_confidence",
    "name_warning",
    "source_row",
]
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "config" / "message_template.txt"
DEFAULT_SUBJECT_TEMPLATE = "【ご確認】{display_name}様向けのWebデザイン案"


class SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _read_rows(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def _is_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://", str(value or "").strip(), flags=re.IGNORECASE))


def _norm_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("（", "(").replace("）", ")").replace("　", "")
    return normalized.replace(" ", "")


def _pick(row: dict, keys: list[str]) -> str:
    normalized = {_norm_key(k): v for k, v in row.items()}
    for key in keys:
        direct = row.get(key)
        if direct is not None and str(direct).strip():
            return str(direct).strip()
        alt = normalized.get(_norm_key(key))
        if alt is not None and str(alt).strip():
            return str(alt).strip()
    return ""


def normalize_web_url(value: str) -> str:
    target = str(value or "").strip()
    if not target:
        return ""
    lowered = target.lower()
    if lowered.startswith(NON_WEB_SCHEMES):
        return ""
    if re.match(r"^https?://", target, flags=re.IGNORECASE):
        return target
    if target.startswith("www."):
        return f"https://{target}"
    return ""


def resolve_target_url(row: dict) -> tuple[str, str, str, str]:
    contact_raw = _pick(row, CONTACT_URL_ALIASES)
    website_raw = _pick(row, WEBSITE_URL_ALIASES)
    contact_url = normalize_web_url(contact_raw)
    website_url = normalize_web_url(website_raw)

    if contact_url:
        return contact_url, contact_url, "", "contact_url"
    if website_url:
        return website_url, "", "", "website_fallback"
    if contact_raw or website_raw:
        return "", "", "invalid_url", "invalid"
    return "", "", "no_contact_url", "missing"


def resolve_demo_url(row: dict, demo_url_base: str = "") -> tuple[str, str]:
    raw_demo_url = _pick(row, DEMO_URL_ALIASES)
    if not raw_demo_url:
        return "", "missing"
    if _is_http_url(raw_demo_url):
        return raw_demo_url, "http"
    if demo_url_base:
        base = str(demo_url_base or "").strip()
        normalized_path = raw_demo_url.lstrip("/").lstrip("./")
        return urljoin(base.rstrip("/") + "/", normalized_path), "built_from_base"
    return raw_demo_url, "local_or_relative"


def _adapt_row(row: dict, row_number: int, demo_url_base: str = "") -> dict:
    url, contact_url, url_status, url_source = resolve_target_url(row)
    demo_url, demo_url_status = resolve_demo_url(row, demo_url_base)
    return {
        "id": _pick(row, LEAD_ID_ALIASES),
        "display_name": _pick(row, ["display_name", "表示名"]) or _pick(row, SALON_NAME_ALIASES),
        "salon_name": _pick(row, SALON_NAME_ALIASES),
        "url": url,
        "contact_url": contact_url,
        "demo_url": demo_url,
        "domain": _pick(row, ["domain", "original__domain"]) or extract_domain(url),
        "url_status": url_status,
        "url_source": url_source,
        "demo_url_status": demo_url_status,
        "name_confidence": _pick(row, ["name_confidence"]),
        "name_warning": _pick(row, ["name_warning"]),
        "source_row": str(row_number),
    }


def _write_adapted(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ADAPTED_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in ADAPTED_FIELDNAMES})


def _template_flags(path: Path) -> tuple[bool, bool, bool, str]:
    if not path.exists():
        return False, False, False, "missing"
    text = path.read_text(encoding="utf-8-sig")
    return "{salon_name}" in text, "{display_name}" in text, "{demo_url}" in text, "ok"


def _load_sender_info(path: str) -> dict[str, str]:
    if not path:
        return {}
    sender_path = Path(path)
    if not sender_path.exists():
        return {}
    try:
        data = json.loads(sender_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return {str(k): str(v or "") for k, v in data.items()}


def _render_message(template_path: Path, subject_template: str, row: dict, sender_info: dict[str, str]) -> tuple[str, str]:
    text = template_path.read_text(encoding="utf-8-sig")
    values = SafeFormatDict(
        {
            "display_name": row.get("display_name", "") or row.get("salon_name", ""),
            "salon_name": row.get("salon_name", "") or row.get("display_name", ""),
            "business_name": row.get("display_name", "") or row.get("salon_name", ""),
            "demo_url": row.get("demo_url", ""),
            "contact_url": row.get("contact_url", ""),
            "website": row.get("url", ""),
            "url": row.get("url", ""),
        }
    )
    for key, value in sender_info.items():
        values.setdefault(str(key), str(value or ""))
    subject = subject_template.format_map(values)
    body = text.format_map(values)
    return subject.strip(), body.strip()


def _row_safe_for_one_lead_semi_auto(row: dict, require_http_demo_url: bool = False) -> tuple[bool, str]:
    required = ["id", "display_name", "url", "demo_url", "domain"]
    missing = [field for field in required if not str(row.get(field, "")).strip()]
    if missing:
        return False, f"missing_{'_'.join(missing)}"
    if str(row.get("url_status", "")).strip() == "invalid_url":
        return False, "invalid_url"
    demo_url = str(row.get("demo_url", "")).strip()
    if require_http_demo_url and not _is_http_url(demo_url):
        return False, "demo_url_not_http"
    if str(row.get("url_source", "")) == "website_fallback":
        return True, "ready_with_website_fallback"
    return True, "ready"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preflight a normalized demo handoff CSV before SEMI_AUTO browser use."
    )
    parser.add_argument("--input", required=True, help="handoff_with_demo_paths.csv")
    parser.add_argument(
        "--write-adapted",
        help="Optional output path for a compact Playwright leads CSV.",
    )
    parser.add_argument(
        "--write-first-row",
        help="Optional output path for a one-row Playwright leads CSV for first manual SEMI_AUTO verification.",
    )
    parser.add_argument(
        "--require-http-demo-url",
        action="store_true",
        help="Fail unless every demo_url resolves to http(s), useful before real outreach.",
    )
    parser.add_argument(
        "--demo-url-base",
        help="Optional public base URL used to build demo_url from local/relative demo paths during adaptation.",
    )
    parser.add_argument(
        "--message-template",
        default=str(DEFAULT_TEMPLATE_PATH),
        help="Message template to inspect for {salon_name} and {demo_url} placeholders.",
    )
    parser.add_argument(
        "--require-demo-placeholder",
        action="store_true",
        help="Fail unless the inspected message template contains {demo_url}.",
    )
    parser.add_argument(
        "--require-display-placeholder",
        action="store_true",
        help="Fail unless the inspected message template contains {display_name} or {salon_name}.",
    )
    parser.add_argument(
        "--require-display-name",
        action="store_true",
        help="Fail unless every row has display_name/salon_name.",
    )
    parser.add_argument(
        "--sender-info",
        default=str(PROJECT_ROOT / "config" / "sender_info.json"),
        help="Optional gitignored sender_info.json used only for local message render checks.",
    )
    parser.add_argument(
        "--subject-template",
        default=DEFAULT_SUBJECT_TEMPLATE,
        help="Subject template used for local render checks.",
    )
    parser.add_argument(
        "--write-first-message",
        help="Optional local output text file for first-row rendered subject/body; does not open a browser.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR input_missing path={input_path}")
        return 2
    demo_url_base = str(args.demo_url_base or "").strip()
    if demo_url_base and not _is_http_url(demo_url_base):
        print(f"ERROR invalid_demo_url_base value={demo_url_base}")
        return 2

    raw_rows, headers = _read_rows(input_path)
    adapted_rows = [_adapt_row(row, idx, demo_url_base) for idx, row in enumerate(raw_rows, 1)]

    missing = Counter()
    duplicate_ids: set[str] = set()
    duplicate_domains: set[str] = set()
    seen_ids: set[str] = set()
    seen_domains: set[str] = set()
    http_demo_count = 0
    local_demo_count = 0
    demo_built_from_base_count = 0
    usable_url_count = 0
    missing_contact_url_count = 0
    website_fallback_count = 0
    invalid_url_count = 0
    ready_rows = 0
    uncertain_name_count = 0
    unknown_name_confidence_count = 0

    for row in adapted_rows:
        for field in ["id", "display_name", "salon_name", "url", "demo_url", "domain"]:
            if not str(row.get(field, "")).strip():
                missing[field] += 1
        lead_id = str(row.get("id", "")).strip()
        if lead_id:
            if lead_id in seen_ids:
                duplicate_ids.add(lead_id)
            seen_ids.add(lead_id)
        domain = str(row.get("domain", "")).strip().lower()
        if domain:
            if domain in seen_domains:
                duplicate_domains.add(domain)
            seen_domains.add(domain)
        if str(row.get("url", "")).strip():
            usable_url_count += 1
        if not str(row.get("contact_url", "")).strip():
            missing_contact_url_count += 1
        if str(row.get("url_source", "")).strip() == "website_fallback":
            website_fallback_count += 1
        if str(row.get("url_status", "")).strip() == "invalid_url":
            invalid_url_count += 1
        demo_url = str(row.get("demo_url", "")).strip()
        if _is_http_url(demo_url):
            http_demo_count += 1
        elif demo_url:
            local_demo_count += 1
        if str(row.get("demo_url_status", "")).strip() == "built_from_base":
            demo_built_from_base_count += 1
        name_confidence = str(row.get("name_confidence", "")).strip().lower()
        name_warning = str(row.get("name_warning", "")).strip()
        if not name_confidence:
            unknown_name_confidence_count += 1
        elif name_confidence in {"low", "unknown"} or name_warning:
            uncertain_name_count += 1
        if all(str(row.get(field, "")).strip() for field in ["id", "display_name", "url", "demo_url", "domain"]):
            ready_rows += 1

    print(f"input={input_path}")
    print(f"rows={len(raw_rows)}")
    print(f"headers={','.join(headers)}")
    print(f"usable_url_count={usable_url_count}")
    print(f"missing_contact_url_count={missing_contact_url_count}")
    print(f"website_fallback_rows={website_fallback_count}")
    print(f"invalid_url_count={invalid_url_count}")
    print(f"missing_display_name_count={missing.get('display_name', 0)}")
    print(f"missing_demo_url_count={missing.get('demo_url', 0)}")
    print(f"missing={dict(sorted(missing.items()))}")
    print(f"duplicate_ids={len(duplicate_ids)}")
    print(f"duplicate_domains={len(duplicate_domains)}")
    print(f"rows_ready_for_semi_auto={ready_rows}")
    print(f"demo_url_http={http_demo_count}")
    print(f"demo_url_local_or_relative={local_demo_count}")
    print(f"non_https_demo_url_count={local_demo_count}")
    print(f"demo_url_built_from_base={demo_built_from_base_count}")
    print(f"uncertain_name_count={uncertain_name_count}")
    print(f"unknown_name_confidence_count={unknown_name_confidence_count}")
    if uncertain_name_count or unknown_name_confidence_count:
        print("warning=display_name_review_recommended")
    if demo_url_base:
        print(f"demo_url_base={demo_url_base}")
    template_path = Path(args.message_template)
    has_name_placeholder, has_display_placeholder, has_demo_placeholder, template_status = _template_flags(template_path)
    sender_info_present = bool(args.sender_info and Path(args.sender_info).exists())
    sender_info = _load_sender_info(args.sender_info)
    first = adapted_rows[0] if adapted_rows else {}
    first_subject = ""
    first_body = ""
    render_error = ""
    if first:
        try:
            first_subject, first_body = _render_message(template_path, args.subject_template, first, sender_info)
            subject_render_status = "ok"
            message_render_status = "ok"
        except Exception as exc:
            subject_render_status = "error"
            message_render_status = "error"
            render_error = type(exc).__name__
    else:
        subject_render_status = "skipped"
        message_render_status = "skipped"

    print(f"message_template={template_path}")
    print(f"message_template_status={template_status}")
    print(f"template_has_salon_name_placeholder={has_name_placeholder}")
    print(f"template_has_display_name_placeholder={has_display_placeholder}")
    print(f"template_has_demo_url_placeholder={has_demo_placeholder}")
    print(f"sender_info_present={sender_info_present}")
    print(f"subject_render_status={subject_render_status}")
    print(f"message_render_status={message_render_status}")
    if render_error:
        print(f"message_render_error={render_error}")

    if adapted_rows:
        first_safe, first_safe_reason = _row_safe_for_one_lead_semi_auto(
            first,
            require_http_demo_url=bool(args.require_http_demo_url),
        )
        print(
            "first_row="
            f"id={first.get('id', '')},"
            f"domain={first.get('domain', '')},"
            f"selected_url={first.get('url', '')},"
            f"url_source={first.get('url_source', '')},"
            f"demo_url={first.get('demo_url', '')}"
        )
        print(f"first_row_safe_for_one_lead_semi_auto={first_safe}")
        print(f"first_row_safe_reason={first_safe_reason}")

    if args.write_adapted:
        output_path = Path(args.write_adapted)
        _write_adapted(output_path, adapted_rows)
        print(f"adapted_csv={output_path}")

    if args.write_first_row:
        output_path = Path(args.write_first_row)
        first_rows = adapted_rows[:1]
        _write_adapted(output_path, first_rows)
        first_id = first_rows[0]["id"] if first_rows else ""
        print(f"first_row_csv={output_path}")
        print(f"first_row_id={first_id}")

    if args.write_first_message:
        output_path = Path(args.write_first_message)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if message_render_status == "ok":
            output_path.write_text(
                "\n".join(
                    [
                        f"Lead-ID: {first.get('id', '')}",
                        f"Domain: {first.get('domain', '')}",
                        f"Selected URL: {first.get('url', '')}",
                        f"URL Source: {first.get('url_source', '')}",
                        f"Demo URL: {first.get('demo_url', '')}",
                        f"Subject: {first_subject}",
                        "",
                        first_body,
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            print(f"first_message={output_path}")
        else:
            print("first_message=skipped")

    if message_render_status == "error":
        print("status=not_ready")
        print("reason=message_render_error")
        return 1
    if missing.get("id") or missing.get("url") or missing.get("demo_url"):
        print("status=not_ready")
        return 1
    if args.require_display_name and missing.get("display_name"):
        print("status=not_ready")
        print("reason=display_name_missing")
        return 1
    if duplicate_ids:
        print("status=not_ready")
        return 1
    if args.require_http_demo_url and local_demo_count:
        print("status=not_ready")
        print("reason=demo_url_not_http")
        print("hint=Use --demo-url-base after deploying demos, or fill demo_url/url(デモ) with public http(s) URLs.")
        return 1
    if args.require_demo_placeholder and not has_demo_placeholder:
        print("status=not_ready")
        print("reason=message_template_missing_demo_url_placeholder")
        return 1
    if args.require_display_placeholder and not (has_display_placeholder or has_name_placeholder):
        print("status=not_ready")
        print("reason=message_template_missing_display_name_placeholder")
        return 1

    print("status=ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
