#!/usr/bin/env python3
"""Prepare and optionally run a controlled SEMI_AUTO review batch."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / ".." / "demo-generator" / "output" / "handoff_with_demo_paths.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "handoff_review_batch.csv"
RESULTS_DIR = PROJECT_ROOT / "results"
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
LOGS_DIR = RESULTS_DIR / "logs"
LEDGER_PATH = PROJECT_ROOT / "data" / "submission_ledger.csv"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
SENDER_INFO_PATH = PROJECT_ROOT / "config" / "sender_info.json"

DISPLAY_NAME_ALIASES = [
    "display_name",
    "表示名",
    "salon_name",
    "business_name",
    "brand_name",
    "company_name",
    "店名",
    "名称",
    "サロン名",
    "店舗名",
    "name",
]
DEMO_URL_ALIASES = ["demo_url", "url(デモ)", "url(デモページ)", "url_demo", "demo_path", "demo"]
DOMAIN_ALIASES = ["domain", "original__domain"]
LEAD_ID_ALIASES = ["id", "ID", "lead_id", "leadid", "管理番号"]
URL_ALIASES = ["contact_url", "contact_page", "website", "url", "reference_url", "original__url"]
LOW_CONFIDENCE_VALUES = {"low", "unknown", "uncertain", "domain_fallback"}
PRIVATE_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
NAME_CONFIDENCE_RANK = {
    "": 0,
    "low": 0,
    "unknown": 0,
    "uncertain": 0,
    "domain_fallback": 0,
    "medium": 1,
    "med": 1,
    "high": 2,
}
CORPORATE_TOKENS = [
    "corporate",
    "corporation",
    "inc.",
    " inc",
    "ltd.",
    "株式会社",
    "有限会社",
    "合同会社",
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
PORTAL_LISTING_HOSTS = [
    "beauty.hotpepper.jp",
    "hotpepper.jp",
    "minimodel.jp",
    "epark.jp",
    "ekiten.jp",
    "findglocal.com",
    "select-type.com",
]
PORTAL_LISTING_TOKENS = [
    "ポータル",
    "掲載",
    "広告",
    "媒体",
    "ホットペッパー",
    "portal",
    "listing",
    "directory",
    "media",
    "advertis",
    "hotpepper",
]


@dataclass
class Candidate:
    row: dict[str, str]
    index: int
    lead_id: str
    domain: str
    display_name: str
    demo_url: str
    contact_url: str
    contact_host: str
    name_confidence: str
    low_confidence_name: bool
    public_https_demo_url: bool
    source_domain_count: int
    direct_contact_url: bool
    corporate_like: bool
    line_or_sns_contact: bool
    portal_listing_like: bool
    weak_contact_url: bool
    quality_issues: tuple[str, ...]

    def score(self) -> tuple[int, ...]:
        return (
            1 if self.public_https_demo_url else 0,
            1 if self.direct_contact_url else 0,
            1 if not self.corporate_like else 0,
            1 if not self.line_or_sns_contact else 0,
            1 if not self.portal_listing_like else 0,
            1 if not self.weak_contact_url else 0,
            1 if self.display_name else 0,
            _name_confidence_rank(self.name_confidence),
            1 if self.source_domain_count == 1 else 0,
            1 if not self.low_confidence_name else 0,
            -self.index,
        )


def _norm_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("（", "(").replace("）", ")").replace("　", "")
    return normalized.replace(" ", "")


def _pick(row: dict[str, str], keys: Iterable[str]) -> str:
    normalized = {_norm_key(k): v for k, v in row.items()}
    for key in keys:
        direct = row.get(key)
        if direct is not None and str(direct).strip():
            return str(direct).strip()
        alt = normalized.get(_norm_key(key))
        if alt is not None and str(alt).strip():
            return str(alt).strip()
    return ""


def _normalize_domain(value: str) -> str:
    domain = str(value or "").strip().lower()
    if not domain:
        return ""
    if "://" in domain:
        domain = urlparse(domain).netloc
    domain = domain.split("/", 1)[0].split(":", 1)[0].strip(".").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _url_host(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    return (parsed.hostname or "").lower()


def _same_site(a: str, b: str) -> bool:
    da = _normalize_domain(a)
    db = _normalize_domain(b)
    return bool(da and db and (da == db or da.endswith(f".{db}") or db.endswith(f".{da}")))


def _domain_from_row(row: dict[str, str]) -> str:
    explicit = _normalize_domain(_pick(row, DOMAIN_ALIASES))
    if explicit:
        return explicit
    for key in URL_ALIASES:
        raw = _pick(row, [key])
        if raw and raw.lower().startswith(("http://", "https://")):
            parsed = urlparse(raw)
            domain = _normalize_domain(parsed.netloc)
            if domain:
                return domain
    return ""


def _domain_matches(domain: str, blocked: Iterable[str]) -> bool:
    target = _normalize_domain(domain)
    for item in blocked:
        blocked_domain = _normalize_domain(item)
        if blocked_domain and (target == blocked_domain or target.endswith(f".{blocked_domain}")):
            return True
    return False


def _is_public_https_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in PRIVATE_HOSTS or host.endswith(".local"):
        return False
    if host.startswith("10.") or host.startswith("192.168."):
        return False
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return False
    return True


def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "有", "あり"}


def _name_confidence(row: dict[str, str]) -> str:
    return _pick(row, ["name_confidence"]).strip().lower()


def _name_confidence_rank(value: str) -> int:
    return NAME_CONFIDENCE_RANK.get(str(value or "").strip().lower(), 0)


def _low_confidence_name(row: dict[str, str], display_name: str) -> bool:
    confidence = _name_confidence(row)
    warning = _pick(row, ["name_warning"]).strip()
    if not display_name:
        return True
    return confidence in LOW_CONFIDENCE_VALUES or bool(warning)


def _contact_url_from_row(row: dict[str, str]) -> str:
    return _pick(row, ["contact_url", "contact_page", "original__contact_url", "original__form_url", "website", "url"])


def _contains_any(text: str, tokens: Iterable[str]) -> bool:
    haystack = str(text or "").lower()
    return any(token.lower() in haystack for token in tokens)


def _combined_row_text(row: dict[str, str]) -> str:
    keys = [
        "display_name",
        "business_name",
        "company_name",
        "salon_name",
        "brand_name",
        "notes",
        "reason",
        "original__title",
        "original__category_guess",
        "original__reason",
        "website",
        "url",
        "contact_url",
        "contact_page",
        "original__contact_url",
        "original__form_url",
    ]
    return " ".join(str(row.get(key, "")) for key in keys)


def _is_corporate_like(row: dict[str, str]) -> bool:
    return _contains_any(_combined_row_text(row), CORPORATE_TOKENS)


def _host_matches(host: str, known_hosts: Iterable[str]) -> bool:
    normalized = _normalize_domain(host)
    return any(normalized == item or normalized.endswith(f".{item}") for item in known_hosts)


def _is_line_or_sns_contact(contact_url: str) -> bool:
    return _host_matches(_url_host(contact_url), SOCIAL_OR_EXTERNAL_CONTACT_HOSTS)


def _is_portal_listing_like(row: dict[str, str], contact_url: str) -> bool:
    return _host_matches(_url_host(contact_url), PORTAL_LISTING_HOSTS) or _contains_any(
        _combined_row_text(row),
        PORTAL_LISTING_TOKENS,
    )


def _is_direct_contact_url(domain: str, contact_url: str) -> bool:
    parsed = urlparse(str(contact_url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    return _same_site(parsed.hostname or "", domain)


def _is_root_url(contact_url: str) -> bool:
    parsed = urlparse(str(contact_url or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and (parsed.path or "/") == "/" and not parsed.query


def _weak_contact_url(row: dict[str, str], domain: str, contact_url: str, direct_contact_url: bool) -> bool:
    if not contact_url or not contact_url.lower().startswith(("http://", "https://")):
        return True
    if not direct_contact_url:
        return True
    if _is_root_url(contact_url) and not _is_truthy(_pick(row, ["original__has_form", "has_form"])):
        return True
    return False


def _quality_issues(
    *,
    low_confidence_name: bool,
    corporate_like: bool,
    line_or_sns_contact: bool,
    portal_listing_like: bool,
    weak_contact_url: bool,
) -> tuple[str, ...]:
    issues: list[str] = []
    if corporate_like:
        issues.append("corporate_like")
    if line_or_sns_contact:
        issues.append("line_or_sns_contact")
    if portal_listing_like:
        issues.append("portal_listing_like")
    if weak_contact_url:
        issues.append("weak_contact_url")
    if low_confidence_name:
        issues.append("low_confidence_display_name")
    return tuple(issues)


def _candidate_from_row(row: dict[str, str], index: int, domain_counts: Counter[str]) -> Candidate | None:
    lead_id = _pick(row, LEAD_ID_ALIASES)
    domain = _domain_from_row(row)
    if not domain:
        return None
    display_name = _pick(row, DISPLAY_NAME_ALIASES)
    demo_url = _pick(row, DEMO_URL_ALIASES)
    contact_url = _contact_url_from_row(row)
    contact_host = _url_host(contact_url)
    name_confidence = _name_confidence(row)
    low_confidence = _low_confidence_name(row, display_name)
    direct_contact = _is_direct_contact_url(domain, contact_url)
    corporate_like = _is_corporate_like(row)
    line_or_sns = _is_line_or_sns_contact(contact_url)
    portal_listing = _is_portal_listing_like(row, contact_url)
    weak_contact = _weak_contact_url(row, domain, contact_url, direct_contact)
    issues = _quality_issues(
        low_confidence_name=low_confidence,
        corporate_like=corporate_like,
        line_or_sns_contact=line_or_sns,
        portal_listing_like=portal_listing,
        weak_contact_url=weak_contact,
    )
    return Candidate(
        row=row,
        index=index,
        lead_id=lead_id,
        domain=domain,
        display_name=display_name,
        demo_url=demo_url,
        contact_url=contact_url,
        contact_host=contact_host,
        name_confidence=name_confidence,
        low_confidence_name=low_confidence,
        public_https_demo_url=_is_public_https_url(demo_url),
        source_domain_count=domain_counts[domain],
        direct_contact_url=direct_contact,
        corporate_like=corporate_like,
        line_or_sns_contact=line_or_sns,
        portal_listing_like=portal_listing,
        weak_contact_url=weak_contact,
        quality_issues=issues,
    )


def _read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_ledger_keys(path: Path) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    domains: set[str] = set()
    if not path.exists():
        return ids, domains
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            lead_id = str(row.get("salon_id", "")).strip()
            domain = _normalize_domain(str(row.get("domain", "")).strip())
            if lead_id:
                ids.add(lead_id)
            if domain:
                domains.add(domain)
    return ids, domains


def _read_feedback_exclusions(path: Path) -> tuple[set[str], set[str], Counter[str]]:
    ids: set[str] = set()
    domains: set[str] = set()
    reasons: Counter[str] = Counter()
    if not path.exists():
        return ids, domains, reasons
    exclude_tokens = {
        "bot_or_protection_page",
        "corporate_or_large_business",
        "external_line_or_sns_page",
        "iframe_only_form",
        "listing_or_media_form",
        "no_form_fields",
        "unsuitable_contact_target",
    }
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            action = str(row.get("lead_finder_recommended_action", "")).strip()
            joined = " ".join(
                str(row.get(key, ""))
                for key in [
                    "lead_finder_exclusion_reason",
                    "lead_quality_issue",
                    "contact_quality_issue",
                    "form_quality_issue",
                    "feedback_for_lead_finder",
                ]
            )
            matched = [token for token in exclude_tokens if token in joined]
            if action != "exclude" and not matched:
                continue
            lead_id = str(row.get("lead_id", "")).strip()
            domain = _normalize_domain(str(row.get("domain", "")).strip())
            if lead_id:
                ids.add(lead_id)
            if domain:
                domains.add(domain)
            if matched:
                for token in matched:
                    reasons[token] += 1
            elif action:
                reasons[action] += 1
    return ids, domains, reasons


def _load_settings_skip_domains() -> set[str]:
    if not SETTINGS_PATH.exists():
        return set()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return set()
    values = data.get("skip_domains", [])
    if not isinstance(values, list):
        return set()
    return {_normalize_domain(str(value)) for value in values if str(value).strip()}


def _select_candidates(
    rows: list[dict[str, str]],
    limit: int,
    exclude_domains: set[str],
    include_ledger_domains: bool,
    avoid_settings_skip_domains: bool,
    exclude_corporate: bool,
    exclude_line_domains: bool,
    exclude_portal_listing: bool,
    min_name_confidence: str,
    prefer_direct_contact_url: bool,
    feedback_exclude_ids: set[str],
    feedback_exclude_domains: set[str],
) -> tuple[list[Candidate], Counter[str]]:
    skipped: Counter[str] = Counter()
    ledger_ids, ledger_domains = _read_ledger_keys(LEDGER_PATH)
    settings_skip_domains = _load_settings_skip_domains() if avoid_settings_skip_domains else set()
    domain_counts = Counter(_domain_from_row(row) for row in rows)
    min_name_rank = _name_confidence_rank(min_name_confidence)

    candidates: list[Candidate] = []
    for index, row in enumerate(rows, 1):
        candidate = _candidate_from_row(row, index, domain_counts)
        if candidate is None:
            skipped["missing_domain"] += 1
            continue
        if _domain_matches(candidate.domain, exclude_domains):
            skipped["excluded_domain_arg"] += 1
            continue
        if avoid_settings_skip_domains and _domain_matches(candidate.domain, settings_skip_domains):
            skipped["settings_skip_domain"] += 1
            continue
        if candidate.lead_id in feedback_exclude_ids or _domain_matches(candidate.domain, feedback_exclude_domains):
            skipped["lead_quality_feedback_excluded"] += 1
            continue
        if not include_ledger_domains and (
            candidate.lead_id in ledger_ids or _domain_matches(candidate.domain, ledger_domains)
        ):
            skipped["already_in_ledger"] += 1
            continue
        if exclude_corporate and candidate.corporate_like:
            skipped["corporate_like"] += 1
            continue
        if exclude_line_domains and candidate.line_or_sns_contact:
            skipped["line_or_sns_contact"] += 1
            continue
        if exclude_portal_listing and candidate.portal_listing_like:
            skipped["portal_listing_like"] += 1
            continue
        if prefer_direct_contact_url and candidate.weak_contact_url:
            skipped["weak_contact_url"] += 1
            continue
        if min_name_rank > 0 and _name_confidence_rank(candidate.name_confidence) < min_name_rank:
            skipped["below_min_name_confidence"] += 1
            continue

        candidates.append(candidate)

    selected: list[Candidate] = []
    selected_domains: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.score(), reverse=True):
        if len(selected) >= limit:
            break
        if candidate.domain in selected_domains:
            skipped["duplicate_domain"] += 1
            continue
        selected.append(candidate)
        selected_domains.add(candidate.domain)

    selected.sort(key=lambda item: item.index)
    return selected, skipped


def _write_selection_preview(path: Path, selected: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "source_row",
        "id",
        "domain",
        "display_name_present",
        "public_https_demo_url",
        "low_confidence_name",
        "name_confidence",
        "direct_contact_url",
        "corporate_like",
        "line_or_sns_contact",
        "portal_listing_like",
        "weak_contact_url",
        "quality_issues",
        "demo_url_present",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for item in selected:
            writer.writerow(
                {
                    "source_row": item.index,
                    "id": item.lead_id,
                    "domain": item.domain,
                    "display_name_present": "1" if item.display_name else "0",
                    "public_https_demo_url": "1" if item.public_https_demo_url else "0",
                    "low_confidence_name": "1" if item.low_confidence_name else "0",
                    "name_confidence": item.name_confidence,
                    "direct_contact_url": "1" if item.direct_contact_url else "0",
                    "corporate_like": "1" if item.corporate_like else "0",
                    "line_or_sns_contact": "1" if item.line_or_sns_contact else "0",
                    "portal_listing_like": "1" if item.portal_listing_like else "0",
                    "weak_contact_url": "1" if item.weak_contact_url else "0",
                    "quality_issues": ";".join(item.quality_issues),
                    "demo_url_present": "1" if item.demo_url else "0",
                }
            )


def _write_selection_audit(
    path: Path,
    rows: list[dict[str, str]],
    selected: list[Candidate],
    feedback_exclude_ids: set[str] | None = None,
    feedback_exclude_domains: set[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    selected_ids = {item.lead_id for item in selected}
    feedback_exclude_ids = feedback_exclude_ids or set()
    feedback_exclude_domains = feedback_exclude_domains or set()
    domain_counts = Counter(_domain_from_row(row) for row in rows)
    headers = [
        "source_row",
        "selected",
        "id",
        "domain",
        "contact_host",
        "name_confidence",
        "direct_contact_url",
        "corporate_like",
        "line_or_sns_contact",
        "portal_listing_like",
        "weak_contact_url",
        "low_confidence_name",
        "feedback_excluded",
        "quality_issues",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            candidate = _candidate_from_row(row, index, domain_counts)
            if candidate is None:
                writer.writerow(
                    {
                        "source_row": index,
                        "selected": "0",
                        "id": _pick(row, LEAD_ID_ALIASES),
                        "domain": "",
                        "feedback_excluded": "0",
                        "quality_issues": "missing_domain",
                    }
                )
                continue
            feedback_excluded = (
                candidate.lead_id in feedback_exclude_ids
                or _domain_matches(candidate.domain, feedback_exclude_domains)
            )
            writer.writerow(
                {
                    "source_row": candidate.index,
                    "selected": "1" if candidate.lead_id in selected_ids else "0",
                    "id": candidate.lead_id,
                    "domain": candidate.domain,
                    "contact_host": candidate.contact_host,
                    "name_confidence": candidate.name_confidence,
                    "direct_contact_url": "1" if candidate.direct_contact_url else "0",
                    "corporate_like": "1" if candidate.corporate_like else "0",
                    "line_or_sns_contact": "1" if candidate.line_or_sns_contact else "0",
                    "portal_listing_like": "1" if candidate.portal_listing_like else "0",
                    "weak_contact_url": "1" if candidate.weak_contact_url else "0",
                    "low_confidence_name": "1" if candidate.low_confidence_name else "0",
                    "feedback_excluded": "1" if feedback_excluded else "0",
                    "quality_issues": ";".join(candidate.quality_issues),
                }
            )


def _run_preflight(batch_path: Path, first_message_path: Path, preflight_log_path: Path) -> tuple[bool, int]:
    cmd = [
        sys.executable,
        "tools/preflight_handoff_csv.py",
        "--input",
        str(batch_path),
        "--require-http-demo-url",
        "--require-display-name",
        "--require-demo-placeholder",
        "--require-display-placeholder",
        "--write-first-message",
        str(first_message_path),
    ]
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    preflight_log_path.parent.mkdir(parents=True, exist_ok=True)
    preflight_log_path.write_text(proc.stdout, encoding="utf-8")
    ready = proc.returncode == 0 and "status=ready" in proc.stdout.splitlines()
    return ready, proc.returncode


def _run_semi_auto(limit: int, batch_path: Path) -> int:
    cmd = [
        ".venv/bin/python",
        "src/main.py",
        "--mode",
        "SEMI_AUTO",
        "--semi-auto-verify",
        "--semi-auto-limit",
        str(limit),
        "--limit",
        str(limit),
        "--input",
        os.path.relpath(batch_path, PROJECT_ROOT),
    ]
    if "FULL_AUTO" in cmd:
        raise RuntimeError("Refusing to run a command containing FULL_AUTO.")
    return subprocess.run(cmd, cwd=PROJECT_ROOT, check=False).returncode


def _command_text(limit: int, batch_path: Path) -> str:
    cmd = [
        ".venv/bin/python",
        "src/main.py",
        "--mode",
        "SEMI_AUTO",
        "--semi-auto-verify",
        "--semi-auto-limit",
        str(limit),
        "--limit",
        str(limit),
        "--input",
        os.path.relpath(batch_path, PROJECT_ROOT),
    ]
    return " ".join(shlex.quote(part) for part in cmd)


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


def _rows_since(path: Path, selected_ids: set[str], start_time: datetime) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            lead_id = str(row.get("salon_id", "") or row.get("id", "")).strip()
            if lead_id not in selected_ids:
                continue
            row_time = _parse_timestamp(str(row.get("timestamp", "")))
            if row_time is not None and row_time < start_time:
                continue
            rows.append(row)
    return rows


def _inspect_artifacts(selected: list[Candidate], start_time: datetime) -> dict[str, object]:
    date_keys = {
        start_time.astimezone(JST).strftime("%Y%m%d"),
        datetime.now(JST).strftime("%Y%m%d"),
    }
    selected_ids = {item.lead_id for item in selected if item.lead_id}
    submissions: list[dict[str, str]] = []
    reviews: list[dict[str, str]] = []
    submissions_paths: list[str] = []
    review_paths: list[str] = []
    summary_paths: list[str] = []
    log_paths: list[str] = []
    screenshot_paths: list[str] = []

    for date_key in sorted(date_keys):
        submission_path = RESULTS_DIR / f"submissions_{date_key}.csv"
        review_path = RESULTS_DIR / f"review_queue_{date_key}.csv"
        summary_path = RESULTS_DIR / f"summary_{date_key}.md"
        log_path = LOGS_DIR / f"{date_key}.log"
        if submission_path.exists():
            submissions_paths.append(str(submission_path))
            submissions.extend(_rows_since(submission_path, selected_ids, start_time))
        if review_path.exists():
            review_paths.append(str(review_path))
            reviews.extend(_rows_since(review_path, selected_ids, start_time))
        if summary_path.exists():
            summary_paths.append(str(summary_path))
        if log_path.exists():
            log_paths.append(str(log_path))
        day_screenshots = SCREENSHOTS_DIR / date_key
        for item in selected:
            if day_screenshots.exists():
                screenshot_paths.extend(str(p) for p in sorted(day_screenshots.glob(f"{item.lead_id}_*.png")))
            root_confirm = SCREENSHOTS_DIR / f"{item.lead_id}_confirm.png"
            if root_confirm.exists():
                screenshot_paths.append(str(root_confirm))

    status_counts = Counter(str(row.get("status", "")).strip() or "unknown" for row in submissions)
    reason_counts = Counter(str(row.get("message", "") or row.get("reason", "")).strip() or "unknown" for row in submissions)
    sent_count = status_counts.get("sent", 0)
    success_count = status_counts.get("prepared_full", 0)
    partial_count = status_counts.get("prepared_partial", 0) + status_counts.get("prepared_external", 0)
    manual_count = sum(
        count
        for status, count in status_counts.items()
        if status in {"prepared_review_needed", "skipped", "skipped_login", "skipped_bot_protection", "skipped_dead_site", "failed"}
    )

    return {
        "submissions": submissions,
        "reviews": reviews,
        "status_counts": status_counts,
        "reason_counts": reason_counts,
        "success_count": success_count,
        "partial_count": partial_count,
        "manual_count": manual_count,
        "sent_count": sent_count,
        "submissions_paths": sorted(set(submissions_paths)),
        "review_paths": sorted(set(review_paths)),
        "summary_paths": sorted(set(summary_paths)),
        "log_paths": sorted(set(log_paths)),
        "screenshot_paths": sorted(set(screenshot_paths)),
    }


def _sender_info_git_state() -> tuple[bool, bool]:
    ignored = False
    gitignore_path = PROJECT_ROOT / ".gitignore"
    if gitignore_path.exists():
        ignored = "config/sender_info.json" in gitignore_path.read_text(encoding="utf-8-sig")

    tracked_proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "config/sender_info.json"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return ignored, tracked_proc.returncode == 0


def _send_discord_notification(enabled: bool) -> str:
    if not enabled:
        return "disabled"
    notify_path = Path.home() / "bin" / "notify-discord"
    if not notify_path.exists():
        return "not_available"
    cmd = 'source ~/.bashrc && ~/bin/notify-discord "\\u2705 SEMI_AUTO review batch finished. Please check the terminal."'
    proc = subprocess.run(["bash", "-lc", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
    return "sent" if proc.returncode == 0 else f"failed_exit_{proc.returncode}"


def _write_summary(
    path: Path,
    *,
    source_path: Path,
    batch_path: Path,
    preview_path: Path,
    audit_path: Path,
    first_message_path: Path,
    preflight_log_path: Path,
    preflight_ready: bool,
    preflight_return_code: int,
    selected: list[Candidate],
    skipped: Counter[str],
    run_requested: bool,
    run_return_code: int | None,
    artifact_info: dict[str, object],
    notification_status: str,
    sender_ignored: bool,
    sender_tracked: bool,
    start_time: datetime,
    selection_options: dict[str, object],
) -> None:
    status_counts: Counter[str] = artifact_info.get("status_counts", Counter())  # type: ignore[assignment]
    reason_counts: Counter[str] = artifact_info.get("reason_counts", Counter())  # type: ignore[assignment]
    sent_count = int(artifact_info.get("sent_count", 0) or 0)
    final_submission_avoided = sent_count == 0
    public_https_count = sum(1 for item in selected if item.public_https_demo_url)
    low_conf_count = sum(1 for item in selected if item.low_confidence_name)

    lines = [
        f"# SEMI_AUTO Review Batch Summary - {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "## Safety",
        "",
        "- mode: SEMI_AUTO",
        "- full_auto_used: no",
        "- final_submission_avoided: " + ("yes" if final_submission_avoided else "no"),
        "- sent_rows_detected: " + str(sent_count),
        "- sender_info_ignored: " + ("yes" if sender_ignored else "no"),
        "- sender_info_tracked: " + ("yes" if sender_tracked else "no"),
        "",
        "## Batch",
        "",
        f"- source_csv: {source_path}",
        f"- batch_csv: {batch_path}",
        f"- selected_rows: {len(selected)}",
        f"- public_https_demo_rows: {public_https_count}",
        f"- low_confidence_name_rows: {low_conf_count}",
        f"- selection_preview: {preview_path}",
        f"- selection_audit: {audit_path}",
        f"- first_message_preview: {first_message_path}",
        "",
        "## Quality Filters",
        "",
        f"- exclude_corporate: {selection_options.get('exclude_corporate')}",
        f"- exclude_line_domains: {selection_options.get('exclude_line_domains')}",
        f"- exclude_portal_listing: {selection_options.get('exclude_portal_listing')}",
        f"- min_name_confidence: {selection_options.get('min_name_confidence')}",
        f"- prefer_direct_contact_url: {selection_options.get('prefer_direct_contact_url')}",
        f"- exclude_feedback_bad: {selection_options.get('exclude_feedback_bad')}",
        f"- feedback_csv: {selection_options.get('feedback_csv')}",
        f"- feedback_excluded_ids: {selection_options.get('feedback_excluded_ids')}",
        f"- feedback_excluded_domains: {selection_options.get('feedback_excluded_domains')}",
        "",
        "## Selection Skips",
        "",
    ]
    if skipped:
        for reason, count in sorted(skipped.items()):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Preflight",
            "",
            "- ready: " + ("yes" if preflight_ready else "no"),
            f"- return_code: {preflight_return_code}",
            f"- preflight_log: {preflight_log_path}",
            "",
            "## Run",
            "",
            "- requested: " + ("yes" if run_requested else "no"),
            "- command: `" + _command_text(len(selected), batch_path) + "`",
            "- return_code: " + ("not_run" if run_return_code is None else str(run_return_code)),
            "",
            "## Results",
            "",
            f"- success_count: {artifact_info.get('success_count', 0)}",
            f"- partial_success_count: {artifact_info.get('partial_count', 0)}",
            f"- stop_manual_review_count: {artifact_info.get('manual_count', 0)}",
            "",
            "## Status Counts",
            "",
        ]
    )
    if status_counts:
        for status, count in sorted(status_counts.items()):
            lines.append(f"- {status}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Failure Categories", ""])
    failure_reasons = {
        reason: count
        for reason, count in reason_counts.items()
        if reason not in {"prepared", "semi_auto_prepared"}
    }
    if failure_reasons:
        for reason, count in sorted(failure_reasons.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Artifacts", ""])
    for label, values in [
        ("logs", artifact_info.get("log_paths", [])),
        ("submissions", artifact_info.get("submissions_paths", [])),
        ("review_queues", artifact_info.get("review_paths", [])),
        ("daily_summaries", artifact_info.get("summary_paths", [])),
        ("screenshots", artifact_info.get("screenshot_paths", [])),
    ]:
        lines.append(f"- {label}:")
        value_list = list(values or [])  # type: ignore[arg-type]
        if value_list:
            for value in value_list[:40]:
                lines.append(f"  - {value}")
            if len(value_list) > 40:
                lines.append(f"  - ... {len(value_list) - 40} more")
        else:
            lines.append("  - none")

    lines.extend(
        [
            "",
            "## Runner",
            "",
            f"- started_at: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"- finished_at: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"- discord_notification: {notification_status}",
            "",
        ]
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _confirm_run(selected: list[Candidate], yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print("ERROR --run requires --yes in non-interactive mode.")
        return False
    print(f"Prepared {len(selected)} SEMI_AUTO review rows.")
    answer = input("Type RUN to start SEMI_AUTO review preparation: ").strip()
    return answer == "RUN"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and optionally run a SEMI_AUTO review batch.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Source handoff_with_demo_paths.csv")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output batch CSV path")
    parser.add_argument("--limit", type=int, default=10, help="Maximum rows to select")
    parser.add_argument("--exclude-domain", action="append", default=[], help="Domain to exclude; may be repeated")
    parser.add_argument("--exclude-corporate", action="store_true", help="Exclude corporate-like rows from the batch.")
    parser.add_argument(
        "--exclude-line-domains",
        action="store_true",
        help="Exclude rows whose selected contact URL is LINE/SNS-only.",
    )
    parser.add_argument(
        "--exclude-portal-listing",
        action="store_true",
        help="Exclude portal/listing/media-style rows and known listing hosts.",
    )
    parser.add_argument(
        "--min-name-confidence",
        choices=["low", "medium", "high"],
        default="low",
        help="Minimum lead-finder display-name confidence required for selection.",
    )
    parser.add_argument(
        "--prefer-direct-contact-url",
        action="store_true",
        help="Exclude rows without a direct same-site contact URL or likely form page.",
    )
    parser.add_argument(
        "--feedback-csv",
        default="",
        help="Optional lead_quality_feedback CSV from tools/analyze_semi_auto_batch.py.",
    )
    parser.add_argument(
        "--exclude-feedback-bad",
        action="store_true",
        help="Exclude rows/domains marked bad by the feedback CSV.",
    )
    parser.add_argument("--prepare-only", action="store_true", help="Create batch CSV and previews only")
    parser.add_argument("--run", action="store_true", help="Run SEMI_AUTO review preparation after preflight")
    parser.add_argument("--yes", action="store_true", help="Skip only the runner confirmation before starting --run")
    parser.add_argument(
        "--include-ledger-domains",
        action="store_true",
        help="Allow domains/IDs already present in data/submission_ledger.csv.",
    )
    parser.add_argument(
        "--include-settings-skip-domains",
        action="store_true",
        help="Allow domains listed in config/settings.json skip_domains.",
    )
    parser.add_argument("--no-notify", action="store_true", help="Do not attempt the optional Discord notification.")
    args = parser.parse_args()

    if args.limit < 1:
        print("ERROR limit must be >= 1.")
        return 2
    if args.prepare_only and args.run:
        print("ERROR choose only one of --prepare-only or --run.")
        return 2

    start_time = datetime.now(JST)
    run_stamp = start_time.strftime("%Y%m%d_%H%M%S")
    source_path = Path(args.input).resolve()
    batch_path = Path(args.output)
    if not batch_path.is_absolute():
        batch_path = (PROJECT_ROOT / batch_path).resolve()
    summary_path = RESULTS_DIR / f"semi_auto_batch_summary_{run_stamp}.md"
    preview_path = RESULTS_DIR / f"semi_auto_batch_preview_{run_stamp}.csv"
    audit_path = RESULTS_DIR / f"semi_auto_batch_selection_audit_{run_stamp}.csv"
    first_message_path = RESULTS_DIR / f"semi_auto_batch_first_message_{run_stamp}.txt"
    preflight_log_path = RESULTS_DIR / f"semi_auto_batch_preflight_{run_stamp}.txt"

    if not source_path.exists():
        print(f"ERROR source CSV missing: {source_path}")
        return 2

    rows, headers = _read_csv(source_path)
    feedback_path = (
        Path(args.feedback_csv).resolve()
        if args.feedback_csv
        else RESULTS_DIR / f"lead_quality_feedback_{start_time.strftime('%Y%m%d')}.csv"
    )
    feedback_exclude_ids: set[str] = set()
    feedback_exclude_domains: set[str] = set()
    feedback_exclusion_reasons: Counter[str] = Counter()
    if args.exclude_feedback_bad:
        feedback_exclude_ids, feedback_exclude_domains, feedback_exclusion_reasons = _read_feedback_exclusions(
            feedback_path
        )
    selected, skipped = _select_candidates(
        rows=rows,
        limit=args.limit,
        exclude_domains={_normalize_domain(item) for item in args.exclude_domain},
        include_ledger_domains=bool(args.include_ledger_domains),
        avoid_settings_skip_domains=not bool(args.include_settings_skip_domains),
        exclude_corporate=bool(args.exclude_corporate),
        exclude_line_domains=bool(args.exclude_line_domains),
        exclude_portal_listing=bool(args.exclude_portal_listing),
        min_name_confidence=str(args.min_name_confidence),
        prefer_direct_contact_url=bool(args.prefer_direct_contact_url),
        feedback_exclude_ids=feedback_exclude_ids,
        feedback_exclude_domains=feedback_exclude_domains,
    )
    for reason, count in feedback_exclusion_reasons.items():
        skipped[f"feedback_reason:{reason}"] += count
    _write_selection_audit(audit_path, rows, selected, feedback_exclude_ids, feedback_exclude_domains)
    selection_options = {
        "exclude_corporate": bool(args.exclude_corporate),
        "exclude_line_domains": bool(args.exclude_line_domains),
        "exclude_portal_listing": bool(args.exclude_portal_listing),
        "min_name_confidence": str(args.min_name_confidence),
        "prefer_direct_contact_url": bool(args.prefer_direct_contact_url),
        "exclude_feedback_bad": bool(args.exclude_feedback_bad),
        "feedback_csv": str(feedback_path) if args.exclude_feedback_bad or args.feedback_csv else "",
        "feedback_excluded_ids": len(feedback_exclude_ids),
        "feedback_excluded_domains": len(feedback_exclude_domains),
    }

    if not selected:
        print("ERROR no rows selected for SEMI_AUTO review batch.")
        print(f"selection_audit={audit_path}")
        return 1

    _write_csv(batch_path, headers, [item.row for item in selected])
    _write_selection_preview(preview_path, selected)
    preflight_ready, preflight_return_code = _run_preflight(batch_path, first_message_path, preflight_log_path)

    run_requested = bool(args.run)
    run_return_code: int | None = None
    artifact_info: dict[str, object] = _inspect_artifacts(selected, start_time)
    sender_ignored, sender_tracked = _sender_info_git_state()
    notification_status = "not_attempted"

    if not preflight_ready:
        notification_status = _send_discord_notification(not args.no_notify)
        _write_summary(
            summary_path,
            source_path=source_path,
            batch_path=batch_path,
            preview_path=preview_path,
            audit_path=audit_path,
            first_message_path=first_message_path,
            preflight_log_path=preflight_log_path,
            preflight_ready=preflight_ready,
            preflight_return_code=preflight_return_code,
            selected=selected,
            skipped=skipped,
            run_requested=run_requested,
            run_return_code=run_return_code,
            artifact_info=artifact_info,
            notification_status=notification_status,
            sender_ignored=sender_ignored,
            sender_tracked=sender_tracked,
            start_time=start_time,
            selection_options=selection_options,
        )
        print(f"batch_csv={batch_path}")
        print(f"summary={summary_path}")
        print(f"preflight_ready=false preflight_log={preflight_log_path}")
        return 1

    if run_requested:
        if not _confirm_run(selected, args.yes):
            return 1
        print("Starting SEMI_AUTO review preparation. Final submission remains guarded by src/main.py SEMI_AUTO mode.")
        print(f"internal_command={_command_text(len(selected), batch_path)}")
        run_return_code = _run_semi_auto(len(selected), batch_path)
        artifact_info = _inspect_artifacts(selected, start_time)
    else:
        print("prepare_only=true")

    notification_status = _send_discord_notification(not args.no_notify)
    _write_summary(
        summary_path,
        source_path=source_path,
        batch_path=batch_path,
        preview_path=preview_path,
        audit_path=audit_path,
        first_message_path=first_message_path,
        preflight_log_path=preflight_log_path,
        preflight_ready=preflight_ready,
        preflight_return_code=preflight_return_code,
        selected=selected,
        skipped=skipped,
        run_requested=run_requested,
        run_return_code=run_return_code,
        artifact_info=artifact_info,
        notification_status=notification_status,
        sender_ignored=sender_ignored,
        sender_tracked=sender_tracked,
        start_time=start_time,
        selection_options=selection_options,
    )

    print(f"selected_rows={len(selected)}")
    print(f"batch_csv={batch_path}")
    print(f"preview_csv={preview_path}")
    print(f"selection_audit={audit_path}")
    print(f"first_message_preview={first_message_path}")
    print(f"preflight_ready=true preflight_log={preflight_log_path}")
    print(f"summary={summary_path}")
    print(f"final_submission_avoided={'true' if int(artifact_info.get('sent_count', 0) or 0) == 0 else 'false'}")
    if run_return_code not in (None, 0):
        return run_return_code
    if int(artifact_info.get("sent_count", 0) or 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
