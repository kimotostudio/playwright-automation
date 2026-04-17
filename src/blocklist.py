"""Domain/URL blocklist and cooldown helpers."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DOMAIN_SEED_URL_ALIASES = [
    "URL",
    "url",
    "website",
    "site_url",
    "url(旧)",
    "old_url",
]


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


def _normalize_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("（", "(").replace("）", ")").replace("　", "")
    return normalized.replace(" ", "")


def _resolve_column(fieldnames: list[str] | None, aliases: list[str]) -> str:
    names = list(fieldnames or [])
    normalized = {_normalize_key(name): name for name in names}
    for alias in aliases:
        direct = next((name for name in names if name == alias), "")
        if direct:
            return direct
        alt = normalized.get(_normalize_key(alias), "")
        if alt:
            return alt
    return ""


def _domain_from_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(raw)
    domain = str(parsed.netloc or "").lower().strip()
    if not domain:
        return ""
    if ":" in domain:
        domain = domain.split(":", 1)[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.strip(".")


def seed_blocklist_domains_from_csv(
    csv_path: str,
    data_dir: str = DEFAULT_DATA_DIR,
    url_aliases: list[str] | None = None,
) -> Dict[str, object]:
    """Import URL domains from a CSV file into blocklist_domains.txt."""
    ensure_blocklist_files(data_dir)
    domains_path, _, _ = _paths(data_dir)
    target_csv = str(csv_path or "").strip()
    if not target_csv:
        return {"status": "file_missing", "csv_path": "", "added_count": 0}
    if not os.path.exists(target_csv):
        return {"status": "file_missing", "csv_path": target_csv, "added_count": 0}

    aliases = list(url_aliases or DOMAIN_SEED_URL_ALIASES)
    existing = _load_lines(domains_path)
    to_add: set[str] = set()
    valid_domains: set[str] = set()
    invalid_url_rows = 0
    nonempty_url_rows = 0
    total_rows = 0

    with open(target_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        url_column = _resolve_column(reader.fieldnames, aliases)
        if not url_column:
            return {
                "status": "missing_url_column",
                "csv_path": target_csv,
                "added_count": 0,
                "url_column": "",
                "total_rows": 0,
            }

        for row in reader:
            total_rows += 1
            raw_url = str((row or {}).get(url_column, "")).strip()
            if not raw_url:
                continue
            nonempty_url_rows += 1
            domain = _domain_from_url(raw_url)
            if not domain:
                invalid_url_rows += 1
                continue
            valid_domains.add(domain)
            if domain not in existing:
                to_add.add(domain)
                existing.add(domain)

    if to_add:
        with open(domains_path, "a", encoding="utf-8") as f:
            for domain in sorted(to_add):
                f.write(f"{domain}\n")

    return {
        "status": "ok",
        "csv_path": target_csv,
        "url_column": url_column,
        "total_rows": total_rows,
        "nonempty_url_rows": nonempty_url_rows,
        "invalid_url_rows": invalid_url_rows,
        "valid_domain_count": len(valid_domains),
        "added_count": len(to_add),
    }


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
