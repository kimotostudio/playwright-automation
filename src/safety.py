"""Safety guards: blocklist, domain cooldown, robots.txt, corporate filter, quiet hours."""

import csv
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")


def extract_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc or parsed.path.split("/")[0]
    except Exception:
        return url


class BlocklistManager:
    """Manages domain and URL blocklists."""

    def __init__(self, data_dir: str):
        self.domains_path = os.path.join(data_dir, "blocklist_domains.txt")
        self.urls_path = os.path.join(data_dir, "blocklist_urls.txt")
        self.blocked_domains = self._load_list(self.domains_path)
        self.blocked_urls = self._load_list(self.urls_path)

    def _load_list(self, path: str) -> Set[str]:
        """Load blocklist from file, one entry per line."""
        entries = set()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        entries.add(line.lower())
        return entries

    def is_blocked(self, url: str) -> Tuple[bool, str]:
        """Check if URL or its domain is blocked.

        Returns:
            (blocked, reason) tuple.
        """
        domain = extract_domain(url).lower()
        if domain in self.blocked_domains:
            return True, f"domain_blocked: {domain}"
        if url.lower() in self.blocked_urls:
            return True, f"url_blocked: {url}"
        return False, ""

    def add_domain(self, domain: str) -> None:
        """Add a domain to the blocklist and persist."""
        domain = domain.lower()
        if domain not in self.blocked_domains:
            self.blocked_domains.add(domain)
            with open(self.domains_path, "a", encoding="utf-8") as f:
                f.write(f"{domain}\n")
            logger.info(f"[BLOCKLIST] Domain added: {domain}")


class DomainCooldownManager:
    """Manages per-domain cooldowns for bot protection recovery."""

    def __init__(self, data_dir: str, cooldown_days: int = 7):
        self.cooldowns_path = os.path.join(data_dir, "domain_cooldowns.json")
        self.cooldown_days = cooldown_days
        self.cooldowns = self._load()

    def _load(self) -> Dict[str, str]:
        """Load cooldown timestamps from JSON."""
        if os.path.exists(self.cooldowns_path):
            try:
                with open(self.cooldowns_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, Exception):
                pass
        return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.cooldowns_path), exist_ok=True)
        with open(self.cooldowns_path, "w", encoding="utf-8") as f:
            json.dump(self.cooldowns, f, ensure_ascii=False, indent=2)

    def is_cooling(self, domain: str) -> Tuple[bool, str]:
        """Check if domain is still in cooldown period."""
        domain = domain.lower()
        if domain in self.cooldowns:
            cooldown_until = datetime.fromisoformat(self.cooldowns[domain])
            now = datetime.now(JST)
            if now < cooldown_until:
                remaining = (cooldown_until - now).days
                return True, f"domain_cooldown: {domain} ({remaining}d remaining)"
            else:
                # Cooldown expired, remove
                del self.cooldowns[domain]
                self._save()
        return False, ""

    def set_cooldown(self, domain: str) -> None:
        """Set cooldown for a domain after bot protection detection."""
        domain = domain.lower()
        until = datetime.now(JST) + timedelta(days=self.cooldown_days)
        self.cooldowns[domain] = until.isoformat()
        self._save()
        logger.info(f"[COOLDOWN] Domain {domain} cooled down until {until.strftime('%Y-%m-%d')}")


class DomainAttemptTracker:
    """Tracks per-domain daily attempts to avoid pattern detection."""

    def __init__(self, max_per_day: int = 2):
        self.max_per_day = max_per_day
        self.attempts: Dict[str, int] = {}

    def can_attempt(self, domain: str) -> bool:
        domain = domain.lower()
        return self.attempts.get(domain, 0) < self.max_per_day

    def record_attempt(self, domain: str) -> None:
        domain = domain.lower()
        self.attempts[domain] = self.attempts.get(domain, 0) + 1

    def get_count(self, domain: str) -> int:
        return self.attempts.get(domain.lower(), 0)


class SubmissionLedger:
    """Durable submission ledger to prevent duplicates even if state.json is lost."""

    FIELDNAMES = ["timestamp", "salon_id", "domain", "contact_url", "status", "reason"]

    def __init__(self, data_dir: str):
        self.ledger_path = os.path.join(data_dir, "submission_ledger.csv")
        self.submitted_ids = self._load_submitted_ids()

    def _load_submitted_ids(self) -> Set[str]:
        """Load all salon IDs that have been submitted from the ledger."""
        ids = set()
        if os.path.exists(self.ledger_path):
            try:
                with open(self.ledger_path, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get("status") == "sent":
                            ids.add(str(row.get("salon_id", "")))
            except Exception as e:
                logger.error(f"[LEDGER] Failed to load: {e}")
        logger.info(f"[LEDGER] Loaded {len(ids)} previously sent IDs")
        return ids

    def is_submitted(self, salon_id: str) -> bool:
        """Check if a salon has already been submitted to."""
        return str(salon_id) in self.submitted_ids

    def record(self, salon_id: str, domain: str, contact_url: str, status: str, reason: str) -> None:
        """Append a record to the ledger."""
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        file_exists = os.path.exists(self.ledger_path)
        with open(self.ledger_path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                "salon_id": salon_id,
                "domain": domain,
                "contact_url": contact_url,
                "status": status,
                "reason": reason,
            })
        if status == "sent":
            self.submitted_ids.add(str(salon_id))


def check_quiet_hours(start_hour: int, end_hour: int) -> Tuple[bool, str]:
    """Check if current time is within quiet hours (JST).

    Returns:
        (is_quiet, message) tuple.
    """
    now = datetime.now(JST)
    hour = now.hour
    if start_hour > end_hour:
        # Wraps midnight: e.g. 22:00 - 08:00
        is_quiet = hour >= start_hour or hour < end_hour
    else:
        is_quiet = start_hour <= hour < end_hour

    if is_quiet:
        msg = f"Quiet hours ({start_hour:02d}:00-{end_hour:02d}:00 JST). Current: {hour:02d}:{now.minute:02d}"
        return True, msg
    return False, ""


async def check_robots_txt(page, base_url: str) -> Tuple[bool, str]:
    """Check /robots.txt for crawl restrictions.

    Returns:
        (disallowed, reason) - True if site disallows contact/all crawling.
    """
    from urllib.parse import urljoin
    robots_url = urljoin(base_url.rstrip("/") + "/", "robots.txt")
    try:
        response = await page.goto(robots_url, timeout=10000, wait_until="domcontentloaded")
        if response and response.status == 200:
            text = await page.inner_text("body")
            text_lower = text.lower()
            # Check for broad disallow
            if "disallow: /" in text_lower and "user-agent: *" in text_lower:
                # Check if it's a blanket disallow (Disallow: / with no Allow:)
                lines = text_lower.split("\n")
                in_star_block = False
                for line in lines:
                    line = line.strip()
                    if line.startswith("user-agent:") and "*" in line:
                        in_star_block = True
                    elif line.startswith("user-agent:"):
                        in_star_block = False
                    elif in_star_block and line == "disallow: /":
                        return True, "robots_disallow: site disallows all crawling"
            # Check for contact-specific disallow
            for path in ["/contact", "/inquiry", "/form", "/mail"]:
                if f"disallow: {path}" in text_lower:
                    return True, f"robots_disallow: {path}"
    except Exception:
        pass
    return False, ""


def detect_bot_protection(page_text: str, status_code: Optional[int] = None) -> bool:
    """Detect if bot protection is active on the page."""
    indicators = [
        "verify you are human",
        "あなたがロボットではないことを確認",
        "cloudflare",
        "just a moment",
        "checking your browser",
        "access denied",
        "403 forbidden",
        "429 too many requests",
        "bot detection",
        "security check",
    ]
    text_lower = page_text.lower()
    if any(ind in text_lower for ind in indicators):
        return True
    if status_code in (403, 429):
        return True
    return False


def detect_corporate(page_text: str) -> Tuple[bool, str]:
    """Detect if the page belongs to a corporate entity (not a solo practitioner).

    Returns:
        (is_corporate, reason) tuple.
    """
    corporate_indicators = [
        "株式会社",
        "有限会社",
        "合同会社",
        "法人",
        "Inc.",
        "Inc",
        "LLC",
        "Ltd.",
        "Co., Ltd.",
        "代表取締役",
        "Corporation",
    ]
    for indicator in corporate_indicators:
        if indicator in page_text:
            return True, f"corporate_skip: '{indicator}' detected"
    return False, ""
