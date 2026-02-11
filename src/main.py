"""Main execution module for spiritual salon outreach automation."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import sys
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from playwright.async_api import Page, async_playwright

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.blocklist import block_domain, ensure_blocklist_files, extract_domain, is_blocked
from src.form_detector import FormDetector
from src.ledger import append_ledger, ledger_has, read_ledger
from src.message_generator import MessageGenerator
from src.rate_limiter import RateLimiter
from src.report_generator import JsonlHandler, generate_summary_report, print_report_from_files
from src.review_queue import append_review_entry, queue_path, read_queue
from src.safety import DomainAttemptTracker, check_quiet_hours, check_robots_txt, detect_bot_protection, detect_corporate

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


def setup_logging(log_format: str = "text") -> logging.Logger:
    os.makedirs(LOGS_DIR, exist_ok=True)
    date_str = datetime.now(JST).strftime("%Y%m%d")

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    file_handler = logging.FileHandler(os.path.join(LOGS_DIR, f"{date_str}.log"), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console_handler)

    if log_format == "jsonl":
        logger.addHandler(JsonlHandler(os.path.join(LOGS_DIR, f"{date_str}.jsonl")))

    return logger


def load_settings() -> dict:
    with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _norm_key(value: str) -> str:
    return str(value).strip().lower().replace(" ", "")


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


def load_leads(path: str) -> List[dict]:
    leads: List[dict] = []
    if not os.path.exists(path):
        logging.error(f"[MAIN] leads file missing: {path}")
        return leads

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            original_url = _pick(
                row,
                [
                    "url(旧)",
                    "url（旧）",
                    "url旧",
                    "url(old)",
                    "URL",
                    "old_url",
                    "url",
                    "website",
                ],
            )
            lead = {
                "id": _pick(row, ["id", "ID"]),
                "salon_name": _pick(row, ["店名", "名称", "サロン名", "salon_name", "name"]),
                "url": original_url,
                "original_url": original_url,
                "demo_url": _pick(
                    row,
                    [
                        "url(デモ)",
                        "url(デモページ)",
                        "url（デモページ）",
                        "url（デモ）",
                        "urlデモ",
                        "demo_url",
                        "demo",
                    ],
                ),
            }
            if lead["id"] and lead["salon_name"] and lead["url"]:
                leads.append(lead)
    logging.info(f"[MAIN] loaded leads: {len(leads)}")
    return leads


def append_results(results: List[dict], date_str: str) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"submissions_{date_str}.csv")

    fieldnames = ["timestamp", "salon_id", "salon_name", "url", "demo_url", "status", "message"]
    file_exists = os.path.exists(path)
    mode = "a" if file_exists else "w"
    encoding = "utf-8" if file_exists else "utf-8-sig"

    with open(path, mode, encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    return path


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
    lead_id = lead["id"]
    salon_name = lead["salon_name"]
    base_url = lead["url"]
    demo_url = lead["demo_url"]
    domain = extract_domain(base_url)

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
    }

    logger.info(f"[{lead_id}] start: {salon_name} ({base_url})")

    # pre-check: blocklist + cooldown
    blocked, blocked_reason = is_blocked(domain, base_url, DATA_DIR)
    if blocked:
        result["status"] = "skipped"
        result["message"] = blocked_reason
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
        result["status"] = "skipped"
        result["message"] = reason
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": "",
                "final_step_url": "",
                "status": "skipped",
                "reason": reason,
            },
            path=LEDGER_PATH,
        )
        return result

    domain_tracker.record_attempt(domain)

    detector = FormDetector(page, message_gen.sender_info, timeout=settings.get("timeout_seconds", 30))

    try:
        if settings.get("respect_robots_and_terms", True):
            disallowed, reason = await check_robots_txt(page, base_url)
            if disallowed:
                result["status"] = "skipped"
                result["message"] = reason
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": "",
                        "final_step_url": "",
                        "status": "skipped",
                        "reason": reason,
                    },
                    path=LEDGER_PATH,
                )
                return result

        contact_url = await detector.find_contact_page(base_url)
        if not contact_url:
            result["status"] = "skipped"
            result["message"] = "no_contact_page"
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": "",
                    "final_step_url": "",
                    "status": "skipped",
                    "reason": "no_contact_page",
                },
                path=LEDGER_PATH,
            )
            return result

        result["contact_url"] = contact_url

        response = await page.goto(contact_url, timeout=settings.get("timeout_seconds", 30) * 1000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        status_code = response.status if response else None
        page_text = await page.inner_text("body")
        if detect_bot_protection(page_text, status_code):
            block_domain(domain, days=7, reason="bot_protection", data_dir=DATA_DIR)
            result["status"] = "skipped"
            result["message"] = "bot_protection"
            result["bot_protection"] = True
            result["blocked_domain"] = domain
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": "",
                    "status": "skipped",
                    "reason": "bot_protection",
                },
                path=LEDGER_PATH,
            )
            return result

        if settings.get("business_only_filter", True):
            is_corp, corp_reason = detect_corporate(page_text)
            if is_corp:
                result["status"] = "skipped"
                result["message"] = corp_reason
                append_ledger(
                    {
                        "run_mode": mode,
                        "salon_id": lead_id,
                        "salon_name": salon_name,
                        "domain": domain,
                        "contact_url": contact_url,
                        "final_step_url": "",
                        "status": "skipped",
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
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": "",
                    "status": "skipped",
                    "reason": "bot_protection",
                },
                path=LEDGER_PATH,
            )
            return result

        fields, form_map = await detector.detect_form_fields()
        if not fields:
            result["status"] = "skipped"
            result["message"] = "no_form_fields"
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": "",
                    "status": "skipped",
                    "reason": "no_form_fields",
                },
                path=LEDGER_PATH,
            )
            return result

        logger.info(f"[{lead_id}] form_map={json.dumps(form_map, ensure_ascii=False)}")

        seq = 0
        if settings.get("screenshot_enabled", True):
            seq += 1
            await take_screenshot(page, lead_id, seq, "before_fill", date_str)

        resolved = message_gen.resolve_lead_fields(lead)
        salon_name = resolved.get("salon_name") or salon_name
        demo_url = resolved.get("demo_url") or demo_url
        if mode == "SEMI_AUTO":
            logger.info(f"Resolved lead: id={lead_id}, salon_name={salon_name}, demo_url={demo_url}")

        message = message_gen.generate(salon_name, demo_url)
        subject = message_gen.generate_subject(salon_name)
        fill_ok, fill_stats = await detector.fill_form(fields, message, subject)
        if not fill_ok:
            result["status"] = "failed"
            result["message"] = f"fill_incomplete:{fill_stats['filled']}/{fill_stats['total_fields']}"
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": "",
                    "status": "failed",
                    "reason": result["message"],
                },
                path=LEDGER_PATH,
            )
            return result

        await detector.handle_checkboxes()
        await detector.handle_dropdowns()

        if settings.get("screenshot_enabled", True):
            seq += 1
            await take_screenshot(page, lead_id, seq, "after_fill", date_str)

        submit_btn, submit_selector, is_confirm_step = await detector.find_submit_button()
        if not submit_btn:
            result["status"] = "failed"
            result["message"] = "no_submit_button"
            append_ledger(
                {
                    "run_mode": mode,
                    "salon_id": lead_id,
                    "salon_name": salon_name,
                    "domain": domain,
                    "contact_url": contact_url,
                    "final_step_url": "",
                    "status": "failed",
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

        if settings.get("screenshot_enabled", True):
            seq += 1
            await take_screenshot(page, lead_id, seq, "before_submit_or_confirm", date_str)

        # SEMI_AUTO: never final submit
        if mode == "SEMI_AUTO" or settings.get("dry_run", False):
            if is_confirm_step:
                await submit_btn.click()
                await asyncio.sleep(2)

                final_btn, final_selector, _ = await detector.find_submit_button()
                result["final_step_url"] = page.url
                if final_btn:
                    result["submit_selector"] = final_selector

                if settings.get("screenshot_enabled", True):
                    seq += 1
                    await take_screenshot(page, lead_id, seq, "on_confirmation_page", date_str)
                logger.info(f"[{lead_id}] SEMI_AUTO: stopped on confirmation page")
            else:
                logger.info(f"[{lead_id}] SEMI_AUTO: stopped before submit")

            result["status"] = "prepared"
            result["message"] = "prepared"
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
            await asyncio.sleep(2)

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
        await asyncio.sleep(2)
        await detector.handle_confirmation_page()
        await asyncio.sleep(2)

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
        result["status"] = "failed"
        result["message"] = f"exception:{str(e)[:160]}"
        append_ledger(
            {
                "run_mode": mode,
                "salon_id": lead_id,
                "salon_name": salon_name,
                "domain": domain,
                "contact_url": result.get("contact_url", ""),
                "final_step_url": result.get("final_step_url", ""),
                "status": "failed",
                "reason": result["message"],
            },
            path=LEDGER_PATH,
        )
        return result


async def run(settings_override: Optional[dict] = None) -> Dict:
    settings = load_settings()
    if settings_override:
        settings.update(settings_override)

    logger = setup_logging(log_format=settings.get("log_format", "text"))
    ensure_blocklist_files(DATA_DIR)

    now = datetime.now(JST)
    date_str = now.strftime("%Y%m%d")
    mode = str(settings.get("mode", "SEMI_AUTO")).upper()
    if mode not in {"SEMI_AUTO", "FULL_AUTO"}:
        mode = "SEMI_AUTO"

    logger.info("=" * 60)
    logger.info("Spiritual Salon Automation")
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

    daily_limit = int(settings.get("test_limit", 2) if settings.get("test_mode", False) else settings.get("daily_limit", 20))

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

    to_process = unprocessed[: rate_limiter.remaining()]
    domain_tracker = DomainAttemptTracker(max_per_day=int(settings.get("max_attempts_per_domain_per_day", 2)))
    message_gen = MessageGenerator(
        TEMPLATE_PATH,
        SENDER_INFO_PATH,
        wrap_message=bool(settings.get("wrap_message", True)),
        wrap_width=int(settings.get("wrap_width", 56)),
    )

    results: List[dict] = []
    stats = {"total": 0, "sent": 0, "prepared": 0, "failed": 0, "skipped": 0}
    reason_counts: Dict[str, int] = {}
    blocked_domains: List[str] = []
    consecutive_failures = 0

    headless = bool(settings.get("headless", False if mode == "SEMI_AUTO" else True))

    existing_prepared_ids = set()
    if mode == "SEMI_AUTO":
        today_queue = queue_path(date_str=date_str, results_dir=RESULTS_DIR)
        for row in read_queue(today_queue):
            rid = str(row.get("salon_id", "")).strip()
            if rid:
                existing_prepared_ids.add(rid)

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
            result = await process_lead(
                page=page,
                lead=lead,
                settings=settings,
                message_gen=message_gen,
                domain_tracker=domain_tracker,
                mode=mode,
                date_str=date_str,
            )

            results.append(
                {
                    "timestamp": result["timestamp"],
                    "salon_id": result["salon_id"],
                    "salon_name": result["salon_name"],
                    "url": result["url"],
                    "demo_url": result["demo_url"],
                    "status": result["status"],
                    "message": result["message"],
                }
            )

            status = result["status"]
            reason_counts[result["message"]] = reason_counts.get(result["message"], 0) + 1

            if status == "sent":
                stats["sent"] += 1
                rate_limiter.record_submission(result["salon_id"])
                consecutive_failures = 0
            elif status == "prepared":
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
                        "status": "prepared",
                        "notes": result["message"],
                    },
                    results_dir=RESULTS_DIR,
                    date_str=date_str,
                )
                if not added:
                    logger.info(f"[{result['salon_id']}] review queue row already exists today")
                existing_prepared_ids.add(result["salon_id"])

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

            # between-lead delay
            if idx < len(to_process) and rate_limiter.can_submit():
                min_delay = int(settings.get("min_delay_sec", settings.get("delay_min", 8)))
                max_delay = int(settings.get("max_delay_sec", settings.get("delay_max", 18)))
                wait = random.uniform(min_delay, max_delay)
                logger.info(f"[{lead_id}] next lead delay: {wait:.0f}s")
                await asyncio.sleep(wait)

        await browser.close()

    if results:
        submissions_path = append_results(results, date_str)
    else:
        submissions_path = os.path.join(RESULTS_DIR, f"submissions_{date_str}.csv")

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
    logger.info(f"summary: {summary_path}")
    logger.info(f"log: {os.path.join(LOGS_DIR, f'{date_str}.log')}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Spiritual Salon Automation")
    parser.add_argument("--report-only", action="store_true", help="Print latest summary")
    parser.add_argument("--dry-run", action="store_true", help="Prepare only (no final submit)")
    parser.add_argument("--test", action="store_true", help="Test mode (limit=2)")
    parser.add_argument("--mode", choices=["SEMI_AUTO", "FULL_AUTO"], help="Override mode")
    args = parser.parse_args()

    if args.report_only:
        print_report_from_files(RESULTS_DIR, DATA_DIR)
        return

    overrides: Dict[str, object] = {}
    if args.dry_run:
        overrides["dry_run"] = True
        overrides["mode"] = "SEMI_AUTO"
    if args.test:
        overrides["test_mode"] = True
    if args.mode:
        overrides["mode"] = args.mode

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
