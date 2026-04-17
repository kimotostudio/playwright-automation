from __future__ import annotations

import csv
import json
import os
import platform
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
from src.target_classifier import (
    EXCLUDE_REASON_KEYS,
    TARGET_BORDERLINE,
    classify_lead,
)

JST = ZoneInfo("Asia/Tokyo")

QUEUE_ID_ALIASES = ["salon_id", "id", "ID"]
QUEUE_NAME_ALIASES = ["salon_name", "店名", "店舗名", "名称", "name"]
QUEUE_DOMAIN_ALIASES = ["domain"]
QUEUE_CONTACT_URL_ALIASES = ["contact_url", "url", "URL"]
QUEUE_FINAL_URL_ALIASES = ["final_step_url", "url", "URL"]
QUEUE_STATUS_ALIASES = ["status"]
QUEUE_REASON_ALIASES = ["reason", "message", "notes"]
QUEUE_LAST_ACTION_ALIASES = ["last_action"]
QUEUE_MESSAGE_ALIASES = ["generated_message", "message_text", "message_body", "mail_body"]

SCREENSHOT_LABELS = {
    "01": "Before fill",
    "02": "After fill",
    "03": "Before submit/confirm",
    "04": "On confirmation page",
}

PORTAL_DOMAINS = [
    "hotpepper.jp",
    "rakuten.co.jp",
    "ameblo.jp",
    "line.me",
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "x.com",
    "youtube.com",
]

PORTAL_URL_HINTS = [
    "google.com/maps",
]

ACTION_FIELDS = [
    "timestamp",
    "staff_user",
    "salon_id",
    "decision",
    "note",
    "final_step_url",
]

FINAL_DECISIONS = {
    "prepared_ok",
    "needs_manual",
    "skip_address_required",
    "skip_login_required",
    "skip_bot_protection",
    "bad_lead",
    "prepared_like",
    "investigate_later",
    "probably_contactable",
    "not_contactable",
    "portal",
    "login_required",
    "captcha",
}


def normalize_col(name: str) -> str:
    return re.sub(r"[\s_\-()（）・]+", "", str(name).strip().lower())


def resolve_column(columns: list[str], aliases: list[str]) -> Optional[str]:
    normalized = {normalize_col(c): c for c in columns}
    for alias in aliases:
        key = normalize_col(alias)
        if key in normalized:
            return normalized[key]
    return None


def parse_queue_date(path: Path) -> str:
    m = re.search(r"review_queue_(\d{8})", path.name)
    if m:
        return m.group(1)
    return datetime.now(JST).strftime("%Y%m%d")


def latest_review_queue(results_dir: Path, env_var: str = "REVIEW_QUEUE_PATH") -> Optional[Path]:
    env_path = os.environ.get(env_var, "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    files = list(results_dir.glob("review_queue_*.csv"))
    if not files:
        return None

    def sort_key(path: Path) -> tuple[str, float]:
        token = parse_queue_date(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return token, mtime

    return sorted(files, key=sort_key)[-1]


def list_review_queue_files(results_dir: Path) -> list[Path]:
    files = list(results_dir.glob("review_queue_*.csv"))
    return sorted(files, key=lambda p: (parse_queue_date(p), p.stat().st_mtime if p.exists() else 0.0), reverse=True)


def latest_submissions(results_dir: Path) -> Optional[Path]:
    files = list(results_dir.glob("submissions_*.csv"))
    if not files:
        return None

    def sort_key(path: Path) -> tuple[str, float]:
        m = re.search(r"submissions_(\d{8})", path.name)
        token = m.group(1) if m else "00000000"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return token, mtime

    return sorted(files, key=sort_key)[-1]


def list_submissions_files(results_dir: Path) -> list[Path]:
    files = list(results_dir.glob("submissions_*.csv"))
    return sorted(files, key=lambda p: (parse_queue_date(p), p.stat().st_mtime if p.exists() else 0.0), reverse=True)


def load_queue_df(path: Path) -> pd.DataFrame:
    src = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    columns = list(src.columns)

    id_col = resolve_column(columns, QUEUE_ID_ALIASES)
    name_col = resolve_column(columns, QUEUE_NAME_ALIASES)
    domain_col = resolve_column(columns, QUEUE_DOMAIN_ALIASES)
    contact_col = resolve_column(columns, QUEUE_CONTACT_URL_ALIASES)
    final_col = resolve_column(columns, QUEUE_FINAL_URL_ALIASES)
    status_col = resolve_column(columns, QUEUE_STATUS_ALIASES)
    reason_col = resolve_column(columns, QUEUE_REASON_ALIASES)
    last_action_col = resolve_column(columns, QUEUE_LAST_ACTION_ALIASES)
    message_col = resolve_column(columns, QUEUE_MESSAGE_ALIASES)

    out = pd.DataFrame(index=src.index)
    out["id"] = src[id_col].astype(str).str.strip() if id_col else (src.index + 1).astype(str)
    out["name"] = src[name_col].astype(str).str.strip() if name_col else ""
    out["contact_url"] = src[contact_col].astype(str).str.strip() if contact_col else ""
    out["final_step_url"] = src[final_col].astype(str).str.strip() if final_col else ""
    out["status"] = src[status_col].astype(str).str.strip() if status_col else ""
    out["reason"] = src[reason_col].astype(str).str.strip() if reason_col else ""
    out["last_action"] = src[last_action_col].astype(str).str.strip() if last_action_col else ""
    out["message_text"] = src[message_col].astype(str).str.strip() if message_col else ""
    out["domain"] = src[domain_col].astype(str).str.strip() if domain_col else ""

    # Carry over additional columns from queue CSV
    for extra in ["evidence", "stop_state", "confidence_level", "detected_platform", "missing_required_fields", "notes"]:
        extra_col = resolve_column(columns, [extra])
        out[extra] = src[extra_col].astype(str).str.strip() if extra_col else ""

    final_norm = out["final_step_url"].str.strip().str.lower().str.rstrip("/")
    contact_norm = out["contact_url"].str.strip().str.lower().str.rstrip("/")
    same_as_contact = (final_norm != "") & (final_norm == contact_norm)

    out["effective_url"] = out["contact_url"].where(
        (out["final_step_url"].str.len() == 0) | same_as_contact,
        out["final_step_url"],
    )

    out["domain"] = out["domain"].where(
        out["domain"].str.len() > 0,
        out["effective_url"].map(lambda u: urlparse(str(u)).netloc.lower() if str(u).strip() else ""),
    )

    out["search_blob"] = (
        out["name"].astype(str)
        + " "
        + out["domain"].astype(str)
        + " "
        + out["contact_url"].astype(str)
        + " "
        + out["final_step_url"].astype(str)
        + " "
        + out["reason"].astype(str)
    ).str.lower()

    return out


def load_submissions_df(path: Path) -> pd.DataFrame:
    src = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    columns = list(src.columns)

    id_col = resolve_column(columns, ["salon_id", "id", "ID"])
    name_col = resolve_column(columns, ["salon_name", "店名", "店舗名", "名称", "name"])
    url_col = resolve_column(columns, ["url", "URL", "contact_url"])
    status_col = resolve_column(columns, ["status"])
    reason_col = resolve_column(columns, ["reason", "message", "notes"])
    evidence_col = resolve_column(columns, ["evidence", "detect_evidence"])

    out = pd.DataFrame(index=src.index)
    out["id"] = src[id_col].astype(str).str.strip() if id_col else (src.index + 1).astype(str)
    out["name"] = src[name_col].astype(str).str.strip() if name_col else ""
    out["contact_url"] = src[url_col].astype(str).str.strip() if url_col else ""
    out["final_step_url"] = out["contact_url"]
    out["status"] = src[status_col].astype(str).str.strip() if status_col else ""
    out["reason"] = src[reason_col].astype(str).str.strip() if reason_col else ""
    out["evidence"] = src[evidence_col].astype(str).str.strip() if evidence_col else ""
    out["domain"] = out["contact_url"].map(lambda u: urlparse(str(u)).netloc.lower() if str(u).strip() else "")
    out["last_action"] = ""
    out["message_text"] = ""
    out["effective_url"] = out["final_step_url"]
    out["search_blob"] = (
        out["name"].astype(str)
        + " "
        + out["domain"].astype(str)
        + " "
        + out["contact_url"].astype(str)
        + " "
        + out["final_step_url"].astype(str)
        + " "
        + out["reason"].astype(str)
        + " "
        + out["evidence"].astype(str)
    ).str.lower()
    return out


def merge_sources(queue_df: pd.DataFrame, submissions_df: pd.DataFrame) -> pd.DataFrame:
    if queue_df.empty and submissions_df.empty:
        return pd.DataFrame()
    if queue_df.empty:
        return submissions_df.copy()
    if submissions_df.empty:
        return queue_df.copy()

    merged = submissions_df.copy()
    q = queue_df.copy()
    q["_is_queue"] = 1
    merged["_is_queue"] = 0
    combined = pd.concat([merged, q], ignore_index=True, sort=False).fillna("")
    combined["_id_key"] = combined["id"].astype(str).str.strip()
    combined = combined.sort_values(by=["_id_key", "_is_queue"], ascending=[True, False], kind="stable")
    combined = combined.drop_duplicates(subset=["_id_key"], keep="first")
    combined = combined.drop(columns=[c for c in ["_id_key", "_is_queue"] if c in combined.columns])
    return combined.reset_index(drop=True)


def _normalize_reason_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        values = [str(v).strip() for v in raw if str(v).strip()]
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        values = [v.strip() for v in re.split(r"[|,;/\n]+", text) if v.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def enrich_target_classification(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        out["target_label"] = ""
        out["target_score"] = 0
        out["exclude_reason"] = ""
        out["debug_tokens"] = ""
        out["exclude_reason_list"] = pd.Series(dtype=object)
        return out

    labels: list[str] = []
    scores: list[int] = []
    reasons_pipe: list[str] = []
    debug_pipe: list[str] = []
    reason_lists: list[list[str]] = []

    for _, row in out.iterrows():
        existing_label = str(row.get("target_label", "")).strip()
        existing_score_raw = str(row.get("target_score", "")).strip()
        existing_reason_list = _normalize_reason_list(row.get("exclude_reason", ""))
        existing_debug = _normalize_reason_list(row.get("debug_tokens", ""))

        if existing_label:
            label = existing_label
            try:
                score = int(float(existing_score_raw))
            except Exception:
                score = 0
            reasons = [r for r in existing_reason_list if r in EXCLUDE_REASON_KEYS]
            debug_tokens = existing_debug
        else:
            classified = classify_lead(
                {
                    "name": str(row.get("name", "")).strip(),
                    "salon_name": str(row.get("salon_name", "")).strip(),
                    "domain": str(row.get("domain", "")).strip(),
                    "url": str(row.get("effective_url", "")).strip()
                    or str(row.get("contact_url", "")).strip()
                    or str(row.get("final_step_url", "")).strip(),
                    "reason": str(row.get("reason", "")).strip(),
                }
            )
            label = str(classified.get("target_label", TARGET_BORDERLINE))
            score = int(classified.get("target_score", 0))
            reasons = _normalize_reason_list(classified.get("exclude_reason", []))
            debug_tokens = _normalize_reason_list(classified.get("debug_tokens", []))

        labels.append(label)
        scores.append(score)
        reason_lists.append(reasons)
        reasons_pipe.append("|".join(reasons))
        debug_pipe.append("|".join(debug_tokens))

    out["target_label"] = labels
    out["target_score"] = scores
    out["exclude_reason"] = reasons_pipe
    out["debug_tokens"] = debug_pipe
    out["exclude_reason_list"] = reason_lists
    return out


def extract_candidate_urls(row: pd.Series) -> list[str]:
    values = []
    for key in ["final_step_url", "contact_url", "effective_url", "url"]:
        value = str(row.get(key, "")).strip()
        if value:
            values.append(value)
    evidence = str(row.get("evidence", "")).strip()
    if evidence:
        values.extend(re.findall(r"https?://[^\s|;,\"]+", evidence))
    raw = str(row.get("candidate_urls", "")).strip()
    if raw:
        values.extend([v.strip() for v in re.split(r"[|,\n]", raw) if v.strip()])
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def infer_detection_source(row: pd.Series) -> str:
    reason = str(row.get("reason", "")).lower()
    evidence = str(row.get("evidence", "")).lower()
    url = str(row.get("effective_url", "")).lower()
    text = " ".join([reason, evidence, url])
    if "external_form" in text or any(h in text for h in ["docs.google.com/forms", "form.run", "typeform", "jotform"]):
        return "external_form"
    return "internal"


def infer_candidate_links_count(row: pd.Series) -> int:
    raw = str(row.get("candidate_contact_links_found", "")).strip()
    if raw.isdigit():
        return int(raw)
    m = re.search(r"candidate(?:s)?[=: ](\d+)", str(row.get("evidence", "")), re.IGNORECASE)
    if m:
        return int(m.group(1))
    return len(extract_candidate_urls(row))


def detect_flags(row: pd.Series) -> list[str]:
    reason = str(row.get("reason", "")).lower()
    status = str(row.get("status", "")).lower()
    url = str(row.get("effective_url", "")).lower()
    domain = str(row.get("domain", "")).lower()
    flags: list[str] = []
    if "captcha" in reason or "bot_protection" in reason or "captcha" in status:
        flags.append("CAPTCHA")
    if "login" in reason or "requires_login" in reason or "password" in reason:
        flags.append("LOGIN")
    if infer_detection_source(row) == "external_form":
        flags.append("EXT_FORM")
    if any(domain == d or domain.endswith("." + d) for d in PORTAL_DOMAINS) or any(h in url for h in PORTAL_URL_HINTS):
        flags.append("PORTAL")
    return flags


def flags_badge(flags: list[str]) -> str:
    if not flags:
        return ""
    return " ".join(f"[{f}]" for f in flags)


def is_hard_exclude(row: pd.Series) -> bool:
    reason = str(row.get("reason", "")).lower()
    url = str(row.get("effective_url", "")).lower()
    domain = str(row.get("domain", "")).lower()
    source = infer_detection_source(row)
    candidates = infer_candidate_links_count(row)

    # 1) known portal/lead source domains
    if any(domain == d or domain.endswith("." + d) for d in PORTAL_DOMAINS):
        return True
    if any(hint in url for hint in PORTAL_URL_HINTS):
        return True

    # 2) pure non-contact pages with no CTA and no external form
    if source != "external_form" and ("no_contact_page" in reason or "no_form_found" in reason) and candidates <= 0:
        return True

    # 3) recruit-only pages without contact/booking hints
    recruit = any(k in reason or k in url for k in ["求人", "採用", "recruit"])
    has_contact_hint = any(k in url or k in reason for k in ["contact", "inquiry", "toiawase", "予約", "reserve", "booking"])
    if recruit and not has_contact_hint:
        return True

    return False


def discover_screenshots(screenshots_root: Path, salon_id: str, date_hint: Optional[str] = None) -> dict[str, Optional[Path]]:
    sid = str(salon_id).strip()
    result: dict[str, Optional[Path]] = {"01": None, "02": None, "03": None, "04": None}

    candidates: list[Path] = []
    if date_hint:
        day_dir = screenshots_root / date_hint
        if day_dir.exists():
            candidates.extend(day_dir.glob(f"{sid}_*.png"))

    if not candidates:
        candidates.extend(screenshots_root.glob(f"**/{sid}_*.png"))

    candidates = sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)

    for step in ["04", "03", "02", "01"]:
        key = f"_{step}_"
        for path in candidates:
            if key in path.name:
                result[step] = path
                break

    return result


def screenshot_folder_for(screenshots: dict[str, Optional[Path]], screenshots_root: Path, date_hint: Optional[str]) -> Path:
    for step in ["01", "02", "03", "04"]:
        path = screenshots.get(step)
        if path is not None:
            return path.parent
    if date_hint:
        return screenshots_root / date_hint
    return screenshots_root


def open_url(url: str) -> tuple[bool, str]:
    target = str(url).strip()
    if not target:
        return False, "empty_url"
    try:
        webbrowser.open(target, new=2)
        return True, "opened"
    except Exception as e:
        return False, str(e)


def open_folder(path: Path) -> tuple[bool, str]:
    try:
        if not path.exists():
            return False, f"missing_folder:{path}"
        if platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True, "opened"
    except Exception as e:
        return False, str(e)


def action_log_path(data_dir: Path, date_str: str) -> Path:
    return data_dir / f"staff_actions_{date_str}.csv"


def actioned_ids_path(data_dir: Path, date_str: str) -> Path:
    return data_dir / f"staff_actioned_ids_{date_str}.json"


def append_action(path: Path, *, salon_id: str, decision: str, final_step_url: str, staff_user: str = "", note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
        "staff_user": str(staff_user).strip(),
        "salon_id": str(salon_id).strip(),
        "decision": str(decision).strip(),
        "note": str(note).strip(),
        "final_step_url": str(final_step_url).strip(),
    }
    exists = path.exists()
    with path.open("a" if exists else "w", encoding="utf-8" if exists else "utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ACTION_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def read_actions(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=ACTION_FIELDS)
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
        for col in ACTION_FIELDS:
            if col not in df.columns:
                df[col] = ""
        return df[ACTION_FIELDS]
    except Exception:
        return pd.DataFrame(columns=ACTION_FIELDS)


def latest_action_map(actions_df: pd.DataFrame) -> dict[str, str]:
    out: dict[str, str] = {}
    if actions_df.empty:
        return out
    for _, row in actions_df.iterrows():
        sid = str(row.get("salon_id", "")).strip()
        action = str(row.get("decision", "")).strip()
        if sid:
            out[sid] = action
    return out


def load_actioned_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return {str(x).strip() for x in raw if str(x).strip()}
    except Exception:
        return set()


def save_actioned_ids(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(sorted(values), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def actioned_ids_from_actions(actions_df: pd.DataFrame) -> set[str]:
    latest: dict[str, str] = {}
    if actions_df.empty:
        return set()
    for _, row in actions_df.iterrows():
        sid = str(row.get("salon_id", "")).strip()
        decision = str(row.get("decision", "")).strip()
        if sid:
            latest[sid] = decision
    return {sid for sid, decision in latest.items() if decision in FINAL_DECISIONS}


def undo_last_action(path: Path) -> tuple[bool, str]:
    df = read_actions(path)
    if df.empty:
        return False, "no_actions"
    df = df.iloc[:-1].copy()
    tmp = path.with_suffix(path.suffix + ".tmp")
    if df.empty:
        path.unlink(missing_ok=True)
        return True, "undone"
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ACTION_FIELDS)
        writer.writeheader()
        writer.writerows(df.to_dict(orient="records"))
    os.replace(tmp, path)
    return True, "undone"


def run_prefill_subprocess(
    salon_id: str,
    review_queue_path: Path | None,
    *,
    final_url: str = "",
    keep_open: bool = True,
) -> tuple[int, str, str, dict]:
    cmd = [
        sys.executable,
        "-m",
        "src.prefill_only",
        "--lead-id",
        str(salon_id),
    ]
    if review_queue_path and review_queue_path.exists() and review_queue_path.is_file():
        cmd.extend(["--queue", str(review_queue_path)])
    if str(final_url).strip():
        cmd.extend(["--final-url", str(final_url).strip()])
    cmd.append("--keep-open" if keep_open else "--no-keep-open")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    payload: dict = {}
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
    return proc.returncode, proc.stdout or "", proc.stderr or "", payload


def run_detection_subprocess(
    salon_id: str,
    review_queue_path: Path | None,
    *,
    base_url: str = "",
    final_url: str = "",
) -> tuple[int, str, str, dict]:
    cmd = [
        sys.executable,
        "-m",
        "src.prefill_only",
        "--lead-id",
        str(salon_id),
        "--detect-only",
        "--no-keep-open",
        "--no-wait",
    ]
    if review_queue_path and review_queue_path.exists() and review_queue_path.is_file():
        cmd.extend(["--queue", str(review_queue_path)])
    if str(base_url).strip():
        cmd.extend(["--base-url", str(base_url).strip()])
    if str(final_url).strip():
        cmd.extend(["--final-url", str(final_url).strip()])

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    payload: dict = {}
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
    return proc.returncode, proc.stdout or "", proc.stderr or "", payload


def try_copy_to_clipboard(text: str) -> tuple[bool, str]:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True, "copied"
    except Exception as e:
        return False, str(e)
