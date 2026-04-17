"""Playwright prefill helper (no submit).

Usage:
  python -m src.prefill_only --lead-id 1137 --queue results/review_queue_YYYYMMDD.csv --keep-open
  python -m src.prefill_only --salon-id 1137 --review-queue results/review_queue_YYYYMMDD.csv --keep-open
  python -m src.prefill_only --lead-id 1137 --final-url https://example.com/contact --keep-open
  python -m src.prefill_only --lead-id 1137 --detect-only --base-url https://example.com
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from playwright.async_api import Page, async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.form_detector import FormDetector
from src.message_generator import MessageGenerator
from src.safety import detect_bot_protection, detect_hard_site

JST = ZoneInfo("Asia/Tokyo")

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = PROJECT_ROOT / "results"
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"

SETTINGS_PATH = CONFIG_DIR / "settings.json"
SENDER_INFO_PATH = CONFIG_DIR / "sender_info.json"
TEMPLATE_PATH = CONFIG_DIR / "message_template.txt"
LEADS_PATH = DATA_DIR / "leads.csv"


@dataclass
class PrefillResult:
    salon_id: str
    status: str
    reason: str
    screenshots_dir: str
    final_step_url: str
    stopped_at: str
    stop_state: str
    debug_screenshot: str = ""
    candidate_urls: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "salon_id": self.salon_id,
                "status": self.status,
                "reason": self.reason,
                "screenshots_dir": self.screenshots_dir,
                "final_step_url": self.final_step_url,
                "stopped_at": self.stopped_at,
                "stop_state": self.stop_state,
                "debug_screenshot": self.debug_screenshot,
                "candidate_urls": self.candidate_urls,
            },
            ensure_ascii=False,
        )


def _load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return fallback.copy()
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return fallback.copy()


def _normalize_col(name: str) -> str:
    return re.sub(r"[\s_\-()（）・]+", "", str(name).strip().lower())


def _resolve_column(columns: Sequence[str], aliases: Sequence[str]) -> Optional[str]:
    mapping = {_normalize_col(c): c for c in columns}
    for alias in aliases:
        key = _normalize_col(alias)
        if key in mapping:
            return mapping[key]
    return None


def _pick_latest_review_queue() -> Optional[Path]:
    files = list(RESULTS_DIR.glob("review_queue_*.csv"))
    if not files:
        return None

    def _sort_key(path: Path) -> tuple[str, float]:
        m = re.search(r"review_queue_(\d{8})", path.name)
        token = m.group(1) if m else "00000000"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return token, mtime

    return sorted(files, key=_sort_key)[-1]


def _read_csv_rows(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def _pick(row: dict, keys: Sequence[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _load_review_row(salon_id: str, queue_path: Path) -> Optional[dict]:
    rows, columns = _read_csv_rows(queue_path)
    if not rows:
        return None

    id_col = _resolve_column(columns, ["salon_id", "id", "ID"])
    name_col = _resolve_column(columns, ["salon_name", "店名", "店舗名", "名称", "name"])
    final_col = _resolve_column(columns, ["final_step_url", "url", "URL"])
    contact_col = _resolve_column(columns, ["contact_url", "url", "URL"])
    status_col = _resolve_column(columns, ["status"])
    reason_col = _resolve_column(columns, ["reason", "message", "notes"])
    domain_col = _resolve_column(columns, ["domain"])
    platform_col = _resolve_column(columns, ["detected_platform"])
    form_root_col = _resolve_column(columns, ["form_root_selector"])
    field_map_col = _resolve_column(columns, ["field_selector_map"])
    submit_col = _resolve_column(columns, ["submit_selector"])
    confirm_col = _resolve_column(columns, ["confirm_selector"])
    reopen_col = _resolve_column(columns, ["reopen_in_browser_url"])

    target = str(salon_id).strip()
    for row in reversed(rows):
        rid = str(row.get(id_col, "") if id_col else "").strip()
        if rid != target:
            continue
        return {
            "salon_id": rid,
            "salon_name": str(row.get(name_col, "") if name_col else "").strip(),
            "final_step_url": str(row.get(final_col, "") if final_col else "").strip(),
            "contact_url": str(row.get(contact_col, "") if contact_col else "").strip(),
            "status": str(row.get(status_col, "") if status_col else "").strip(),
            "reason": str(row.get(reason_col, "") if reason_col else "").strip(),
            "domain": str(row.get(domain_col, "") if domain_col else "").strip(),
            "detected_platform": str(row.get(platform_col, "") if platform_col else "").strip(),
            "form_root_selector": str(row.get(form_root_col, "") if form_root_col else "").strip(),
            "field_selector_map": str(row.get(field_map_col, "") if field_map_col else "").strip(),
            "submit_selector": str(row.get(submit_col, "") if submit_col else "").strip(),
            "confirm_selector": str(row.get(confirm_col, "") if confirm_col else "").strip(),
            "reopen_in_browser_url": str(row.get(reopen_col, "") if reopen_col else "").strip(),
        }
    return None


def _load_leads_lookup(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    out: Dict[str, dict] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lead_id = _pick(row, ["id", "ID"])
            if not lead_id:
                continue
            out[lead_id] = {
                "id": lead_id,
                "salon_name": _pick(row, ["店名", "店舗名", "名称", "サロン名", "salon_name", "name"]),
                "url": _pick(row, ["url(旧)", "url（旧）", "URL", "url", "old_url"]),
                "demo_url": _pick(row, ["url(デモ)", "url(デモページ)", "url（デモ）", "demo_url", "url_demo"]),
            }
    return out


def _action_log_path(date_str: str) -> Path:
    return RESULTS_DIR / f"staff_actions_{date_str}.csv"


def _append_staff_action(
    *,
    salon_id: str,
    status: str,
    reason: str,
    stop_state: str,
    screenshots_dir: str,
    date_str: str,
) -> None:
    path = _action_log_path(date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    default_fields = ["timestamp", "salon_id", "action", "status", "reason", "stop_state", "screenshots_dir"]
    fields = list(default_fields)
    has_stop_state_col = True
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, [])
                if header:
                    fields = list(header)
                    has_stop_state_col = "stop_state" in fields
        except Exception:
            fields = list(default_fields)
            has_stop_state_col = True
    reason_text = str(reason).strip()
    if not has_stop_state_col and stop_state:
        reason_text = f"{reason_text}|stop_state={stop_state}"
    row = {
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
        "salon_id": str(salon_id).strip(),
        "action": "open_prefill",
        "status": str(status).strip(),
        "reason": reason_text,
        "stop_state": str(stop_state).strip(),
        "screenshots_dir": str(screenshots_dir).strip(),
    }

    exists = path.exists()
    with path.open("a" if exists else "w", encoding="utf-8" if exists else "utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _safe_update_last_action(queue_path: Optional[Path], salon_id: str, action: str) -> None:
    if not queue_path or not queue_path.exists():
        return
    rows, fieldnames = _read_csv_rows(queue_path)
    if not rows:
        return
    fieldnames = list(fieldnames or [])
    if "last_action" not in fieldnames:
        fieldnames.append("last_action")
        for row in rows:
            row.setdefault("last_action", "")
    if not fieldnames:
        return

    target = str(salon_id).strip()
    updated = False
    for row in reversed(rows):
        if str(row.get("salon_id", row.get("id", ""))).strip() == target:
            row["last_action"] = action
            updated = True
            break
    if not updated:
        return

    tmp_path = queue_path.with_suffix(queue_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, queue_path)


async def _take_screenshot(page: Page, salon_id: str, seq: int, label: str, date_str: str) -> str:
    out_dir = SCREENSHOTS_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{salon_id}_{seq:02d}_{label}.png"
    await page.screenshot(path=str(path), full_page=True)
    return str(path)


async def _wait_until_user_close(page: Page) -> None:
    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:
        pass


def _merge_candidate_urls(*urls: str, extra: Optional[List[str]] = None) -> List[str]:
    values = list(urls)
    if extra:
        values.extend(extra)
    out: List[str] = []
    seen = set()
    for v in values:
        s = str(v or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _is_prepared_status(status: str) -> bool:
    value = str(status or "").strip().lower()
    return value == "prepared" or value.startswith("prepared")


def _is_skipped_status(status: str) -> bool:
    return str(status or "").strip().lower().startswith("skipped")


def _parse_field_selector_map(raw: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return {}
    return {}


def _selector_to_css(raw_selector: str) -> str:
    text = str(raw_selector or "").strip()
    if not text:
        return ""
    for prefix in ("attr:", "placeholder:", "fallback:", "saved_css:"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    if text.startswith(("input", "textarea", "select", "form", "#", ".", "[")):
        return text
    return ""


def _merge_field_map(primary: dict, fallback: dict) -> dict:
    out = dict(primary or {})
    for key, value in (fallback or {}).items():
        if key not in out and str(value).strip():
            out[key] = str(value).strip()
    return out


async def _fields_from_saved_map(detector: FormDetector, selector_map: dict) -> Dict[str, object]:
    detected: Dict[str, object] = {}
    for field_name, selector_used in (selector_map or {}).items():
        if str(field_name).startswith("_"):
            continue
        css = _selector_to_css(str(selector_used))
        if not css:
            continue
        try:
            locator = detector.page.locator(css)
            if await detector._is_fillable(locator):  # type: ignore[attr-defined]
                detected[str(field_name)] = locator.first
        except Exception:
            continue
    return detected


async def _resolve_submit_from_saved(page: Page, selector_text: str) -> Optional[object]:
    css = _selector_to_css(selector_text)
    if not css:
        css = str(selector_text or "").strip()
    if not css:
        return None
    try:
        locator = page.locator(css)
        if await locator.count() == 0:
            return None
        await locator.first.wait_for(state="visible", timeout=2000)
        return locator.first
    except Exception:
        return None


async def _highlight_submit_button(submit_locator: object) -> None:
    try:
        await submit_locator.scroll_into_view_if_needed(timeout=3000)
        await submit_locator.evaluate(
            """
            (el) => {
              const cls = "kimoto-prefill-submit-highlight";
              if (!document.getElementById(cls)) {
                const style = document.createElement("style");
                style.id = cls;
                style.textContent = `
                  .${cls} {
                    outline: 3px solid #ff5a1f !important;
                    outline-offset: 3px !important;
                    box-shadow: 0 0 0 4px rgba(255,90,31,.2) !important;
                  }
                `;
                document.head.appendChild(style);
              }
              el.classList.add(cls);
            }
            """
        )
    except Exception:
        return


async def _run_prefill(args: argparse.Namespace) -> PrefillResult:
    settings = _load_json(SETTINGS_PATH, {})
    sender_info = _load_json(SENDER_INFO_PATH, {})
    timeout_sec = int(settings.get("timeout_seconds", 30))
    lead_id = str(getattr(args, "lead_id", "") or getattr(args, "salon_id", "")).strip()

    queue_path: Optional[Path]
    queue_arg = str(getattr(args, "review_queue", "") or getattr(args, "queue", "")).strip()
    if queue_arg:
        queue_path = Path(queue_arg)
    else:
        queue_path = _pick_latest_review_queue()

    today_str = datetime.now(JST).strftime("%Y%m%d")
    date_str = today_str
    if queue_path and queue_path.exists():
        m = re.search(r"review_queue_(\d{8})", queue_path.name)
        if m:
            date_str = m.group(1)

    screenshots_dir = str(SCREENSHOTS_DIR / date_str)
    debug_path = RESULTS_DIR / "debug_prefill_failed.png"

    row = None
    if queue_path and queue_path.exists():
        row = _load_review_row(lead_id, queue_path)

    if not row and not args.final_url:
        result = PrefillResult(
            salon_id=lead_id,
            status="failed",
            reason="missing_target_url",
            screenshots_dir=screenshots_dir,
            final_step_url="",
            stopped_at="bootstrap",
            stop_state="unknown",
        )
        _append_staff_action(
            salon_id=result.salon_id,
            status=result.status,
            reason=result.reason,
            stop_state=result.stop_state,
            screenshots_dir=result.screenshots_dir,
            date_str=date_str,
        )
        return result

    salon_id = lead_id
    row = row or {
        "salon_id": salon_id,
        "salon_name": "",
        "final_step_url": "",
        "contact_url": "",
        "status": "",
        "reason": "",
        "domain": "",
        "detected_platform": "",
        "form_root_selector": "",
        "field_selector_map": "",
        "submit_selector": "",
        "confirm_selector": "",
        "reopen_in_browser_url": "",
    }

    lead_lookup = _load_leads_lookup(Path(str(settings.get("leads_csv_path", LEADS_PATH))))
    lead_row = lead_lookup.get(salon_id, {})

    final_step_url = str(
        args.final_url
        or row.get("reopen_in_browser_url")
        or row.get("final_step_url")
        or ""
    ).strip()
    contact_url = str(row.get("contact_url") or lead_row.get("url") or "").strip()
    target_url = final_step_url or contact_url
    base_url = str(args.base_url or lead_row.get("url") or contact_url or target_url).strip()
    stored_selector_map = _parse_field_selector_map(str(row.get("field_selector_map", "")))
    stored_submit_selector = str(row.get("submit_selector", "")).strip()
    stored_confirm_selector = str(row.get("confirm_selector", "")).strip()

    msg_gen = MessageGenerator(
        str(TEMPLATE_PATH),
        str(SENDER_INFO_PATH),
        wrap_message=bool(settings.get("wrap_message", True)),
        wrap_width=int(settings.get("wrap_width", 56)),
        debug=bool(settings.get("debug", False)),
    )
    lead_for_message = {
        "店名": lead_row.get("salon_name", "") or row.get("salon_name", ""),
        "url(デモ)": lead_row.get("demo_url", ""),
        "url(旧)": lead_row.get("url", "") or contact_url or target_url,
    }
    resolved = msg_gen.resolve_lead_fields(lead_for_message)
    salon_name = str(resolved.get("salon_name") or row.get("salon_name") or lead_row.get("salon_name") or salon_id).strip()
    demo_url = str(resolved.get("demo_url") or "").strip()
    message = msg_gen.generate(salon_name, demo_url)
    subject = msg_gen.generate_subject(salon_name)

    result = PrefillResult(
        salon_id=salon_id,
        status="failed",
        reason="unknown",
        screenshots_dir=screenshots_dir,
        final_step_url=target_url,
        stopped_at="bootstrap",
        stop_state="unknown",
        candidate_urls=_merge_candidate_urls(target_url, contact_url, base_url),
    )

    browser = None
    context = None
    page = None

    keep_open = bool(args.keep_open) and not bool(args.no_wait)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                viewport={
                    "width": int(settings.get("browser", {}).get("viewport_width", 1280)),
                    "height": int(settings.get("browser", {}).get("viewport_height", 720)),
                },
                user_agent=settings.get("browser", {}).get(
                    "user_agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                ),
                locale=settings.get("browser", {}).get("locale", "ja-JP"),
            )
            page = await context.new_page()
            detector = FormDetector(page, sender_info, timeout=timeout_sec)

            if args.detect_only:
                if not base_url:
                    result.status = "failed"
                    result.reason = "missing_base_url"
                    result.stopped_at = "bootstrap"
                else:
                    contact_candidate = await detector.find_contact_page(
                        base_url,
                        max_internal_links=int(settings.get("max_contact_candidate_links", 80)),
                        max_pages_to_try=int(settings.get("max_contact_pages_to_try", 8)),
                        contact_link_text_keywords=settings.get("contact_link_text_keywords"),
                        allow_querystring_urls=bool(settings.get("allow_querystring_urls", True)),
                    )
                    result.candidate_urls = _merge_candidate_urls(
                        target_url,
                        contact_url,
                        base_url,
                        contact_candidate or "",
                        extra=getattr(detector, "last_candidate_urls", []),
                    )
                    if contact_candidate:
                        await page.goto(contact_candidate, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
                        await detector.dismiss_cookie_banners()
                        await asyncio.sleep(0.3)
                        await _take_screenshot(page, salon_id, 1, "contact_page", date_str)
                        found, detect_reason, _meta = await detector.detect_form_presence()
                        result.final_step_url = page.url
                        result.stopped_at = "contact_page"
                        if found:
                            result.status = "prepared"
                            result.reason = detect_reason or "form_detected"
                        else:
                            result.status = "skipped"
                            result.reason = detect_reason or "no_form_found"
                    else:
                        result.status = "skipped"
                        result.reason = "no_contact_page"
                        result.stopped_at = "exploration"
            else:
                if not target_url:
                    result.status = "failed"
                    result.reason = "missing_target_url"
                    result.stopped_at = "bootstrap"
                    result.stop_state = "unknown"
                else:
                    response = await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
                    await detector.dismiss_cookie_banners()
                    await page.wait_for_load_state("networkidle", timeout=min(timeout_sec, 10) * 1000)
                    await _take_screenshot(page, salon_id, 1, "before_fill", date_str)

                    status_code = response.status if response else None
                    page_text = await page.inner_text("body")

                    if detect_bot_protection(page_text, status_code) or await detector.detect_captcha():
                        result.status = "skipped_bot_protection"
                        result.reason = "bot_protection"
                        result.stopped_at = "before_fill"
                        result.stop_state = "unknown"
                        result.final_step_url = page.url
                    else:
                        hard, hard_reason = detect_hard_site(page_text, status_code, skip_if_requires_login=True)
                        if hard and hard_reason in {"requires_login", "bot_protection"}:
                            if hard_reason == "requires_login":
                                result.status = "skipped_login"
                            else:
                                result.status = "skipped_bot_protection"
                            result.reason = hard_reason
                            result.stopped_at = "before_fill"
                            result.stop_state = "unknown"
                            result.final_step_url = page.url
                        else:
                            fields, detected_form_map = await detector.detect_form_fields()
                            merged_form_map = _merge_field_map(detected_form_map, stored_selector_map)
                            saved_fields = await _fields_from_saved_map(detector, stored_selector_map)
                            for key, locator in saved_fields.items():
                                if key not in fields:
                                    fields[key] = locator
                            if not fields and base_url:
                                contact_candidate = await detector.find_contact_page(
                                    base_url,
                                    max_internal_links=int(settings.get("max_contact_candidate_links", 80)),
                                    max_pages_to_try=int(settings.get("max_contact_pages_to_try", 8)),
                                    contact_link_text_keywords=settings.get("contact_link_text_keywords"),
                                    allow_querystring_urls=bool(settings.get("allow_querystring_urls", True)),
                                )
                                result.candidate_urls = _merge_candidate_urls(
                                    target_url,
                                    contact_url,
                                    base_url,
                                    contact_candidate or "",
                                    extra=getattr(detector, "last_candidate_urls", []),
                                )
                                if contact_candidate and contact_candidate != page.url:
                                    await page.goto(contact_candidate, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
                                    await detector.dismiss_cookie_banners()
                                    await page.wait_for_load_state("networkidle", timeout=min(timeout_sec, 10) * 1000)
                                    result.final_step_url = page.url
                                    await _take_screenshot(page, salon_id, 1, "before_fill", date_str)
                                    fields, detected_form_map = await detector.detect_form_fields()
                                    merged_form_map = _merge_field_map(detected_form_map, merged_form_map)
                                    saved_fields = await _fields_from_saved_map(detector, stored_selector_map)
                                    for key, locator in saved_fields.items():
                                        if key not in fields:
                                            fields[key] = locator
                            else:
                                result.candidate_urls = _merge_candidate_urls(
                                    target_url,
                                    contact_url,
                                    base_url,
                                    extra=getattr(detector, "last_candidate_urls", []),
                                )

                            if not fields and contact_url and contact_url != page.url:
                                try:
                                    await page.goto(contact_url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
                                    await detector.dismiss_cookie_banners()
                                    await page.wait_for_load_state("networkidle", timeout=min(timeout_sec, 10) * 1000)
                                    result.final_step_url = page.url
                                    await _take_screenshot(page, salon_id, 1, "before_fill", date_str)
                                    fields, detected_form_map = await detector.detect_form_fields()
                                    merged_form_map = _merge_field_map(detected_form_map, merged_form_map)
                                except Exception:
                                    pass

                            if not fields:
                                result.status = "prepared_review_needed"
                                result.reason = "dom_mismatch_or_form_not_found"
                                result.stopped_at = "before_fill"
                                result.stop_state = "unknown"
                                result.final_step_url = page.url
                            else:
                                req = await detector.inspect_required_fields()
                                if req.get("address_required_fields"):
                                    result.status = "prepared_review_needed"
                                    result.reason = "requires_address"
                                    result.stopped_at = "before_fill"
                                    result.stop_state = "form_filled"
                                    result.final_step_url = page.url
                                else:
                                    fill_ok, fill_stats = await detector.fill_form(fields, message, subject)
                                    await detector.fill_required_dates()
                                    await detector.handle_checkboxes()
                                    await detector.handle_dropdowns(required_only=True)
                                    await _take_screenshot(page, salon_id, 2, "after_fill", date_str)

                                    submit_btn, submit_selector, is_confirm_step = await detector.find_submit_button()
                                    if not submit_btn:
                                        saved_submit = await _resolve_submit_from_saved(page, stored_submit_selector)
                                        saved_confirm = await _resolve_submit_from_saved(page, stored_confirm_selector)
                                        if saved_confirm:
                                            submit_btn = saved_confirm
                                            submit_selector = stored_confirm_selector
                                            is_confirm_step = True
                                        elif saved_submit:
                                            submit_btn = saved_submit
                                            submit_selector = stored_submit_selector
                                            is_confirm_step = False

                                    if submit_btn:
                                        await _highlight_submit_button(submit_btn)
                                    await _take_screenshot(page, salon_id, 3, "before_submit_or_confirm", date_str)
                                    result.stopped_at = "submit_button"
                                    result.stop_state = "submit_button"
                                    result.final_step_url = page.url

                                    # Confirmation step is allowed; final submit is never clicked.
                                    if is_confirm_step and submit_btn:
                                        await submit_btn.click()
                                        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_sec, 10) * 1000)
                                        final_btn, _final_selector, _ = await detector.find_submit_button()
                                        if final_btn:
                                            await _highlight_submit_button(final_btn)
                                        await _take_screenshot(page, salon_id, 4, "on_confirmation_page", date_str)
                                        result.stopped_at = "confirmation"
                                        result.stop_state = "confirmation"
                                        result.final_step_url = page.url

                                    if not submit_btn:
                                        result.status = "prepared_review_needed"
                                        result.reason = "no_submit_button"
                                        result.stop_state = "form_filled" if fill_stats.get("filled_fields") else "unknown"
                                        result.stopped_at = result.stop_state
                                    elif fill_ok:
                                        missing = list(fill_stats.get("missing_required_fields", []))
                                        if missing:
                                            result.status = "prepared_partial"
                                            result.reason = "prefill_incomplete"
                                        else:
                                            result.status = "prepared_full"
                                            result.reason = "prefill_ready"
                                    else:
                                        missing = fill_stats.get("missing_required_fields", [])
                                        result.status = "prepared_partial"
                                        result.reason = "prefill_incomplete" if missing else "prefill_partial"

                            if not result.stop_state:
                                result.stop_state = "unknown"
                            if not result.stopped_at:
                                result.stopped_at = result.stop_state
                            if merged_form_map:
                                row["field_selector_map"] = json.dumps(merged_form_map, ensure_ascii=False)

            if keep_open and page is not None:
                print("Browser left open for manual review; close the browser window to finish.", flush=True)
                await _wait_until_user_close(page)
    except Exception as e:
        result.status = "failed"
        result.reason = f"exception:{str(e)[:120]}"
        result.stopped_at = "exception"
        result.stop_state = "unknown"
        result.debug_screenshot = str(debug_path)
        if page is not None:
            try:
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_path), full_page=True)
            except Exception:
                pass
    finally:
        # Close only after manual close (keep_open) or immediately (no_wait/non-keep-open).
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass

    _append_staff_action(
        salon_id=result.salon_id,
        status=result.status,
        reason=result.reason,
        stop_state=result.stop_state,
        screenshots_dir=result.screenshots_dir,
        date_str=date_str,
    )
    action_value = f"open_prefill:{result.stop_state or 'unknown'}"
    _safe_update_last_action(queue_path, salon_id, action_value)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Playwright prefill helper (no submit)")
    parser.add_argument("--lead-id", dest="lead_id", help="target lead/salon id")
    parser.add_argument("--salon-id", dest="lead_id", help="legacy alias for --lead-id")
    parser.add_argument("--queue", dest="queue", help="review queue CSV path")
    parser.add_argument("--review-queue", dest="review_queue", help="legacy alias for --queue")
    parser.add_argument("--final-url", help="override final/contact URL")
    parser.add_argument("--base-url", help="override base URL for contact search")
    parser.add_argument("--detect-only", action="store_true", help="rerun contact detection only")
    parser.add_argument(
        "--keep-open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep browser open for manual review (default: true)",
    )
    parser.add_argument("--no-wait", action="store_true", help="legacy: do not wait for manual browser close")
    args = parser.parse_args()
    if not str(getattr(args, "lead_id", "")).strip():
        parser.error("--lead-id (or --salon-id) is required")

    result = asyncio.run(_run_prefill(args))
    print(result.to_json(), flush=True)

    if _is_prepared_status(result.status):
        raise SystemExit(0)
    if _is_skipped_status(result.status):
        raise SystemExit(2)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
