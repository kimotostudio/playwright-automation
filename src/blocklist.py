"""Domain/URL blocklist and cooldown helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def extract_domain(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path.split("/")[0]
    return domain.lower().strip()


def _paths(data_dir: str) -> tuple[str, str, str]:
    return (
        os.path.join(data_dir, "blocklist_domains.txt"),
        os.path.join(data_dir, "blocklist_urls.txt"),
        os.path.join(data_dir, "domain_cooldowns.json"),
    )


def ensure_blocklist_files(data_dir: str = DEFAULT_DATA_DIR) -> None:
    os.makedirs(data_dir, exist_ok=True)
    domains_path, urls_path, cooldown_path = _paths(data_dir)

    if not os.path.exists(domains_path):
        with open(domains_path, "w", encoding="utf-8") as f:
            f.write("# blocked domains\n")

    if not os.path.exists(urls_path):
        with open(urls_path, "w", encoding="utf-8") as f:
            f.write("# blocked urls\n")

    if not os.path.exists(cooldown_path):
        with open(cooldown_path, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)


def _load_lines(path: str) -> set[str]:
    rows = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                value = line.strip().lower()
                if value and not value.startswith("#"):
                    rows.add(value)
    return rows


def _load_cooldowns(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _save_cooldowns(path: str, payload: dict) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def is_blocked(domain: str, url: str, data_dir: str = DEFAULT_DATA_DIR) -> Tuple[bool, str]:
    """Check domain/url blocklists and active domain cooldowns (JST)."""
    ensure_blocklist_files(data_dir)
    domains_path, urls_path, cooldown_path = _paths(data_dir)

    target_domain = (domain or "").lower().strip()
    target_url = (url or "").lower().strip()

    blocked_domains = _load_lines(domains_path)
    blocked_urls = _load_lines(urls_path)
    cooldowns = _load_cooldowns(cooldown_path)

    for blocked in blocked_domains:
        if target_domain == blocked or target_domain.endswith(f".{blocked}"):
            return True, f"blocked_domain:{blocked}"

    if target_url in blocked_urls:
        return True, "blocked_url"

    now = datetime.now(JST)
    cooldown = cooldowns.get(target_domain)
    if isinstance(cooldown, dict) and cooldown.get("until"):
        try:
            until = datetime.fromisoformat(str(cooldown["until"]))
            if now < until:
                return True, f"domain_cooldown_until:{until.isoformat()}"
        except ValueError:
            pass

    return False, ""


def block_domain(
    domain: str,
    days: int = 7,
    reason: str = "bot_protection",
    data_dir: str = DEFAULT_DATA_DIR,
) -> dict:
    """Add domain to blocklist and set/refresh cooldown."""
    ensure_blocklist_files(data_dir)
    domains_path, _, cooldown_path = _paths(data_dir)

    target_domain = (domain or "").lower().strip()
    if not target_domain:
        return {}

    blocked_domains = _load_lines(domains_path)
    if target_domain not in blocked_domains:
        with open(domains_path, "a", encoding="utf-8") as f:
            f.write(f"{target_domain}\n")

    cooldowns = _load_cooldowns(cooldown_path)
    until = datetime.now(JST) + timedelta(days=days)
    cooldowns[target_domain] = {"until": until.isoformat(), "reason": reason}
    _save_cooldowns(cooldown_path, cooldowns)
    return cooldowns[target_domain]
