"""Manual submit helper for SEMI_AUTO prepared leads.

Usage:
  python src/resume_submit.py --salon-id 1100
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import os
import sys
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from playwright.async_api import Page, async_playwright

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.form_detector import FormDetector
from src.ledger import append_ledger, ledger_has, read_ledger
from src.rate_limiter import RateLimiter
from src.review_queue import find_prepared_entry, update_review_status
from src.blocklist import extract_domain

JST = ZoneInfo("Asia/Tokyo")

CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
SCREENSHOTS_DIR = os.path.join(PROJECT_ROOT, "screenshots")

SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
SENDER_INFO_PATH = os.path.join(CONFIG_DIR, "sender_info.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LEADS_PATH = os.path.join(DATA_DIR, "leads.csv")
LEDGER_PATH = os.path.join(DATA_DIR, "submission_ledger.csv")


def load_json(path: str, fallback: dict) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return fallback.copy()


def load_lead_ids(leads_path: str) -> set[str]:
    if not os.path.exists(leads_path):
        return set()
    ids = set()
    with open(leads_path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            value = str(row.get("id", "")).strip()
            if value:
                ids.add(value)
    return ids


def _read_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def update_results_to_sent(salon_id: str, notes: str) -> Optional[str]:
    """Update latest prepared row in submissions CSV; append if absent."""
    candidates = sorted(glob.glob(os.path.join(RESULTS_DIR, "submissions_*.csv")), reverse=True)
    for path in candidates:
        rows = _read_rows(path)
        if not rows:
            continue

        fieldnames = list(rows[0].keys())
        updated = False
        for idx in range(len(rows) - 1, -1, -1):
            row = rows[idx]
            if str(row.get("salon_id", "")).strip() != salon_id:
                continue
            if str(row.get("status", "")).strip().lower() in {"prepared", "dry_run"}:
                row["status"] = "sent"
                row["message"] = notes
                row["timestamp"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
                updated = True
                break

        if updated:
            _write_rows(path, rows, fieldnames)
            return path

    return None


async def find_submit_button(page: Page, stored_selector: str) -> Tuple[Optional[object], str]:
    if stored_selector:
        try:
            loc = page.locator(stored_selector)
            if await loc.count() > 0 and await loc.first.is_visible():
                return loc.first, stored_selector
        except Exception:
            pass

    fallback_selectors = [
        "button:has-text('送信')",
        "button:has-text('送信する')",
        "button:has-text('送信内容を送信')",
        "button:has-text('確定')",
        "button:has-text('この内容で送信')",
        "input[value='送信']",
        "input[value='送信する']",
        "input[value='確定']",
    ]

    for selector in fallback_selectors:
        try:
            loc = page.locator(selector)
            if await loc.count() > 0 and await loc.first.is_visible():
                return loc.first, selector
        except Exception:
            continue

    return None, ""


async def submit_prepared(entry: dict, settings: dict, sender_info: dict) -> Tuple[bool, str, str]:
    salon_id = str(entry.get("salon_id", "")).strip()
    final_step_url = (entry.get("final_step_url") or entry.get("contact_url") or "").strip()
    submit_selector = (entry.get("submit_selector") or "").strip()

    if not final_step_url:
        return False, "", "final_step_url missing"

    folder = entry.get("screenshot_folder") or os.path.join("screenshots", datetime.now(JST).strftime("%Y%m%d"))
    abs_folder = os.path.join(PROJECT_ROOT, folder)
    os.makedirs(abs_folder, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={
                "width": settings.get("browser", {}).get("viewport_width", 1280),
                "height": settings.get("browser", {}).get("viewport_height", 720),
            },
            user_agent=settings.get("browser", {}).get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ),
            locale=settings.get("browser", {}).get("locale", "ja-JP"),
        )
        page = await context.new_page()

        await page.goto(final_step_url, wait_until="domcontentloaded", timeout=int(settings.get("timeout_seconds", 30)) * 1000)
        await asyncio.sleep(2)

        detector = FormDetector(page, sender_info, timeout=int(settings.get("timeout_seconds", 30)))
        button, selector_used = await find_submit_button(page, submit_selector)

        if not button:
            # Last fallback: use detector generic logic.
            auto_button, auto_selector, _ = await detector.find_submit_button()
            button, selector_used = auto_button, auto_selector

        if not button:
            await browser.close()
            return False, "", "submit button not found"

        print("\n================ MANUAL SUBMIT WARNING ================")
        print(f"Salon ID     : {salon_id}")
        print(f"Salon Name   : {entry.get('salon_name', '')}")
        print(f"Final URL    : {page.url}")
        print(f"ButtonSelector: {selector_used}")
        print("この操作は実際にフォーム送信を実行します。")
        print("Enter で送信 / Ctrl+C で中止")
        input("Press ENTER to submit: ")

        await button.click()
        await asyncio.sleep(2)

        # handle optional additional confirmation step
        await detector.handle_confirmation_page()
        await asyncio.sleep(2)

        screenshot_path = os.path.join(abs_folder, f"{salon_id}_04_after_submit.png")
        await page.screenshot(path=screenshot_path, full_page=True)

        text = await page.inner_text("body")
        success_markers = ["ありがとうございました", "送信しました", "受け付けました", "完了", "Thank you", "success"]
        ok = any(marker in text for marker in success_markers)

        final_url = page.url
        await browser.close()
        return ok, selector_used, final_url


async def async_main(args: argparse.Namespace) -> int:
    salon_id = str(args.salon_id).strip()
    settings = load_json(SETTINGS_PATH, {})
    leads_path = str(settings.get("leads_csv_path", LEADS_PATH))

    if salon_id not in load_lead_ids(leads_path):
        print(f"salon_id={salon_id} is not in {leads_path}")
        return 1

    # Robust anti-duplicate check (state + ledger)
    state = load_json(STATE_PATH, {"completed_ids": []})
    completed_ids = {str(x) for x in state.get("completed_ids", [])}
    if salon_id in completed_ids or ledger_has(salon_id, LEDGER_PATH):
        print(f"salon_id={salon_id} is already sent (state/ledger).")
        return 1

    entry, queue_path = find_prepared_entry(salon_id, results_dir=RESULTS_DIR, queue_file=args.queue_file)
    if not entry:
        print(f"No prepared entry found for salon_id={salon_id}")
        return 1

    sender_info = load_json(SENDER_INFO_PATH, {})

    ok = False
    selector_used = ""
    final_url = ""
    reason = ""

    try:
        ok, selector_used, final_url = await submit_prepared(entry, settings, sender_info)
    except KeyboardInterrupt:
        reason = "manual_cancel"
    except Exception as e:
        reason = f"exception:{str(e)[:140]}"

    domain = extract_domain(entry.get("contact_url") or entry.get("final_step_url") or "")

    if ok:
        append_ledger(
            {
                "run_mode": "MANUAL_SUBMIT",
                "salon_id": salon_id,
                "salon_name": entry.get("salon_name", ""),
                "domain": domain,
                "contact_url": entry.get("contact_url", ""),
                "final_step_url": final_url or entry.get("final_step_url", ""),
                "status": "sent",
                "reason": f"manual_submit:{selector_used}",
            },
            path=LEDGER_PATH,
        )

        ledger_index = read_ledger(LEDGER_PATH)
        limiter = RateLimiter(
            state_path=STATE_PATH,
            daily_limit=int(settings.get("daily_limit", 20)),
            ledger_ids=ledger_index.get("sent_ids", set()),
        )
        limiter.record_submission(salon_id)

        update_review_status(
            salon_id=salon_id,
            status="sent",
            notes=f"manual_submit_done selector={selector_used}",
            results_dir=RESULTS_DIR,
            queue_file=queue_path,
        )
        result_file = update_results_to_sent(salon_id, "sent via manual_submit")

        print("\nManual submit completed.")
        print(f"salon_id: {salon_id}")
        print(f"final_url: {final_url}")
        print(f"selector: {selector_used}")
        if result_file:
            print(f"updated results: {result_file}")
        return 0

    failure_reason = reason or "manual_submit_failed"
    append_ledger(
        {
            "run_mode": "MANUAL_SUBMIT",
            "salon_id": salon_id,
            "salon_name": entry.get("salon_name", ""),
            "domain": domain,
            "contact_url": entry.get("contact_url", ""),
            "final_step_url": entry.get("final_step_url", ""),
            "status": "failed",
            "reason": failure_reason,
        },
        path=LEDGER_PATH,
    )

    print(f"Manual submit failed: {failure_reason}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual submit helper")
    parser.add_argument("--salon-id", required=True, help="target salon id")
    parser.add_argument("--queue-file", help="optional review queue path")
    args = parser.parse_args()

    code = asyncio.run(async_main(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
