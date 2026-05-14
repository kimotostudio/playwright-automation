"""Main execution module for review-first browser automation."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
from datetime import datetime
from contextlib import suppress
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from playwright.async_api import Locator, Page, async_playwright
except ModuleNotFoundError:
    Locator = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]
    async_playwright = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.blocklist import block_domain, ensure_blocklist_files, extract_domain, is_blocked, seed_blocklist_domains_from_csv
try:
    from src.form_detector import FormDetector, detect_sales_prohibited_text
except ModuleNotFoundError:
    FormDetector = None

    def detect_sales_prohibited_text(_text: str) -> bool:
        return False

from src.ledger import append_ledger as _append_ledger, ledger_has, read_ledger
from src.message_generator import MessageGenerator
from src.rate_limiter import RateLimiter
from src.report_generator import JsonlHandler, generate_summary_report, print_report_from_files
from src.review_queue import append_review_entry, queue_path, read_queue
from src.safety import (
    DomainAttemptTracker,
    check_quiet_hours,
    check_robots_txt,
    count_required_fields,
    detect_bot_protection,
    detect_corporate,
    detect_hard_site,
)

JST = ZoneInfo("Asia/Tokyo")

CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
LOGS_DIR = os.path.join(RESULTS_DIR, "logs")
SCREENSHOTS_DIR = os.path.join(PROJECT_ROOT, "screenshots")

SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
SENDER_INFO_PATH = os.path.join(CONFIG_DIR, "sender_info.json")
TEMPLATE_PATH = os.path.join(CONFIG_DIR, "message_template.txt")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LEADS_PATH = os.path.join(DATA_DIR, "leads.csv")
LEDGER_PATH = os.path.join(DATA_DIR, "submission_ledger.csv")
SEMI_AUTO_REPORT_PATH = os.path.join(RESULTS_DIR, "semi_auto_report.csv")
DEFAULT_AIDNET_DOMAIN_LIST_PATH = os.path.join(DATA_DIR, "エイドネット_ドメインリスト - リスト_日本語学校.csv")

DEFAULT_SKIP_DOMAINS = [
    "hotpepper.jp",
    "beauty.hotpepper.jp",
    "ekiten.jp",
    "my-best.com",
]

DEFAULT_SKIP_URL_KEYWORDS = [
    "hotpepper",
    "ekiten",
    "my-best",
]

DEFAULT_CONTACT_LINK_TEXT_KEYWORDS = [
    "お問い合わせ",
    "contact",
    "form",
    "予約",
    "reserve",
    "booking",
    "inquiry",
    "ご相談",
    "申し込み",
    "entry",
]

EXTERNAL_FORM_HINTS = [
    "docs.google.com/forms",
    "form.run",
    "reserva",
    "select-type",
    "coubic",
    "tol-app",
    "stores.jp",
    "airreserve",
    "jotform",
    "typeform",
]

TRANSIENT_EXCEPTION_PATTERNS = [
    r"timeout",
    r"timed out",
    r"detached",
    r"execution context was destroyed",
    r"target closed",
    r"page crashed",
    r"net::err",
    r"connection reset",
]

PREPARED_STATUSES = {
    "prepared_full",
    "prepared_partial",
    "prepared_external",
    "prepared_review_needed",
}
SKIPPED_STATUSES = {
    "skipped_login",
    "skipped_bot_protection",
    "skipped_dead_site",
}

SALON_NAME_ALIASES = [
    "店名",
    "名称",
    "サロン名",
    "店舗名",
    "salon_name",
    "display_name",
    "business_name",
    "brand_name",
    "company_name",
    "name",
]
CONTACT_URL_ALIASES = [
    "contact_url",
    "contact_page",
    "original__contact_url",
    "original__form_url",
]
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
OLD_URL_ALIASES = WEBSITE_URL_ALIASES + CONTACT_URL_ALIASES
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
MESSAGE_ALIASES = ["message", "本文", "outreach_message"]
MESSAGE_PATH_ALIASES = ["message_path", "outreach_message_path"]
NON_WEB_SCHEMES = ("tel:", "mailto:", "line:", "sms:", "javascript:", "data:")


def is_prepared_status(status: str) -> bool:
    value = str(status or "").strip().lower()
    return value in PREPARED_STATUSES or value == "prepared"


def _infer_platform_from_urls(*urls: str) -> str:
    joined = " ".join([str(u or "").lower() for u in urls])
    if "jimdo" in joined:
        return "jimdo"
    if "wix" in joined:
        return "wix"
    if "peraichi" in joined:
        return "peraichi"
    if "amebaownd" in joined:
        return "ameba_ownd"
    if "google.com/forms" in joined or "docs.google.com/forms" in joined:
        return "google_forms"
    if "form.run" in joined:
        return "form_run"
    if "reserva" in joined:
        return "reserva"
    if "select-type" in joined:
        return "select_type"
    if "coubic" in joined:
        return "coubic"
    return "unknown"


def _normalize_status(
    *,
    status: str,
    reason: str,
    decision: str = "",
    missing_required_fields: Optional[list[str]] = None,
) -> str:
    value = str(status or "").strip().lower()
    reason_l = str(reason or "").strip().lower()
    decision_l = str(decision or "").strip().lower()
    missing_count = len(missing_required_fields or [])

    if value == "sent":
        return "sent"
    if value in PREPARED_STATUSES or value in SKIPPED_STATUSES:
        return value
    partial_tokens = ["requires_address", "missing_required", "unfilled_required", "fill_incomplete", "timeout_fill"]

    if value == "prepared":
        if "external_form" in reason_l:
            return "prepared_external"
        if missing_count > 0 or "needs_manual" in decision_l or "validation" in reason_l:
            return "prepared_partial"
        if decision_l == "prepared_ok":
            return "prepared_full"
        return "prepared_review_needed"

    login_tokens = ["login", "requires_login", "required_login", "password", "会員", "認証"]
    bot_tokens = ["bot_protection", "captcha", "cloudflare", "verify you are human", "access denied", "403", "429", "blocked_domain", "blocked_url", "domain_cooldown"]
    dead_tokens = ["dead_site", "name_not_resolved", "dns", "connection_refused", "ssl_error", "net::err_name_not_resolved"]

    if any(token in reason_l for token in login_tokens):
        return "skipped_login"
    if any(token in reason_l for token in bot_tokens):
        return "skipped_bot_protection"
    if any(token in reason_l for token in dead_tokens):
        return "skipped_dead_site"
    if any(token in reason_l for token in partial_tokens):
        return "prepared_partial"
    return "prepared_review_needed"


def enrich_result_for_outputs(result: dict) -> dict:
    out = dict(result)
    missing_required = []
    for item in (out.get("missing_required_fields", []) or []) + (out.get("any_missing_required_fields", []) or []):
        text = str(item).strip()
        if text and text not in missing_required:
            missing_required.append(text)

    normalized_status = _normalize_status(
        status=str(out.get("status", "")),
        reason=str(out.get("message", "")),
        decision=str(out.get("decision", "")),
        missing_required_fields=missing_required,
    )
    out["status"] = normalized_status

    if normalized_status in {"prepared_full", "sent"}:
        out["confidence_level"] = "high"
    elif normalized_status in {"prepared_partial", "prepared_external"}:
        out["confidence_level"] = "medium"
    else:
        out["confidence_level"] = "low"

    stop_state_value = str(out.get("stop_state", "")).strip().lower()
    if stop_state_value in {"confirmation", "submit_button", "form_filled", "unknown"}:
        out["stop_state"] = stop_state_value
    elif normalized_status == "sent":
        out["stop_state"] = "unknown"
    elif str(out.get("confirm_selector", "")).strip() and str(out.get("final_step_url", "")).strip():
        out["stop_state"] = "confirmation"
    elif str(out.get("submit_selector", "")).strip():
        out["stop_state"] = "submit_button"
    elif out.get("filled_fields"):
        out["stop_state"] = "form_filled"
    else:
        out["stop_state"] = "unknown"

    out["missing_required_fields"] = missing_required
    out["missing_required_fields_json"] = json.dumps(missing_required, ensure_ascii=False)
    out["detected_platform"] = _infer_platform_from_urls(
        str(out.get("url", "")),
        str(out.get("contact_url", "")),
        str(out.get("final_step_url", "")),
    )
    selector_map = out.get("field_selector_map", "")
    if isinstance(selector_map, dict):
        out["field_selector_map"] = json.dumps(selector_map, ensure_ascii=False)
    else:
        out["field_selector_map"] = str(selector_map or "").strip()
    out["form_root_selector"] = str(out.get("form_root_selector", "") or "").strip()
    notes = str(out.get("validation_notes", "")).strip() or str(out.get("message", "")).strip()
    out["notes"] = notes
    contact_url = str(out.get("contact_url", "")).strip()
    final_url = str(out.get("final_step_url", "")).strip()
    out["reopen_in_browser_url"] = final_url or contact_url
    return out


def append_ledger(entry: dict, path: str = LEDGER_PATH) -> dict:
    normalized = dict(entry)
    status = _normalize_status(
        status=str(normalized.get("status", "")),
        reason=str(normalized.get("reason", "")),
    )
    normalized["status"] = status
    return _append_ledger(normalized, path=path)


async def _highlight_submit_button(page: Page, submit_locator: Locator) -> bool:
    try:
        await submit_locator.wait_for(state="visible", timeout=5000)
        await submit_locator.scroll_into_view_if_needed(timeout=5000)
        await submit_locator.evaluate(
            """
            (el) => {
              const id = "kimoto-submit-highlight-style";
              if (!document.getElementById(id)) {
                const style = document.createElement("style");
                style.id = id;
                style.textContent = `
                  .kimoto-submit-highlight {
                    outline: 3px solid #ff5a1f !important;
                    outline-offset: 3px !important;
                    box-shadow: 0 0 0 4px rgba(255, 90, 31, 0.2) !important;
                  }
                  .kimoto-submit-highlight::after {
                    content: "ここをスタッフが最終確認して送信";
                    position: absolute;
                    top: -22px;
                    right: 0;
                    background: #ff5a1f;
                    color: #fff;
                    font-size: 11px;
                    padding: 2px 6px;
                    border-radius: 6px;
                    z-index: 2147483647;
                    pointer-events: none;
                  }
                `;
                document.head.appendChild(style);
              }
              el.classList.add("kimoto-submit-highlight");
              if (getComputedStyle(el).position === "static") {
                el.style.position = "relative";
              }
            }
            """
        )
        return True
    except Exception:
        return False


async def _detect_form_root_selector(page: Page, fields: Dict[str, Locator]) -> str:
    for _name, locator in fields.items():
        try:
            target = locator.first
            selector = await target.evaluate(
                """
                (el) => {
                  const form = el.closest('form');
                  if (!form) return '';
                  const byId = (form.getAttribute('id') || '').trim();
                  if (byId) return `form#${byId}`;
                  const byName = (form.getAttribute('name') || '').trim();
                  if (byName) return `form[name="${byName}"]`;
                  const byAction = (form.getAttribute('action') || '').trim();
                  if (byAction) return `form[action*="${byAction.slice(0, 80)}"]`;
                  const forms = Array.from(document.querySelectorAll('form'));
                  const idx = forms.indexOf(form);
                  if (idx >= 0) return `form:nth-of-type(${idx + 1})`;
                  return 'form';
                }
                """
            )
            selector_text = str(selector or "").strip()
            if selector_text:
                return selector_text
        except Exception:
            continue
    return ""


async def _wait_operator_pause(page: Page, settings: dict, lead_id: str, stop_state: str) -> None:
    debug_pause = _setting_bool(settings, "debug_pause", False)
    operator_pause = _setting_bool(settings, "operator_pause", True)
    if not operator_pause:
        return

    logger = logging.getLogger()
    if debug_pause:
        logger.info("[%s] operator pause (page.pause) stop_state=%s", lead_id, stop_state)
        try:
            await page.pause()
            return
        except Exception as e:
            logger.warning("[%s] page.pause unavailable, fallback to terminal pause: %s", lead_id, e)

    prompt = f"[{lead_id}] stop_state={stop_state}. 手動確認後、ENTERで次へ進みます: "
    if sys.stdin and sys.stdin.isatty():
        await asyncio.to_thread(input, prompt)
    else:
        logger.info("[%s] non-interactive stdin; keeping browser open for 60s fallback.", lead_id)
        await asyncio.sleep(60)


async def _wait_operator_session_end(settings: dict) -> None:
    logger = logging.getLogger()
    if not _setting_bool(settings, "operator_pause", True):
        return
    prompt = "SEMI_AUTO実行を終了してブラウザを閉じる場合は ENTER を押してください: "
    if sys.stdin and sys.stdin.isatty():
        await asyncio.to_thread(input, prompt)
    else:
        logger.info("SEMI_AUTO end: non-interactive stdin fallback wait 60s before close.")
        await asyncio.sleep(60)


def setup_logging(log_format: str = "text") -> logging.Logger:
    os.makedirs(LOGS_DIR, exist_ok=True)
    date_str = datetime.now(JST).strftime("%Y%m%d")

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    file_handler = logging.FileHandler(os.path.join(LOGS_DIR, f"{date_str}.log"), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    # Avoid Windows console UnicodeEncodeError from site-specific symbols.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console_handler)

    if log_format == "jsonl":
        logger.addHandler(JsonlHandler(os.path.join(LOGS_DIR, f"{date_str}.jsonl")))

    return logger


def load_settings() -> dict:
    with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _resolve_setting_path(path_value: str) -> str:
    target = str(path_value or "").strip()
    if not target:
        return ""
    if os.path.isabs(target):
        return target
    normalized = target.replace("/", os.sep).replace("\\", os.sep)
    return os.path.join(PROJECT_ROOT, normalized)


def _normalized_list(value: object, fallback: List[str]) -> List[str]:
    if isinstance(value, list):
        source = value
    else:
        source = fallback
    return [str(item).strip().lower() for item in source if str(item).strip()]


def _setting_bool(settings: dict, key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _setting_int(settings: dict, key: str, default: int) -> int:
    try:
        return int(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def get_pre_skip_reason(lead: dict, settings: dict, mode: str = "") -> str:
    base_url = str(lead.get("url", "")).strip()
    url_status = str(lead.get("url_status", "")).strip()
    salon_name = str(lead.get("salon_name", "")).strip()
    demo_url = str(lead.get("demo_url", "")).strip()
    if url_status in {"no_contact_url", "invalid_url"}:
        return url_status
    if not base_url:
        return "no_contact_url"
    if not _is_http_url(base_url):
        return "invalid_base_url"
    if not salon_name:
        return "missing_salon_name"
    mode_upper = str(mode or "").upper()
    # High-recall modes should not be blocked by missing demo URL.
    skip_demo_check = _setting_bool(settings, "skip_on_missing_demo_url", False)
    if mode_upper in {"DETECT_ONLY", "SEMI_AUTO"}:
        skip_demo_check = False
    if skip_demo_check and not demo_url:
        return "missing_demo_url"
    return ""


def should_skip_exception(exc_text: str, aggressive_skip: bool) -> bool:
    if not aggressive_skip:
        return False
    text = (exc_text or "").lower()
    if any(re.search(pattern, text) for pattern in TRANSIENT_EXCEPTION_PATTERNS):
        return True
    # In aggressive mode, unexpected automation errors are also skipped for throughput.
    return True


async def count_unfilled_required_fields(page: Page) -> int:
    try:
        return int(
            await page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('input, textarea, select'));
                  const isRequired = (el) => {
                    if (el.hasAttribute('required')) return true;
                    if ((el.getAttribute('aria-required') || '').toLowerCase() === 'true') return true;
                    const marker = (el.closest('label,td,th,div,p,li,dt,dd,tr')?.innerText || '');
                    return /必須|\\*|＊/.test(marker);
                  };
                  const isFilled = (el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    if (tag === 'select') return !!el.value;
                    if ((el.type || '').toLowerCase() === 'checkbox' || (el.type || '').toLowerCase() === 'radio') {
                      return !!el.checked;
                    }
                    return (el.value || '').trim().length > 0;
                  };
                  let missing = 0;
                  for (const el of nodes) {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    if (el.disabled) continue;
                    if (!isRequired(el)) continue;
                    if (!isFilled(el)) missing += 1;
                  }
                  return missing;
                }
                """
            )
        )
    except Exception:
        return 0


async def is_iframe_only_form(page: Page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                  const topFormCount = document.querySelectorAll('form, input, textarea, select').length;
                  const frameCount = document.querySelectorAll('iframe').length;
                  return topFormCount === 0 && frameCount > 0;
                }
                """
            )
        )
    except Exception:
        return False


def get_skip_reason(base_url: str, domain: str, settings: dict) -> Tuple[bool, str]:
    target_domain = (domain or extract_domain(base_url)).lower().strip()
    target_url = (base_url or "").lower().strip()
    hard_skip_portals = _setting_bool(settings, "hard_skip_portals", False)

    skip_domains = _normalized_list(settings.get("skip_domains"), DEFAULT_SKIP_DOMAINS)
    for skip_domain in skip_domains:
        if target_domain == skip_domain or target_domain.endswith(f".{skip_domain}"):
            # Prefer recall: keep exploration and tag as exclude candidate by default.
            return hard_skip_portals, f"exclude_clear:portal_domain:{skip_domain}"

    skip_keywords = _normalized_list(settings.get("skip_url_keywords"), DEFAULT_SKIP_URL_KEYWORDS)
    for keyword in skip_keywords:
        if keyword in target_url:
            # Prefer recall: keep exploration and tag as exclude candidate by default.
            return hard_skip_portals, f"exclude_clear:portal_url:{keyword}"

    return False, ""


def is_external_form_url(url: str) -> bool:
    target = (url or "").strip().lower()
    if not target:
        return False
    return any(hint in target for hint in EXTERNAL_FORM_HINTS)


def build_cli_overrides(args: argparse.Namespace) -> Dict[str, object]:
    overrides: Dict[str, object] = {}
    if args.dry_run:
        overrides["dry_run"] = True
        overrides["mode"] = "SEMI_AUTO"
    if args.test:
        overrides["test_mode"] = True
    if args.mode:
        overrides["mode"] = str(args.mode).upper()
    if args.semi_auto_verify:
        overrides["semi_auto_verify"] = True
        overrides["semi_auto_limit"] = int(args.semi_auto_limit)
        overrides["semi_auto_prompt"] = not args.no_prompt
    if args.limit is not None:
        overrides["daily_limit"] = int(args.limit)
    leads_path = args.leads or getattr(args, "input", None)
    if leads_path:
        overrides["leads_csv_path"] = str(leads_path)
    return overrides


def _norm_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("（", "(").replace("）", ")").replace("　", "")
    return normalized.replace(" ", "")


def _pick(row: dict, keys: List[str]) -> str:
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
        return contact_url, website_url or contact_url, contact_raw, ""
    if website_url:
        return website_url, website_url, contact_raw, ""
    if contact_raw or website_raw:
        return "", website_raw or contact_raw, contact_raw, "invalid_url"
    return "", "", "", "no_contact_url"


def _is_mock_mode(settings: dict) -> bool:
    return _setting_bool(settings, "mock_mode", False) or _setting_bool(settings, "test_mode", False)


def _looks_mock_salon_name(name: str) -> bool:
    value = str(name or "").strip().lower()
    return value.startswith("mock salon")


def _looks_mock_demo_url(url: str) -> bool:
    domain = extract_domain(str(url or ""))
    return domain == "example.com" or domain.endswith(".example.com")


def _is_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://", str(value or "").strip(), flags=re.IGNORECASE))


def _reason_ja(reason: str, status: str = "") -> str:
    reason_text = str(reason or "").strip()
    status_text = str(status or "").strip().lower()
    if status_text == "sent":
        return "送信完了"
    mapping = {
        "invalid_base_url": "URL不正",
        "invalid_url": "URL不正",
        "no_contact_url": "問い合わせURL未設定",
        "missing_base_url": "URL未設定",
        "missing_salon_name": "店名未設定",
        "missing_demo_url": "デモURL未設定",
        "no_contact_page": "問い合わせページなし",
        "no_form_found": "フォームなし",
        "no_form_fields": "フォームなし",
        "iframe_only_form": "フォームなし（iframeのみ）",
        "no_submit_button": "送信ボタンなし",
        "no_final_submit_button": "最終送信ボタンなし",
        "timeout_contact": "問い合わせ探索タイムアウト",
        "timeout_detect_form": "フォーム検出タイムアウト",
        "timeout_fill": "フォーム入力タイムアウト",
        "bot_protection": "ボット保護",
        "captcha": "ボット保護（CAPTCHA）",
        "requires_login": "ログイン必須",
        "dead_site": "サイト接続不可",
        "sales_prohibited": "営業禁止",
        "corporate_skipped": "法人/対象外",
        "unsupported_form": "未対応フォーム",
        "prepared_no_submit": "送信前停止",
        "manual_review_needed": "手動確認",
        "error": "エラー",
    }
    if reason_text in mapping:
        return mapping[reason_text]
    if reason_text.startswith("no_form"):
        return "フォームなし"
    if reason_text.startswith("unfilled_required_fields"):
        return "必須項目未入力"
    if reason_text.startswith("fill_incomplete"):
        return "入力不足"
    if reason_text.startswith("domain_attempt_limit"):
        return "同一ドメイン試行上限"
    if reason_text.startswith("blocked_domain"):
        return "ブロック対象ドメイン"
    if reason_text.startswith("domain_cooldown"):
        return "ドメインクールダウン中"
    return reason_text


def load_leads(path: str) -> List[dict]:
    leads: List[dict] = []
    if not os.path.exists(path):
        logging.error(f"[MAIN] leads file missing: {path}")
        return leads

    generated_id_count = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, 1):
            lead_id = _pick(row, LEAD_ID_ALIASES)
            if not lead_id:
                lead_id = f"aidnet-{idx:04d}"
                generated_id_count += 1
            target_url, original_url, contact_url, url_status = resolve_target_url(row)
            lead = {
                "id": lead_id,
                "salon_name": _pick(row, SALON_NAME_ALIASES + ["学校名", "school_name"]),
                "display_name": _pick(row, ["display_name", "表示名"]),
                "business_name": _pick(row, ["business_name", "brand_name"]),
                "company_name": _pick(row, ["company_name", "会社名", "法人名"]),
                "url": target_url,
                "website": original_url,
                "original_url": original_url,
                "contact_url": contact_url,
                "demo_url": _pick(row, DEMO_URL_ALIASES),
                "domain": _pick(row, ["domain", "original__domain"]) or extract_domain(target_url),
                "message": _pick(row, MESSAGE_ALIASES),
                "message_path": _pick(row, MESSAGE_PATH_ALIASES),
                "source_status": _pick(row, ["status"]),
                "source_reason": _pick(row, ["reason"]),
                "url_status": url_status,
            }
            if lead["id"] and lead["salon_name"]:
                leads.append(lead)
    logging.info(
        "[MAIN] loaded leads: %s (path=%s generated_ids=%s)",
        len(leads),
        path,
        generated_id_count,
    )
    return leads


def append_results(results: List[dict], date_str: str) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"submissions_{date_str}.csv")

    file_exists = os.path.exists(path)
    default_fields = [
        "timestamp",
        "salon_id",
        "salon_name",
        "url",
        "contact_url",
        "final_step_url",
        "demo_url",
        "status",
        "message",
        "reason_ja",
        "evidence",
        "confidence_level",
        "stop_state",
        "missing_required_fields",
        "detected_platform",
        "submit_selector",
        "confirm_selector",
        "reopen_in_browser_url",
        "form_root_selector",
        "field_selector_map",
        "notes",
    ]
    fieldnames = list(default_fields)
    needs_header_upgrade = False
    existing_rows: List[dict] = []
    if file_exists:
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, [])
                if header:
                    merged = list(header)
                    for col in default_fields:
                        if col not in merged:
                            merged.append(col)
                    fieldnames = merged
                    needs_header_upgrade = merged != list(header)
        except Exception:
            fieldnames = list(default_fields)
            needs_header_upgrade = False

    if file_exists and needs_header_upgrade:
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                existing_rows = list(csv.DictReader(f))
        except Exception:
            existing_rows = []

        for row in existing_rows:
            if not str(row.get("reason_ja", "")).strip():
                row["reason_ja"] = _reason_ja(str(row.get("message", "")), status=str(row.get("status", "")))

        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            if existing_rows:
                writer.writerows(existing_rows)

    mode = "a" if file_exists else "w"
    encoding = "utf-8" if file_exists else "utf-8-sig"

    with open(path, mode, encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    return path


def append_semi_auto_report(rows: List[dict]) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = SEMI_AUTO_REPORT_PATH
    fieldnames = ["id", "店名", "url_demo", "ok", "missing_fields", "notes", "screenshot_path"]
    file_exists = os.path.exists(path)
    mode = "a" if file_exists else "w"
    encoding = "utf-8" if file_exists else "utf-8-sig"
    with open(path, mode, encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)
    return path


def export_leads_prepared_view(leads_csv_path: str, date_str: str) -> str:
    """Export original leads with prepared/status columns for manual sales workflow."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    output_path = os.path.join(RESULTS_DIR, f"leads_prepared_{date_str}.csv")

    submission_path = os.path.join(RESULTS_DIR, f"submissions_{date_str}.csv")
    submission_by_id: Dict[str, dict] = {}
    if os.path.exists(submission_path):
        with open(submission_path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                salon_id = str(row.get("salon_id", "")).strip()
                if salon_id:
                    # keep latest status per salon_id
                    submission_by_id[salon_id] = row

    review_by_id: Dict[str, dict] = {}
    review_path = queue_path(date_str=date_str, results_dir=RESULTS_DIR)
    if os.path.exists(review_path):
        for row in read_queue(review_path):
            salon_id = str(row.get("salon_id", "")).strip()
            if salon_id:
                # keep latest queue row per salon_id
                review_by_id[salon_id] = row

    with open(leads_csv_path, "r", encoding="utf-8-sig", newline="") as src:
        reader = csv.DictReader(src)
        base_fieldnames = list(reader.fieldnames or [])
        extra_fields = [
            "prepared",
            "status",
            "reason",
            "manual_send_target",
            "contact_url",
            "final_step_url",
            "review_notes",
        ]
        fieldnames = base_fieldnames + [f for f in extra_fields if f not in base_fieldnames]

        rows_out: List[dict] = []
        for row in reader:
            salon_id = _pick(row, ["id", "ID"])
            submission = submission_by_id.get(salon_id, {})
            review = review_by_id.get(salon_id, {})

            # Prepared flag should follow today's latest submissions status for this ID.
            # review_queue can contain earlier/manual rows, so do not OR it here.
            is_prepared = is_prepared_status(str(submission.get("status", "")).strip().lower())
            status = str(submission.get("status", "")).strip() or str(review.get("status", "")).strip()
            reason = str(submission.get("message", "")).strip() or str(review.get("notes", "")).strip()

            row_out = dict(row)
            row_out["prepared"] = "1" if is_prepared else "0"
            row_out["status"] = status
            row_out["reason"] = reason
            row_out["manual_send_target"] = "1" if is_prepared else "0"
            row_out["contact_url"] = str(review.get("contact_url", "")).strip()
            row_out["final_step_url"] = str(review.get("final_step_url", "")).strip()
            row_out["review_notes"] = str(review.get("notes", "")).strip()
            rows_out.append(row_out)

    with open(output_path, "w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    return output_path


async def take_screenshot(page: Page, lead_id: str, seq: int, label: str, date_str: str) -> str:
    day_dir = os.path.join(SCREENSHOTS_DIR, date_str)
    os.makedirs(day_dir, exist_ok=True)
    filename = f"{lead_id}_{seq:02d}_{label}.png"
    path = os.path.join(day_dir, filename)
    await page.screenshot(path=path, full_page=True)
    return path


async def process_lead(
    page: Page,
    lead: dict,
    settings: dict,
    message_gen: MessageGenerator,
    domain_tracker: DomainAttemptTracker,
    mode: str,
    date_str: str,
) -> dict:
    logger = logging.getLogger()
    lead_id = str(lead.get("id", "")).strip()
    base_url = str(lead.get("url", "")).strip()
    resolved = message_gen.resolve_lead_fields(lead)
    display_name = str(resolved.get("display_name") or lead.get("display_name") or lead.get("salon_name", "")).strip()
    salon_name = str(resolved.get("salon_name") or display_name or lead.get("salon_name", "")).strip()
    demo_url = str(resolved.get("demo_url") or lead.get("demo_url", "")).strip()
    lead["salon_name"] = salon_name
    lead["display_name"] = display_name
    lead["demo_url"] = demo_url
    domain = extract_domain(base_url)
    logger.info("Lead resolved: id=%s, salon_name=%s, demo_url=%s", lead_id, salon_name, demo_url)
    aggressive_skip = _setting_bool(settings, "aggressive_skip", False)
    skip_if_new_tabs_or_downloads = _setting_bool(settings, "skip_if_new_tabs_or_downloads", True)
    skip_if_requires_login = _setting_bool(settings, "skip_if_requires_login", True)
    skip_if_iframe_only_form = _setting_bool(settings, "skip_if_iframe_only_form", False)
    skip_if_too_many_required_fields = _setting_int(settings, "skip_if_too_many_required_fields", 10)
    skip_if_unfilled_required_fields = _setting_bool(settings, "skip_if_unfilled_required_fields", True)
    skip_if_submit_not_found = _setting_bool(settings, "skip_if_submit_not_found", True)
    record_blocked_as_prepared = _setting_bool(settings, "record_blocked_as_prepared", False)
    max_contact_candidate_links = _setting_int(settings, "max_contact_candidate_links", 80)
    max_contact_pages_to_try = _setting_int(settings, "max_contact_pages_to_try", 8)
    max_contact_page_seconds = _setting_int(settings, "max_contact_page_seconds", 25)
    max_form_detect_seconds = _setting_int(settings, "max_form_detect_seconds", 20)
    max_fill_seconds = _setting_int(settings, "max_fill_seconds", 20)
    allow_querystring_urls = _setting_bool(settings, "allow_querystring_urls", True)
    contact_link_text_keywords = settings.get("contact_link_text_keywords", DEFAULT_CONTACT_LINK_TEXT_KEYWORDS)
    if not isinstance(contact_link_text_keywords, list):
        contact_link_text_keywords = list(DEFAULT_CONTACT_LINK_TEXT_KEYWORDS)

    result = {
        "timestamp": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "run_mode": mode,
        "salon_id": lead_id,
        "salon_name": salon_name,
        "url": base_url,
        "demo_url": demo_url,
        "domain": domain,
        "contact_url": "",
        "final_step_url": "",
        "submit_selector": "",
        "confirm_selector": "",
        "screenshot_folder": os.path.join("screenshots", date_str),
        "status": "pending",
        "message": "",
        "bot_protection": False,
        "blocked_domain": "",
        "has_store_name_in_message": False,
        "has_demo_url_in_message": False,
        "any_missing_required_fields": [],
        "validation_notes": "",
        "confirm_screenshot_path": "",
        "evidence": "",
        "detected_required_fields": [],
        "filled_fields": [],
        "missing_required_fields": [],
        "validation_errors": [],
        "decision": "prepared_needs_manual",
        "detect_only_meta": {},
        "pages_visited": 0,
        "candidate_contact_links_found": 0,
        "skipped_before_exploration": False,
        "stop_state": "unknown",
        "form_root_selector": "",
        "field_selector_map": "",
    }

    logger.info(f"[{lead_id}] start: {salon_name} ({base_url})")

    if not _is_mock_mode(settings) and (_looks_mock_salon_name(salon_name) or _looks_mock_demo_url(demo_url)):
        result["status"] = "prepared_review_needed"
        result["message"] = "mock_placeholder_detected"
        result["evidence"] = "resolved_lead_contains_mock_placeholder"
        result["skipped_before_exploration"] = True
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": "",
                "final_step_url": "",
                "status": "prepared_review_needed",
                "reason": "mock_placeholder_detected",
            },
            path=LEDGER_PATH,
        )
        return result

    pre_skip_reason = get_pre_skip_reason(lead, settings, mode=mode)
    if pre_skip_reason:
        result["status"] = "prepared_review_needed"
        result["message"] = pre_skip_reason
        result["evidence"] = f"pre_skip:{pre_skip_reason}"
        result["skipped_before_exploration"] = True
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": "",
                "final_step_url": "",
                "status": "prepared_review_needed",
                "reason": pre_skip_reason,
            },
            path=LEDGER_PATH,
        )
        return result

    if mode == "DETECT_ONLY" and is_external_form_url(base_url):
        result["status"] = "prepared"
        result["message"] = "external_form"
        result["contact_url"] = base_url
        result["final_step_url"] = base_url
        result["evidence"] = "external_form:base_url"
        result["decision"] = "prepared_ok"
        result["validation_notes"] = "external_form"
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": base_url,
                "final_step_url": base_url,
                "status": "prepared",
                "reason": "external_form",
            },
            path=LEDGER_PATH,
        )
        return result

    should_skip, skip_reason = get_skip_reason(base_url, domain, settings)
    if skip_reason:
        evidence_parts = [part for part in [result.get("evidence", ""), skip_reason] if part]
        result["evidence"] = "; ".join(evidence_parts)
        if not str(result.get("validation_notes", "")).strip():
            result["validation_notes"] = skip_reason
    if should_skip:
        result["status"] = "prepared_review_needed"
        result["message"] = skip_reason
        result["evidence"] = f"portal_detected:{skip_reason}"
        result["skipped_before_exploration"] = True
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": "",
                "final_step_url": "",
                "status": "prepared_review_needed",
                "reason": skip_reason,
            },
            path=LEDGER_PATH,
        )
        return result

    # pre-check: blocklist + cooldown — these are bot_protection related
    blocked, blocked_reason = is_blocked(domain, base_url, DATA_DIR)
    if blocked:
        result["status"] = "skipped"
        result["message"] = blocked_reason
        result["evidence"] = f"bot_protection_blocklist:{blocked_reason}"
        result["skipped_before_exploration"] = True
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": "",
                "final_step_url": "",
                "status": "skipped",
                "reason": blocked_reason,
            },
            path=LEDGER_PATH,
        )
        return result

    if not domain_tracker.can_attempt(domain):
        reason = f"domain_attempt_limit:{domain_tracker.get_count(domain)}/{domain_tracker.max_per_day}"
        result["status"] = "prepared_review_needed"
        result["message"] = reason
        result["evidence"] = f"domain_rate_limited:{reason}"
        result["skipped_before_exploration"] = True
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": "",
                "final_step_url": "",
                "status": "prepared_review_needed",
                "reason": reason,
            },
            path=LEDGER_PATH,
        )
        return result

    domain_tracker.record_attempt(domain)
    popup_state = {"triggered": False}

    def _popup_listener(_page) -> None:
        popup_state["triggered"] = True

    def _download_listener(_download) -> None:
        popup_state["triggered"] = True

    if skip_if_new_tabs_or_downloads:
        page.on("popup", _popup_listener)
        page.on("download", _download_listener)

    detector = FormDetector(page, message_gen.sender_info, timeout=settings.get("timeout_seconds", 30))

    try:
        if settings.get("respect_robots_and_terms", True):
            disallowed, reason = await check_robots_txt(page, base_url)
            if disallowed:
                result["status"] = "prepared_review_needed"
                result["message"] = reason
                result["evidence"] = f"robots_txt_disallowed:{reason}"
                result["skipped_before_exploration"] = True
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": "",
                        "final_step_url": "",
                        "status": "prepared_review_needed",
                        "reason": reason,
                    },
                    path=LEDGER_PATH,
                )
                return result

        try:
            contact_url = await asyncio.wait_for(
                detector.find_contact_page(
                    base_url,
                    max_internal_links=max_contact_candidate_links,
                    max_pages_to_try=max_contact_pages_to_try,
                    contact_link_text_keywords=contact_link_text_keywords,
                    allow_querystring_urls=allow_querystring_urls,
                ),
                timeout=max_contact_page_seconds,
            )
        except asyncio.TimeoutError:
            pages_visited = int(getattr(detector, "last_pages_visited", 0) or 0)
            candidates_found = int(getattr(detector, "last_candidate_contact_links_found", 0) or 0)
            result["status"] = "prepared_review_needed"
            result["message"] = "timeout_contact"
            result["pages_visited"] = pages_visited
            result["candidate_contact_links_found"] = candidates_found
            result["evidence"] = f"timeout_during_contact_exploration_after_{max_contact_page_seconds}s;pages_visited={pages_visited};candidates={candidates_found}"
            result["contact_url"] = base_url
            result["final_step_url"] = base_url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": base_url,
                    "final_step_url": base_url,
                    "status": "prepared_review_needed",
                    "reason": "timeout_contact",
                },
                path=LEDGER_PATH,
            )
            return result

        result["pages_visited"] = int(getattr(detector, "last_pages_visited", 0) or 0)
        result["candidate_contact_links_found"] = int(getattr(detector, "last_candidate_contact_links_found", 0) or 0)
        logger.info(
            "[%s] contact exploration: candidates=%s pages_visited=%s evidence=%s",
            lead_id,
            result["candidate_contact_links_found"],
            result["pages_visited"],
            detector.last_contact_evidence or "",
        )

        if not contact_url:
            candidates_found = result.get("candidate_contact_links_found", 0)
            result["status"] = "prepared_review_needed"
            result["message"] = "no_contact_page"
            result["evidence"] = f"no_obvious_contact_page_but_collected_{candidates_found}_candidate_links"
            result["contact_url"] = base_url
            result["final_step_url"] = base_url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": base_url,
                    "final_step_url": base_url,
                    "status": "prepared_review_needed",
                    "reason": "no_contact_page",
                },
                path=LEDGER_PATH,
            )
            return result

        if mode == "DETECT_ONLY" and is_external_form_url(contact_url):
            result["contact_url"] = contact_url
            result["final_step_url"] = contact_url
            result["status"] = "prepared"
            result["message"] = "external_form"
            result["evidence"] = f"{detector.last_contact_evidence or 'contact_candidate'}; external_form:contact_url"
            result["decision"] = "prepared_ok"
            result["validation_notes"] = "external_form"
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": contact_url,
                    "status": "prepared",
                    "reason": "external_form",
                },
                path=LEDGER_PATH,
            )
            return result

        result["contact_url"] = contact_url

        try:
            response = await asyncio.wait_for(
                page.goto(contact_url, timeout=settings.get("timeout_seconds", 30) * 1000, wait_until="domcontentloaded"),
                timeout=max_contact_page_seconds,
            )
        except asyncio.TimeoutError:
            result["status"] = "prepared_review_needed"
            result["message"] = "timeout_contact"
            result["evidence"] = f"timeout_loading_contact_page:{contact_url}"
            result["final_step_url"] = contact_url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": contact_url,
                    "status": "prepared_review_needed",
                    "reason": "timeout_contact",
                },
                path=LEDGER_PATH,
            )
            return result

        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=5000)

        status_code = response.status if response else None
        page_text = await page.inner_text("body")
        required_fields_count = await count_required_fields(page)

        if aggressive_skip:
            is_hard, hard_reason = detect_hard_site(
                page_text=page_text,
                status_code=status_code,
                popup_or_download=popup_state.get("triggered", False),
                required_fields_count=required_fields_count,
                required_field_threshold=skip_if_too_many_required_fields,
                skip_if_requires_login=skip_if_requires_login,
            )
            if is_hard:
                hard_reason_low = hard_reason.lower()
                is_login = any(t in hard_reason_low for t in ["requires_login", "login"])
                is_bot = any(t in hard_reason_low for t in ["bot_protection", "access_denied", "captcha"])
                is_dead = any(t in hard_reason_low for t in ["dead_site", "name_not_resolved", "connection_refused"])

                result["evidence"] = f"{detector.last_contact_evidence or ''}; hard_site={hard_reason}".strip("; ")
                result["final_step_url"] = page.url

                if is_bot:
                    block_domain(domain, days=7, reason="bot_protection", data_dir=DATA_DIR)
                    result["bot_protection"] = True
                    result["blocked_domain"] = domain

                if is_login or is_bot or is_dead:
                    result["status"] = "skipped"
                    result["message"] = hard_reason
                    append_ledger(
                        {
                            "run_mode": mode,
                            "salon_id": lead_id,
                            "salon_name": salon_name,
                            "domain": domain,
                            "contact_url": contact_url,
                            "final_step_url": result["final_step_url"],
                            "status": "skipped",
                            "reason": hard_reason,
                        },
                        path=LEDGER_PATH,
                    )
                    return result
                else:
                    # Not login/bot/dead → prepared_review_needed per CEO policy
                    result["status"] = "prepared_review_needed"
                    result["message"] = hard_reason
                    append_ledger(
                        {
                            "run_mode": mode,
                            "salon_id": lead_id,
                            "salon_name": salon_name,
                            "domain": domain,
                            "contact_url": contact_url,
                            "final_step_url": result["final_step_url"],
                            "status": "prepared_review_needed",
                            "reason": hard_reason,
                        },
                        path=LEDGER_PATH,
                    )
                    return result

        if detect_bot_protection(page_text, status_code):
            block_domain(domain, days=7, reason="bot_protection", data_dir=DATA_DIR)
            result["status"] = "skipped"
            result["message"] = "bot_protection"
            result["bot_protection"] = True
            result["blocked_domain"] = domain
            result["evidence"] = f"{detector.last_contact_evidence or ''}; bot_protection_detected".strip("; ")
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "skipped",
                    "reason": "bot_protection",
                },
                path=LEDGER_PATH,
            )
            return result

        if detect_sales_prohibited_text(page_text):
            result["status"] = "skipped"
            result["message"] = "sales_prohibited"
            result["evidence"] = f"{detector.last_contact_evidence or ''}; sales_prohibited_text".strip("; ")
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "skipped",
                    "reason": "sales_prohibited",
                },
                path=LEDGER_PATH,
            )
            return result

        if settings.get("business_only_filter", True):
            is_corp, corp_reason = detect_corporate(page_text)
            if is_corp:
                result["status"] = "prepared_review_needed"
                result["message"] = corp_reason
                result["evidence"] = f"business_filter_uncertain:{corp_reason}"
                result["final_step_url"] = page.url
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": contact_url,
                        "final_step_url": page.url,
                        "status": "prepared_review_needed",
                        "reason": corp_reason,
                    },
                    path=LEDGER_PATH,
                )
                return result

        if await detector.detect_captcha():
            block_domain(domain, days=7, reason="bot_protection", data_dir=DATA_DIR)
            result["status"] = "skipped"
            result["message"] = "bot_protection"
            result["bot_protection"] = True
            result["blocked_domain"] = domain
            result["evidence"] = f"{detector.last_contact_evidence or ''}; captcha_detected".strip("; ")
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "skipped",
                    "reason": "bot_protection",
                },
                path=LEDGER_PATH,
            )
            return result

        if mode == "DETECT_ONLY":
            seq = 0
            if settings.get("screenshot_enabled", True):
                seq += 1
                await take_screenshot(page, lead_id, seq, "contact_page", date_str)

            found, detect_reason, detect_meta = await detector.detect_form_presence()
            result["detect_only_meta"] = detect_meta
            result["final_step_url"] = page.url
            detect_evidence = str(detect_meta.get("evidence", "")).strip()
            result["evidence"] = "; ".join(
                [part for part in [detector.last_contact_evidence, detect_evidence] if part]
            )

            if found:
                result["status"] = "prepared"
                result["message"] = detect_reason or "form_detected"
                result["decision"] = "prepared_ok"
                result["validation_notes"] = result["message"]
                if settings.get("screenshot_enabled", True):
                    seq += 1
                    await take_screenshot(page, lead_id, seq, "form_detected", date_str)
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": contact_url,
                        "final_step_url": result["final_step_url"],
                        "status": "prepared",
                        "reason": result["message"],
                    },
                    path=LEDGER_PATH,
                )
                return result

            actual_reason = detect_reason or "no_form_found"
            if detect_reason == "login_form":
                result["status"] = "skipped"
                result["message"] = "login_form"
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": contact_url,
                        "final_step_url": result["final_step_url"],
                        "status": "skipped",
                        "reason": "login_form",
                    },
                    path=LEDGER_PATH,
                )
                return result

            # Everything else → prepared_review_needed per CEO policy
            candidates_found = result.get("candidate_contact_links_found", 0)
            result["status"] = "prepared_review_needed"
            result["message"] = actual_reason
            if not result.get("evidence"):
                result["evidence"] = f"no_form_found_but_collected_{candidates_found}_contact_candidates"
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": result["final_step_url"],
                    "status": "prepared_review_needed",
                    "reason": actual_reason,
                },
                path=LEDGER_PATH,
            )
            return result

        try:
            fields, form_map = await asyncio.wait_for(detector.detect_form_fields(), timeout=max_form_detect_seconds)
        except asyncio.TimeoutError:
            result["status"] = "prepared_review_needed"
            result["message"] = "timeout_detect_form"
            result["evidence"] = f"timeout_during_form_detection_after_{max_form_detect_seconds}s"
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "prepared_review_needed",
                    "reason": "timeout_detect_form",
                },
                path=LEDGER_PATH,
            )
            return result

        result["field_selector_map"] = json.dumps(form_map, ensure_ascii=False)
        result["form_root_selector"] = await _detect_form_root_selector(page, fields)

        if not fields:
            if await is_iframe_only_form(page):
                no_field_reason = "iframe_only_form"
                evidence = "iframe_form_detected_needs_review"
            else:
                no_field_reason = "no_form_fields"
                evidence = "no_form_found_but_collected_contact_candidates"
            result["status"] = "prepared_review_needed"
            result["message"] = no_field_reason
            result["evidence"] = evidence
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "prepared_review_needed",
                    "reason": no_field_reason,
                },
                path=LEDGER_PATH,
            )
            return result

        logger.info(f"[{lead_id}] form_map={json.dumps(form_map, ensure_ascii=False)}")

        required_inspection = await detector.inspect_required_fields()
        result["detected_required_fields"] = list(required_inspection.get("detected_required_fields", []))

        seq = 0
        if settings.get("screenshot_enabled", True):
            seq += 1
            await take_screenshot(page, lead_id, seq, "before_fill", date_str)

        if required_inspection.get("address_required_fields"):
            addr_fields = list(required_inspection.get("address_required_fields", []))
            result["status"] = "prepared_review_needed"
            result["message"] = "requires_address"
            result["missing_required_fields"] = addr_fields
            result["evidence"] = f"address_fields_detected_needs_manual_completion:{','.join(addr_fields)}"
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "prepared_review_needed",
                    "reason": "requires_address",
                },
                path=LEDGER_PATH,
            )
            return result

        message = message_gen.generate(
            salon_name,
            demo_url,
            display_name=display_name,
            business_name=lead.get("business_name", ""),
            company_name=lead.get("company_name", ""),
            contact_url=contact_url,
            website=lead.get("website", "") or lead.get("original_url", ""),
            old_url=lead.get("original_url", ""),
            url=lead.get("url", ""),
        )
        message = message_gen.sanitize_message_for_legacy_encodings(message)
        subject = message_gen.generate_subject(
            salon_name,
            display_name=display_name,
            business_name=lead.get("business_name", ""),
            company_name=lead.get("company_name", ""),
        )
        try:
            fill_ok, fill_stats = await asyncio.wait_for(
                detector.fill_form(fields, message, subject),
                timeout=max_fill_seconds,
            )
        except asyncio.TimeoutError:
            result["status"] = "prepared_review_needed"
            result["message"] = "timeout_fill"
            result["evidence"] = f"timeout_during_fill_attempt_after_{max_fill_seconds}s"
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "prepared_review_needed",
                    "reason": "timeout_fill",
                },
                path=LEDGER_PATH,
            )
            return result

        if not fill_ok:
            fill_reason = f"fill_incomplete:{fill_stats['filled']}/{fill_stats['total_fields']}"
            result["status"] = "prepared_review_needed"
            result["message"] = fill_reason
            result["evidence"] = f"required_fields_partially_filled:{fill_reason}"
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "prepared_review_needed",
                    "reason": fill_reason,
                },
                path=LEDGER_PATH,
            )
            return result

        date_filled = await detector.fill_required_dates()
        checked_boxes = await detector.handle_checkboxes()
        selected_dropdowns = await detector.handle_dropdowns(required_only=True)
        result["filled_fields"] = list(fill_stats.get("filled_fields", [])) + date_filled + checked_boxes + selected_dropdowns

        validation = await detector.validate_form_without_submit()
        validation_errors = list(validation.get("validation_errors", []))
        fill_missing = list(fill_stats.get("missing_required_fields", []))
        invalid_missing = list(validation.get("missing_required_fields", []))
        merged_missing = []
        seen_missing = set()
        for item in fill_missing + invalid_missing:
            key = str(item).strip()
            if not key or key in seen_missing:
                continue
            seen_missing.add(key)
            merged_missing.append(key)
        result["missing_required_fields"] = merged_missing
        result["validation_errors"] = validation_errors

        if skip_if_new_tabs_or_downloads and popup_state.get("triggered", False):
            result["status"] = "prepared_review_needed"
            result["message"] = "popup_or_download"
            result["evidence"] = "popup_or_download_detected_needs_review"
            result["final_step_url"] = page.url
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "prepared_review_needed",
                    "reason": "popup_or_download",
                },
                path=LEDGER_PATH,
            )
            return result

        if skip_if_unfilled_required_fields:
            missing_required_count = await count_unfilled_required_fields(page)
            if missing_required_count > 0:
                unfilled_reason = f"unfilled_required_fields:{missing_required_count}"
                result["status"] = "prepared_review_needed"
                result["message"] = unfilled_reason
                result["evidence"] = f"required_fields_unfilled_needs_review:{missing_required_count}_fields"
                result["missing_required_fields"] = result.get("missing_required_fields", []) + [unfilled_reason]
                result["final_step_url"] = page.url
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": contact_url,
                        "final_step_url": page.url,
                        "status": "prepared_review_needed",
                        "reason": unfilled_reason,
                    },
                    path=LEDGER_PATH,
                )
                return result

        if settings.get("screenshot_enabled", True):
            seq += 1
            await take_screenshot(page, lead_id, seq, "after_fill", date_str)

        submit_btn, submit_selector, is_confirm_step = await detector.find_submit_button()
        if not submit_btn:
            if mode == "SEMI_AUTO":
                result["status"] = "prepared_review_needed"
                result["message"] = "no_submit_button"
                result["decision"] = "prepared_needs_manual"
                result["stop_state"] = "form_filled" if result.get("filled_fields") else "unknown"
                result["final_step_url"] = page.url
                if settings.get("screenshot_enabled", True):
                    seq += 1
                    await take_screenshot(page, lead_id, seq, "before_submit_or_confirm", date_str)
                await _wait_operator_pause(page, settings, lead_id, result["stop_state"])
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": contact_url,
                        "final_step_url": result["final_step_url"],
                        "status": result["status"],
                        "reason": "no_submit_button",
                    },
                    path=LEDGER_PATH,
                )
                return result

            result["status"] = "prepared_review_needed"
            result["message"] = "no_submit_button"
            result["evidence"] = "form_filled_but_no_submit_button_found"
            result["final_step_url"] = page.url
            result["stop_state"] = "form_filled" if result.get("filled_fields") else "unknown"
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": page.url,
                    "status": "prepared_review_needed",
                    "reason": "no_submit_button",
                },
                path=LEDGER_PATH,
            )
            return result

        if is_confirm_step:
            result["confirm_selector"] = submit_selector
        else:
            result["submit_selector"] = submit_selector

        result["final_step_url"] = page.url
        with suppress(Exception):
            await _highlight_submit_button(page, submit_btn)

        if settings.get("screenshot_enabled", True):
            seq += 1
            await take_screenshot(page, lead_id, seq, "before_submit_or_confirm", date_str)

        # SEMI_AUTO: never final submit
        if mode == "SEMI_AUTO" or settings.get("dry_run", False):
            confirm_shot = ""
            allow_confirm_click = _setting_bool(settings, "semi_auto_allow_confirm_click", False)
            if is_confirm_step and allow_confirm_click:
                await submit_btn.click()
                with suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)

                final_btn, final_selector, _ = await detector.find_submit_button()
                result["final_step_url"] = page.url
                if final_btn:
                    result["submit_selector"] = final_selector
                    with suppress(Exception):
                        await _highlight_submit_button(page, final_btn)
                result["stop_state"] = "confirmation"

                seq += 1
                confirm_shot = await take_screenshot(page, lead_id, seq, "on_confirmation_page", date_str)
                # simple alias required by semi-auto verification flow
                os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
                alias_path = os.path.join(SCREENSHOTS_DIR, f"{lead_id}_confirm.png")
                await page.screenshot(path=alias_path, full_page=True)
                confirm_shot = alias_path
                logger.info(f"[{lead_id}] SEMI_AUTO: stopped on confirmation page")
            else:
                result["stop_state"] = "submit_button"
                os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
                confirm_shot = os.path.join(SCREENSHOTS_DIR, f"{lead_id}_confirm.png")
                await page.screenshot(path=confirm_shot, full_page=True)
                if is_confirm_step:
                    result["confirm_selector"] = submit_selector
                    logger.info(
                        "[%s] SEMI_AUTO: confirmation-like button found but not clicked; stopped before any submit-like click",
                        lead_id,
                    )
                else:
                    logger.info(f"[{lead_id}] SEMI_AUTO: stopped before submit")

            confirm_text = await page.inner_text("body")
            failed_fields = []
            for field_name, status_text in fill_stats.get("field_details", {}).items():
                if str(status_text).startswith("failed:"):
                    failed_fields.append(field_name)

            missing_required_fields = list(failed_fields)
            if "email" not in fields:
                missing_required_fields.append("email_field_missing")
            if "message" not in fields:
                missing_required_fields.append("message_field_missing")
            if "name" not in fields and not ("name_sei" in fields and "name_mei" in fields):
                missing_required_fields.append("name_field_missing")
            # keep stable order, deduplicated
            seen = set()
            missing_required_fields = [x for x in missing_required_fields if not (x in seen or seen.add(x))]

            has_store_name = salon_name in confirm_text if salon_name else False
            has_demo_url = demo_url in confirm_text if demo_url else False
            notes = []
            if not has_store_name:
                notes.append("store_name_not_found_on_confirm")
            if not has_demo_url:
                notes.append("demo_url_not_found_on_confirm")
            if missing_required_fields:
                notes.append("required_fields_missing_or_failed")
            if result.get("validation_errors"):
                notes.append("validation_errors_present")

            result["status"] = "prepared"
            result["message"] = "prepared"
            result["has_store_name_in_message"] = has_store_name
            result["has_demo_url_in_message"] = has_demo_url
            result["any_missing_required_fields"] = missing_required_fields + result.get("missing_required_fields", [])
            result["validation_notes"] = ";".join(notes) if notes else "ok"
            result["confirm_screenshot_path"] = confirm_shot
            decision = "prepared_ok"
            if result["any_missing_required_fields"] or result.get("validation_errors"):
                decision = "prepared_needs_manual"
            result["decision"] = decision
            if result.get("stop_state") not in {"confirmation", "submit_button"}:
                result["stop_state"] = "form_filled"
            await _wait_operator_pause(page, settings, lead_id, result["stop_state"])
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": result["final_step_url"],
                    "status": "prepared",
                    "reason": "semi_auto_prepared",
                },
                path=LEDGER_PATH,
            )
            return result

        # FULL_AUTO
        if is_confirm_step:
            await submit_btn.click()
            with suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=5000)

            result["final_step_url"] = page.url
            if settings.get("screenshot_enabled", True):
                seq += 1
                await take_screenshot(page, lead_id, seq, "on_confirmation_page", date_str)

            submit_btn, submit_selector, _ = await detector.find_submit_button()
            if not submit_btn:
                result["status"] = "failed"
                result["message"] = "no_final_submit_button"
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": contact_url,
                        "final_step_url": result["final_step_url"],
                        "status": "failed",
                        "reason": "no_final_submit_button",
                    },
                    path=LEDGER_PATH,
                )
                return result
            result["submit_selector"] = submit_selector

        await submit_btn.click()
        with suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        await detector.handle_confirmation_page()
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=5000)

        if settings.get("screenshot_enabled", True):
            seq += 1
            await take_screenshot(page, lead_id, seq, "after_submit", date_str)

        result["status"] = "sent"
        result["message"] = "sent"
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": contact_url,
                "final_step_url": page.url,
                "status": "sent",
                "reason": "full_auto_submit",
            },
            path=LEDGER_PATH,
        )
        return result

    except Exception as e:
        exc_short = str(e)[:160]
        exc_lower = exc_short.lower()
        # Classify exceptions: dead_site for DNS/connection errors, prepared_review_needed for everything else
        if any(t in exc_lower for t in ["name_not_resolved", "dns", "connection_refused", "ssl_error", "net::err"]):
            result["status"] = "skipped"
            result["message"] = f"dead_site:{exc_short}"
        else:
            result["status"] = "prepared_review_needed"
            result["message"] = f"exception:{exc_short}"
        result["evidence"] = f"exception_during_processing:{exc_short}"
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": result.get("contact_url", ""),
                "final_step_url": result.get("final_step_url", ""),
                "status": result["status"],
                "reason": result["message"],
            },
            path=LEDGER_PATH,
        )
        return result
    finally:
        if skip_if_new_tabs_or_downloads:
            try:
                page.remove_listener("popup", _popup_listener)
            except Exception:
                pass
            try:
                page.remove_listener("download", _download_listener)
            except Exception:
                pass


async def run(settings_override: Optional[dict] = None) -> Dict:
    settings = load_settings()
    if settings_override:
        settings.update(settings_override)

    logger = setup_logging(log_format=settings.get("log_format", "text"))
    ensure_blocklist_files(DATA_DIR)
    aidnet_path_raw = settings.get("aidnet_domain_list_path", DEFAULT_AIDNET_DOMAIN_LIST_PATH)
    aidnet_path = _resolve_setting_path(str(aidnet_path_raw))
    seed_stats = seed_blocklist_domains_from_csv(aidnet_path, data_dir=DATA_DIR)
    if seed_stats.get("status") == "ok":
        logger.info(
            "[MAIN] aidnet domain list synced: added=%s valid_domains=%s invalid_url_rows=%s csv=%s",
            seed_stats.get("added_count", 0),
            seed_stats.get("valid_domain_count", 0),
            seed_stats.get("invalid_url_rows", 0),
            seed_stats.get("csv_path", aidnet_path),
        )
    elif seed_stats.get("status") != "file_missing":
        logger.warning(
            "[MAIN] aidnet domain list sync skipped: status=%s csv=%s",
            seed_stats.get("status"),
            seed_stats.get("csv_path", aidnet_path),
        )

    now = datetime.now(JST)
    date_str = now.strftime("%Y%m%d")
    mode = str(settings.get("mode", "SEMI_AUTO")).upper()
    if mode not in {"SEMI_AUTO", "FULL_AUTO", "DETECT_ONLY"}:
        mode = "SEMI_AUTO"

    logger.info("=" * 60)
    logger.info("Playwright Automation")
    logger.info(f"Date: {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    logger.info(f"Mode: {mode}")
    logger.info("=" * 60)

    if settings.get("stop", False):
        logger.info("STOP switch is ON. Exit.")
        return {"total": 0, "sent": 0, "prepared": 0, "failed": 0, "skipped": 0, "stopped": True}

    quiet, quiet_msg = check_quiet_hours(settings.get("quiet_hours_start", 22), settings.get("quiet_hours_end", 8))
    if quiet:
        logger.info(f"quiet hours: {quiet_msg}")
        return {"total": 0, "sent": 0, "prepared": 0, "failed": 0, "skipped": 0, "quiet_hours": True}

    daily_limit = int(settings.get("test_limit", 2) if settings.get("test_mode", False) else settings.get("daily_limit", 10))

    ledger_data = read_ledger(LEDGER_PATH)
    rate_limiter = RateLimiter(STATE_PATH, daily_limit=daily_limit, ledger_ids=ledger_data.get("sent_ids", set()))
    limiter_stats = rate_limiter.get_stats()

    if not rate_limiter.can_submit():
        logger.info(f"daily sent limit reached: {limiter_stats['today_count']}/{daily_limit}")
        return {"total": 0, "sent": 0, "prepared": 0, "failed": 0, "skipped": 0, "limit_reached": True}

    leads_path = settings.get("leads_csv_path", LEADS_PATH)
    leads = load_leads(leads_path)
    if not leads:
        return {"total": 0, "sent": 0, "prepared": 0, "failed": 0, "skipped": 0}

    unprocessed = [lead for lead in leads if not rate_limiter.is_completed(lead["id"]) and not ledger_has(lead["id"], LEDGER_PATH)]
    if not unprocessed:
        logger.info("no unprocessed leads")
        return {"total": 0, "sent": 0, "prepared": 0, "failed": 0, "skipped": 0}

    semi_auto_verify = bool(settings.get("semi_auto_verify", False))
    semi_auto_limit = int(settings.get("semi_auto_limit", 3))
    semi_auto_prompt = bool(settings.get("semi_auto_prompt", True))

    if mode == "SEMI_AUTO" and semi_auto_verify:
        to_process = unprocessed[: min(rate_limiter.remaining(), max(1, semi_auto_limit))]
    else:
        to_process = unprocessed[: rate_limiter.remaining()]
    domain_tracker = DomainAttemptTracker(max_per_day=int(settings.get("max_attempts_per_domain_per_day", 2)))
    message_gen = MessageGenerator(
        TEMPLATE_PATH,
        SENDER_INFO_PATH,
        wrap_message=bool(settings.get("wrap_message", True)),
        wrap_width=int(settings.get("wrap_width", 56)),
        debug=bool(settings.get("debug", False)),
    )

    results: List[dict] = []
    stats = {
        "total": 0,
        "sent": 0,
        "prepared": 0,
        "failed": 0,
        "skipped": 0,
        "pages_visited": 0,
        "candidate_contact_links_found": 0,
        "skipped_before_exploration": 0,
    }
    reason_counts: Dict[str, int] = {}
    blocked_domains: List[str] = []
    consecutive_failures = 0

    headless_default = False if mode in {"SEMI_AUTO", "DETECT_ONLY"} else True
    headless = bool(settings.get("headless", headless_default))

    existing_prepared_ids = set()
    if mode == "SEMI_AUTO":
        today_queue = queue_path(date_str=date_str, results_dir=RESULTS_DIR)
        for row in read_queue(today_queue):
            rid = str(row.get("salon_id", "")).strip()
            if rid:
                existing_prepared_ids.add(rid)

    if async_playwright is None:
        raise RuntimeError("Playwright is not installed. Run `pip install -r requirements.txt` before browser automation.")
    if FormDetector is None:
        raise RuntimeError("Playwright form detection dependencies are not installed. Run `pip install -r requirements.txt`.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={
                "width": settings["browser"]["viewport_width"],
                "height": settings["browser"]["viewport_height"],
            },
            user_agent=settings["browser"]["user_agent"],
            locale=settings["browser"]["locale"],
        )
        page = await context.new_page()

        for idx, lead in enumerate(to_process, 1):
            if not rate_limiter.can_submit():
                logger.info("daily sent limit reached during run")
                break

            lead_id = lead["id"]
            # hard duplicate check before any attempt
            if rate_limiter.is_completed(lead_id) or ledger_has(lead_id, LEDGER_PATH):
                logger.info(f"[{lead_id}] skipped duplicate (state+ledger check)")
                continue
            if mode == "SEMI_AUTO" and lead_id in existing_prepared_ids:
                logger.info(f"[{lead_id}] skipped (already prepared in today's review_queue)")
                continue

            stats["total"] += 1
            raw_result = await process_lead(
                page=page,
                lead=lead,
                settings=settings,
                message_gen=message_gen,
                domain_tracker=domain_tracker,
                mode=mode,
                date_str=date_str,
            )
            result = enrich_result_for_outputs(raw_result)

            results.append(
                {
                    "timestamp": result["timestamp"],
                    "salon_id": result["salon_id"],
                    "salon_name": result["salon_name"],
                    "url": result["url"],
                    "contact_url": result.get("contact_url", ""),
                    "final_step_url": result.get("final_step_url", ""),
                    "demo_url": result["demo_url"],
                    "status": result["status"],
                    "message": result["message"],
                    "reason_ja": _reason_ja(result.get("message", ""), status=result.get("status", "")),
                    "evidence": result.get("evidence", ""),
                    "confidence_level": result.get("confidence_level", ""),
                    "stop_state": result.get("stop_state", ""),
                    "missing_required_fields": result.get("missing_required_fields_json", "[]"),
                    "detected_platform": result.get("detected_platform", ""),
                    "submit_selector": result.get("submit_selector", ""),
                    "confirm_selector": result.get("confirm_selector", ""),
                    "reopen_in_browser_url": result.get("reopen_in_browser_url", ""),
                    "form_root_selector": result.get("form_root_selector", ""),
                    "field_selector_map": result.get("field_selector_map", ""),
                    "notes": result.get("notes", ""),
                }
            )

            status = result["status"]
            reason_counts[result["message"]] = reason_counts.get(result["message"], 0) + 1
            stats["pages_visited"] += int(result.get("pages_visited", 0) or 0)
            stats["candidate_contact_links_found"] += int(result.get("candidate_contact_links_found", 0) or 0)
            if status.startswith("skipped") and bool(result.get("skipped_before_exploration", False)):
                stats["skipped_before_exploration"] += 1

            if status == "sent":
                stats["sent"] += 1
                rate_limiter.record_submission(result["salon_id"])
                consecutive_failures = 0
            elif is_prepared_status(status):
                stats["prepared"] += 1
                rate_limiter.record_prepared(result["salon_id"])
                consecutive_failures = 0

                _, added = append_review_entry(
                    {
                        "timestamp": result["timestamp"],
                        "salon_id": result["salon_id"],
                        "salon_name": result["salon_name"],
                        "domain": result["domain"],
                        "contact_url": result["contact_url"],
                        "final_step_url": result["final_step_url"],
                        "submit_selector": result["submit_selector"],
                        "confirm_selector": result["confirm_selector"],
                        "screenshot_folder": result["screenshot_folder"],
                        "status": result["status"],
                        "reason": result["message"],
                        "notes": (result.get("validation_notes") or result.get("notes") or result["message"]),
                        "evidence": result.get("evidence", ""),
                        "detected_required_fields": "|".join(result.get("detected_required_fields", [])),
                        "filled_fields": "|".join(result.get("filled_fields", [])),
                        "missing_required_fields": "|".join(result.get("missing_required_fields", [])),
                        "validation_errors": " / ".join(result.get("validation_errors", [])),
                        "decision": result.get("decision", "prepared_needs_manual"),
                        "confidence_level": result.get("confidence_level", ""),
                        "stop_state": result.get("stop_state", ""),
                        "detected_platform": result.get("detected_platform", ""),
                        "reopen_in_browser_url": result.get("reopen_in_browser_url", ""),
                        "form_root_selector": result.get("form_root_selector", ""),
                        "field_selector_map": result.get("field_selector_map", ""),
                    },
                    results_dir=RESULTS_DIR,
                    date_str=date_str,
                )
                if not added:
                    logger.info(f"[{result['salon_id']}] review queue row already exists today")
                existing_prepared_ids.add(result["salon_id"])

                if mode == "SEMI_AUTO":
                    report_ok = bool(result.get("has_store_name_in_message")) and bool(
                        result.get("has_demo_url_in_message")
                    ) and not result.get("any_missing_required_fields", [])
                    append_semi_auto_report(
                        [
                            {
                                "id": result["salon_id"],
                                "店名": result["salon_name"],
                                "url_demo": result["demo_url"],
                                "ok": str(report_ok),
                                "missing_fields": "|".join(result.get("any_missing_required_fields", [])),
                                "notes": result.get("validation_notes", ""),
                                "screenshot_path": result.get("confirm_screenshot_path", ""),
                            }
                        ]
                    )

            elif status == "failed":
                stats["failed"] += 1
                rate_limiter.record_skip(result["salon_id"])
                consecutive_failures += 1
            else:
                stats["skipped"] += 1
                rate_limiter.record_skip(result["salon_id"])

            # bot protection cooldown
            if result.get("bot_protection"):
                if result.get("blocked_domain"):
                    blocked_domains.append(result["blocked_domain"])
                cooldown_min = int(settings.get("cooldown_on_error_min_sec", settings.get("error_cooldown_min", 30)))
                cooldown_max = int(settings.get("cooldown_on_error_max_sec", settings.get("error_cooldown_max", 90)))
                sleep_sec = random.uniform(cooldown_min, cooldown_max)
                logger.warning(f"[{lead_id}] bot protection cooldown: sleep {sleep_sec:.0f}s")
                await asyncio.sleep(sleep_sec)
            elif status == "failed" and consecutive_failures >= int(settings.get("consecutive_failure_pause_threshold", 3)):
                pause_minutes = int(settings.get("consecutive_failure_pause_minutes", 10))
                logger.warning(f"consecutive failures={consecutive_failures}, pausing {pause_minutes} minutes")
                await asyncio.sleep(pause_minutes * 60)
                consecutive_failures = 0

            if mode == "SEMI_AUTO" and semi_auto_verify and idx < len(to_process):
                if semi_auto_prompt and sys.stdin.isatty():
                    answer = input("Proceed to next lead? (y/n): ").strip().lower()
                    if answer not in {"y", "yes"}:
                        logger.info("semi_auto verify stopped by user")
                        break
                else:
                    logger.info("semi_auto verify prompt skipped (non-interactive or disabled)")

            # between-lead delay
            if idx < len(to_process) and rate_limiter.can_submit():
                min_delay = int(settings.get("min_delay_sec", settings.get("delay_min", 5)))
                max_delay = int(settings.get("max_delay_sec", settings.get("delay_max", 5)))
                wait = random.uniform(min_delay, max_delay)
                logger.info(f"[{lead_id}] next lead delay: {wait:.0f}s")
                await asyncio.sleep(wait)

        if mode == "SEMI_AUTO":
            logger.info("SEMI_AUTO: browser remains open for operator. Waiting explicit end signal.")
            await _wait_operator_session_end(settings)

        try:
            await browser.close()
        except Exception as e:
            logger.warning("[MAIN] browser close warning: %s", e)

    if results:
        submissions_path = append_results(results, date_str)
    else:
        submissions_path = os.path.join(RESULTS_DIR, f"submissions_{date_str}.csv")

    prepared_view_path = ""
    if mode == "DETECT_ONLY":
        try:
            prepared_view_path = export_leads_prepared_view(leads_path, date_str)
        except Exception as e:
            logger.warning("[MAIN] leads_prepared export failed: %s", e)

    remaining_unprocessed = [lead for lead in leads if not rate_limiter.is_completed(lead["id"]) and not ledger_has(lead["id"], LEDGER_PATH)]
    next_lead_id = remaining_unprocessed[0]["id"] if remaining_unprocessed else ""
    next_lead_index = None
    if next_lead_id:
        for i, row in enumerate(leads, 1):
            if row["id"] == next_lead_id:
                next_lead_index = i
                break

    final_stats = rate_limiter.get_stats()
    stats["remaining"] = final_stats["remaining"]
    stats["success_rate"] = stats["sent"] / stats["total"] if stats["total"] else 0.0
    stats["limit_reached"] = not rate_limiter.can_submit()

    summary_path = generate_summary_report(
        results=results,
        stats=stats,
        settings=settings,
        blocked_domains_today=blocked_domains,
        next_lead_id=next_lead_id,
        unprocessed_count=len(remaining_unprocessed),
        results_dir=RESULTS_DIR,
        next_lead_index=next_lead_index,
    )

    logger.info(f"results: {submissions_path}")
    if prepared_view_path:
        logger.info(f"prepared-view: {prepared_view_path}")
    logger.info(f"summary: {summary_path}")
    logger.info(f"log: {os.path.join(LOGS_DIR, f'{date_str}.log')}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Playwright Automation")
    parser.add_argument("--report-only", action="store_true", help="Print latest summary")
    parser.add_argument("--dry-run", action="store_true", help="Prepare only (no final submit)")
    parser.add_argument("--test", action="store_true", help="Test mode (limit=2)")
    parser.add_argument("--mode", choices=["SEMI_AUTO", "FULL_AUTO", "DETECT_ONLY"], help="Override mode")
    parser.add_argument("--semi-auto-verify", action="store_true", help="Enable SEMI_AUTO verify mode")
    parser.add_argument("--semi-auto-limit", type=int, default=3, help="SEMI_AUTO verify lead count")
    parser.add_argument("--no-prompt", action="store_true", help="Disable SEMI_AUTO next-lead prompt")
    parser.add_argument("--limit", type=int, help="Override daily_limit for this run")
    parser.add_argument("--leads", help="Override leads CSV path for this run")
    parser.add_argument("--input", help="Alias for --leads; useful for pipeline handoff CSVs")
    args = parser.parse_args()

    if args.report_only:
        print_report_from_files(RESULTS_DIR, DATA_DIR)
        return

    overrides = build_cli_overrides(args)

    try:
        stats = asyncio.run(run(overrides if overrides else None))
    except KeyboardInterrupt:
        print("\n中断しました。")
        sys.exit(1)

    if stats.get("stopped"):
        print("STOPスイッチONのため終了しました。")
    elif stats.get("quiet_hours"):
        print("静穏時間帯のため終了しました。")
    elif stats.get("limit_reached"):
        print("本日の送信上限に達しました。")

    if stats.get("prepared", 0) > 0:
        print(f"{stats['prepared']}件を prepared として記録しました。")
        print("manual submit: python src/resume_submit.py --salon-id <ID>")


if __name__ == "__main__":
    main()
