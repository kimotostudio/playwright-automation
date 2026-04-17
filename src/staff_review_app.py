from __future__ import annotations

import csv
import json
import math
import os
import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from src.ui_utils import (
    SCREENSHOT_LABELS,
    detect_flags,
    discover_screenshots,
    enrich_target_classification,
    flags_badge,
    infer_candidate_links_count,
    latest_review_queue,
    latest_submissions,
    list_review_queue_files,
    list_submissions_files,
    load_queue_df,
    load_submissions_df,
    merge_sources,
    open_folder,
    open_url,
    parse_queue_date,
    run_prefill_subprocess,
)

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
DATA_DIR = ROOT / "data"
SCREENSHOTS_DIR = ROOT / "screenshots"
SETTINGS_PATH = ROOT / "config" / "settings.json"
STATE_PATH = DATA_DIR / "state.json"
LEADS_PATH = DATA_DIR / "leads.csv"
AIDNET_DEFAULT_PATH = DATA_DIR / "エイドネット_ドメインリスト - リスト_日本語学校.csv"

SOURCE_LABELS = {"merged": "統合（推奨）", "queue": "Review Queue", "submissions": "Submissions", "aidnet": "Aidnet CSV"}
VISIBILITY_LABELS = {"review": "Review（ほぼ全件表示）", "strict": "Strict（厳しめ）"}
PREPARED_STATUSES = ["prepared_full", "prepared_partial", "prepared_external", "prepared_review_needed"]
TAGS = ["GOOD", "BORDERLINE", "EXCLUDE_CLEAR"]
ACTION_COLS = ["timestamp", "staff_user", "salon_id", "salon_name", "domain", "status", "action", "reason", "note", "final_step_url", "stop_state"]

# ---------------------------------------------------------------------------
# Status display & badge configuration
# ---------------------------------------------------------------------------

STATUS_DISPLAY = {
    "prepared_full": "完全入力済み",
    "prepared_partial": "部分入力済み",
    "prepared_external": "外部フォーム",
    "prepared_review_needed": "要確認",
    "skipped_login": "ログイン必須",
    "skipped_bot_protection": "Bot保護",
    "skipped_dead_site": "サイト不通",
    "sent": "送信済み",
}

STATUS_COLORS = {
    "prepared_full": ("#059669", "#ecfdf5"),       # green
    "prepared_partial": ("#d97706", "#fffbeb"),     # amber
    "prepared_external": ("#7c3aed", "#f5f3ff"),    # purple
    "prepared_review_needed": ("#dc2626", "#fef2f2"),  # red
    "skipped_login": ("#6b7280", "#f3f4f6"),        # gray
    "skipped_bot_protection": ("#6b7280", "#f3f4f6"),
    "skipped_dead_site": ("#6b7280", "#f3f4f6"),
    "sent": ("#2563eb", "#eff6ff"),                 # blue
}

CONFIDENCE_COLORS = {
    "high": "#059669",
    "medium": "#d97706",
    "low": "#dc2626",
}

TAG_COLORS = {
    "GOOD": ("#059669", "#ecfdf5"),
    "BORDERLINE": ("#d97706", "#fffbeb"),
    "EXCLUDE_CLEAR": ("#dc2626", "#fef2f2"),
}


def format_status(status: str) -> str:
    return STATUS_DISPLAY.get(status, status)


def status_badge_html(status: str) -> str:
    label = STATUS_DISPLAY.get(status, status)
    fg, bg = STATUS_COLORS.get(status, ("#374151", "#f3f4f6"))
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;font-size:0.82em;font-weight:600;white-space:nowrap;">{label}</span>'


def confidence_dot(level: str) -> str:
    color = CONFIDENCE_COLORS.get(level, "#6b7280")
    return f'<span style="color:{color};font-size:0.85em;">● {level}</span>'


def tag_badge_html(tag: str) -> str:
    fg, bg = TAG_COLORS.get(tag, ("#374151", "#f3f4f6"))
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;font-size:0.82em;font-weight:600;">{tag}</span>'


# ---------------------------------------------------------------------------
# CSS theme
# ---------------------------------------------------------------------------

CUSTOM_CSS = """
<style>
/* System font stack */
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
}

/* Sidebar */
section[data-testid="stSidebar"] {
    background: #f8fafc;
    border-right: 1px solid #e2e8f0;
}
section[data-testid="stSidebar"] .stRadio > label {
    font-weight: 600;
    color: #334155;
}

/* Metric cards */
div[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 12px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
div[data-testid="stMetric"] label {
    color: #64748b !important;
    font-size: 0.8rem !important;
    font-weight: 600 !important;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #1e293b !important;
    font-weight: 700 !important;
}

/* Buttons */
.stButton > button {
    border-radius: 6px;
    font-weight: 600;
    transition: all 0.15s ease;
}
.stButton > button:hover {
    transform: translateY(-1px);
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}
.stButton > button[kind="primary"] {
    background: #4f46e5;
}
.stButton > button[kind="primary"]:hover {
    background: #4338ca;
}

/* Dataframe */
div[data-testid="stDataFrame"] {
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    overflow: hidden;
}

/* Detail panel header */
.detail-header {
    background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 12px;
}
.detail-header h2 {
    margin: 0 0 6px 0;
    color: #1e293b;
    font-size: 1.3rem;
}
.detail-header .badges {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
}

/* URL links */
.url-section {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 6px 0;
    font-size: 0.88em;
}
.url-section a {
    color: #4f46e5;
    text-decoration: none;
    word-break: break-all;
}
.url-section a:hover {
    text-decoration: underline;
}
.url-label {
    color: #64748b;
    font-weight: 600;
    font-size: 0.82em;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    margin-bottom: 2px;
}

/* Evidence box */
.evidence-box {
    background: #fffbeb;
    border: 1px solid #fde68a;
    border-left: 4px solid #f59e0b;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 8px 0;
    font-size: 0.88em;
    color: #92400e;
}

/* Warning box for review needed */
.review-warning {
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-left: 4px solid #ef4444;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 8px 0;
    font-size: 0.88em;
    color: #991b1b;
    font-weight: 500;
}

/* Shortcut hints */
.shortcut-hints {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 8px 0;
}
.shortcut-key {
    background: #f1f5f9;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 2px 6px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.78em;
    color: #475569;
    box-shadow: 0 1px 0 #94a3b8;
}

/* Quick actions row */
.action-buttons {
    display: flex;
    gap: 8px;
}

/* Page header */
.page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 12px;
    border-bottom: 2px solid #e2e8f0;
    margin-bottom: 16px;
}

/* Hide streamlit branding */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header[data-testid="stHeader"] {
    background: rgba(255,255,255,0.97);
    backdrop-filter: blur(4px);
}
</style>
"""


# ---------------------------------------------------------------------------
# Helpers (data I/O)
# ---------------------------------------------------------------------------

def _read_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return dict(fallback)


@st.cache_data(show_spinner=False)
def _load_leads(path_str: str) -> dict[str, dict]:
    path = Path(path_str)
    if not path.exists():
        return {}
    src = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    out: dict[str, dict] = {}
    for _, row in src.iterrows():
        lead_id = str(row.get("id", "") or row.get("ID", "")).strip()
        if not lead_id:
            continue
        out[lead_id] = {
            "name": str(row.get("店名", "") or row.get("店舗名", "") or row.get("名称", "")).strip(),
            "original_url": str(row.get("url(旧)", "") or row.get("url（旧）", "") or row.get("url", "")).strip(),
            "demo_url": str(row.get("url(デモ)", "") or row.get("url（デモ）", "") or row.get("demo_url", "")).strip(),
        }
    return out


def _normalize_col_name(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("（", "(").replace("）", ")").replace("　", "")
    return normalized.replace(" ", "")


def _resolve_col_name(columns: list[str], aliases: list[str]) -> str:
    normalized = {_normalize_col_name(col): col for col in columns}
    for alias in aliases:
        direct = next((col for col in columns if col == alias), "")
        if direct:
            return direct
        alt = normalized.get(_normalize_col_name(alias), "")
        if alt:
            return alt
    return ""


def _is_http_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def _extract_domain(value: str) -> str:
    if not _is_http_url(value):
        return ""
    parsed = urlparse(str(value).strip())
    domain = str(parsed.netloc or "").lower().strip()
    if ":" in domain:
        domain = domain.split(":", 1)[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.strip(".")


@st.cache_data(show_spinner=False)
def _load_aidnet_rows(path_str: str) -> pd.DataFrame:
    cols = [
        "id",
        "name",
        "status",
        "domain",
        "effective_url",
        "contact_url",
        "final_step_url",
        "reason",
        "evidence",
        "confidence_level",
    ]
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame(columns=cols)
    try:
        src = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    except Exception:
        return pd.DataFrame(columns=cols)

    if src.empty:
        return pd.DataFrame(columns=cols)

    source_cols = [str(c) for c in src.columns]
    name_col = _resolve_col_name(source_cols, ["学校名", "店名", "店舗名", "名称", "サロン名", "name"])
    url_col = _resolve_col_name(source_cols, ["URL", "url", "website", "site_url"])
    pref_col = _resolve_col_name(source_cols, ["都道府県", "prefecture", "エリア"])
    worker_col = _resolve_col_name(source_cols, ["ワーカー名", "担当", "worker"])
    date_col = _resolve_col_name(source_cols, ["問い合わせ日", "問合せ日", "連絡日", "date"])

    rows: list[dict[str, str]] = []
    for idx, row in src.iterrows():
        raw_name = str(row.get(name_col, "") if name_col else "").strip()
        raw_url = str(row.get(url_col, "") if url_col else "").strip()
        prefecture = str(row.get(pref_col, "") if pref_col else "").strip()
        worker = str(row.get(worker_col, "") if worker_col else "").strip()
        contact_date = str(row.get(date_col, "") if date_col else "").strip()
        is_valid_url = _is_http_url(raw_url)
        normalized_url = raw_url if is_valid_url else ""
        evidence_parts = []
        if prefecture:
            evidence_parts.append(f"prefecture={prefecture}")
        if contact_date:
            evidence_parts.append(f"contact_date={contact_date}")
        if worker:
            evidence_parts.append(f"worker={worker}")
        if raw_url and not is_valid_url:
            evidence_parts.append(f"raw_url={raw_url}")

        rows.append(
            {
                "id": f"aidnet-{idx + 1:04d}",
                "name": raw_name or f"Aidnet Row {idx + 1}",
                "status": "prepared_review_needed",
                "domain": _extract_domain(normalized_url),
                "effective_url": normalized_url,
                "contact_url": normalized_url,
                "final_step_url": normalized_url,
                "reason": "aidnet_non_url_entry" if raw_url and not is_valid_url else "aidnet_domain_list",
                "evidence": ";".join(evidence_parts),
                "confidence_level": "low",
            }
        )

    return pd.DataFrame(rows, columns=cols)


def _date_token(path: Path | None) -> str:
    if not path:
        return datetime.now(JST).strftime("%Y%m%d")
    m = re.search(r"(review_queue|submissions)_(\d{8})", path.name)
    return m.group(2) if m else datetime.now(JST).strftime("%Y%m%d")


def _operator_path(date_token: str) -> Path:
    return RESULTS_DIR / f"operator_actions_{date_token}.csv"


def _read_actions(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=ACTION_COLS)
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    except Exception:
        return pd.DataFrame(columns=ACTION_COLS)
    for c in ACTION_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[ACTION_COLS]


def _append_action(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a" if exists else "w", encoding="utf-8" if exists else "utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ACTION_COLS)
        if not exists:
            writer.writeheader()
        writer.writerow({k: str(row.get(k, "")).strip() for k in ACTION_COLS})


def _undo_last(path: Path) -> tuple[bool, str]:
    df = _read_actions(path)
    if df.empty:
        return False, "操作ログがありません。"
    rest = df.iloc[:-1].copy()
    if rest.empty:
        path.unlink(missing_ok=True)
        return True, "最後の操作を取り消しました。"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ACTION_COLS)
        writer.writeheader()
        writer.writerows(rest.to_dict(orient="records"))
    os.replace(tmp, path)
    return True, "最後の操作を取り消しました。"


def _tag(label: str) -> str:
    if label in {"GOOD_SPIRITUAL_SOLO", "OK_RELAX_BEAUTY"}:
        return "GOOD"
    if label == "EXCLUDE_CLEAR":
        return "EXCLUDE_CLEAR"
    return "BORDERLINE"


def _confidence(status: str, raw: str) -> str:
    r = str(raw).strip().lower()
    if r:
        return r
    if status in {"prepared_full", "sent"}:
        return "high"
    if status in {"prepared_partial", "prepared_external"}:
        return "medium"
    return "low"


def _open_file(path: Path) -> tuple[bool, str]:
    try:
        if not path.exists():
            return False, f"missing:{path}"
        if platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True, "opened"
    except Exception as e:
        return False, str(e)


def ensure_col(df: pd.DataFrame, name: str, default: str = "") -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="営業オペレーターダッシュボード", page_icon="🧭", layout="wide")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    settings = _read_json(SETTINGS_PATH, {})
    state = _read_json(STATE_PATH, {})
    leads = _load_leads(str(LEADS_PATH))
    aidnet_path_raw = str(settings.get("aidnet_domain_list_path", str(AIDNET_DEFAULT_PATH))).strip()
    aidnet_path = Path(aidnet_path_raw)
    if not aidnet_path.is_absolute():
        aidnet_path = ROOT / aidnet_path

    # -- Sidebar --
    with st.sidebar:
        st.markdown("### フィルタ・データソース")
        visibility = st.radio("表示モード", ["review", "strict"], format_func=lambda x: VISIBILITY_LABELS[x], index=0)
        source = st.radio("データソース", ["merged", "queue", "submissions", "aidnet"], format_func=lambda x: SOURCE_LABELS[x], index=0)

        queue_files = list_review_queue_files(RESULTS_DIR)
        sub_files = list_submissions_files(RESULTS_DIR)
        queue_path = latest_review_queue(RESULTS_DIR)
        sub_path = latest_submissions(RESULTS_DIR)
        if queue_files:
            queue_path = st.selectbox("Review Queue", queue_files, index=queue_files.index(queue_path) if queue_path in queue_files else 0, format_func=str)
        if sub_files:
            sub_path = st.selectbox("Submissions", sub_files, index=sub_files.index(sub_path) if sub_path in sub_files else 0, format_func=str)
        if source == "aidnet":
            st.caption(f"Aidnet CSV: {aidnet_path}")

    # -- Load data --
    qdf = load_queue_df(queue_path) if queue_path and queue_path.exists() else pd.DataFrame()
    sdf = load_submissions_df(sub_path) if sub_path and sub_path.exists() else pd.DataFrame()
    aidnet_df = _load_aidnet_rows(str(aidnet_path)) if source == "aidnet" else pd.DataFrame()
    if source == "queue":
        base = qdf
    elif source == "submissions":
        base = sdf
    elif source == "aidnet":
        base = aidnet_df
    else:
        base = merge_sources(qdf, sdf)
    if base.empty:
        if source == "aidnet":
            st.error(f"表示できるデータがありません。Aidnet CSVを確認してください: {aidnet_path}")
        else:
            st.error("表示できるデータがありません。review_queue / submissions を確認してください。")
        return

    actions_path = _operator_path(_date_token(queue_path if source != "submissions" else sub_path))
    actions_df = _read_actions(actions_path)
    latest_action = {str(r["salon_id"]).strip(): str(r["action"]).strip() for _, r in actions_df.iterrows()}
    actioned = {sid for sid, act in latest_action.items() if act in {"mark_sent", "mark_skip"}}

    # -- Enrich data --
    df = enrich_target_classification(base.copy())
    df["id"] = df["id"].astype(str).str.strip()
    df["name"] = df["name"].astype(str).str.strip()
    df["status"] = ensure_col(df, "status", "").astype(str).replace("", "unknown")
    df["domain"] = ensure_col(df, "domain", "").astype(str)
    df["effective_url"] = ensure_col(df, "effective_url", "").astype(str)
    df["contact_url"] = ensure_col(df, "contact_url", "").astype(str)
    df["final_step_url"] = ensure_col(df, "final_step_url", "").astype(str)
    df["reason"] = ensure_col(df, "reason", "").astype(str)
    df["confidence"] = [
        _confidence(s, c)
        for s, c in zip(
            df["status"].tolist(),
            ensure_col(df, "confidence_level", "").astype(str).tolist(),
        )
    ]
    df["tag"] = df["target_label"].astype(str).map(_tag)
    df["last_action"] = df["id"].map(lambda sid: latest_action.get(str(sid), ""))
    df["flags"] = df.apply(lambda r: flags_badge(detect_flags(r)), axis=1)
    df["candidate_links_count"] = df.apply(infer_candidate_links_count, axis=1)
    df["original_url"] = df["id"].map(lambda sid: leads.get(str(sid), {}).get("original_url", ""))
    df["demo_url"] = df["id"].map(lambda sid: leads.get(str(sid), {}).get("demo_url", ""))
    df["name"] = df["name"].where(df["name"].str.len() > 0, df["id"].map(lambda sid: leads.get(str(sid), {}).get("name", "")))
    df["domain"] = df["domain"].where(df["domain"].str.len() > 0, df["effective_url"].map(lambda u: urlparse(str(u)).netloc.lower() if str(u).strip() else ""))
    df["evidence"] = ensure_col(df, "evidence", "").astype(str)
    df["status_display"] = df["status"].map(format_status)
    df["search_blob"] = (df["name"] + " " + df["domain"] + " " + df["contact_url"] + " " + df["final_step_url"] + " " + df["reason"] + " " + df["evidence"]).str.lower()

    # -- Sidebar filters --
    all_status = sorted([s for s in df["status"].unique().tolist() if str(s).strip()])
    default_status = PREPARED_STATUSES if visibility == "strict" else all_status
    with st.sidebar:
        status_filter = st.multiselect("ステータス", options=all_status, default=default_status)
        tag_filter = st.multiselect("タグ", options=TAGS, default=TAGS if visibility == "review" else ["GOOD", "BORDERLINE"])
        search = st.text_input("🔍 検索（店名/ドメイン/URL/理由）", "")
        hide_actioned = st.checkbox("判定済みを非表示", value=True)
        hide_exclude = st.checkbox("EXCLUDE_CLEARを非表示", value=(visibility == "strict"))
        page_size = st.select_slider("1ページ件数", options=[10, 20, 30, 50, 100], value=20)

    # -- Apply filters --
    filtered = df.copy()
    if hide_actioned:
        filtered = filtered[~filtered["id"].isin(actioned)]
    if hide_exclude:
        filtered = filtered[filtered["tag"] != "EXCLUDE_CLEAR"]
    if status_filter:
        filtered = filtered[filtered["status"].isin(status_filter)]
    if tag_filter:
        filtered = filtered[filtered["tag"].isin(tag_filter)]
    if search.strip():
        filtered = filtered[filtered["search_blob"].str.contains(re.escape(search.strip().lower()), na=False)]
    filtered["_id_num"] = pd.to_numeric(filtered["id"], errors="coerce")
    filtered = filtered.sort_values(by=["_id_num", "id"], kind="stable").drop(columns=["_id_num"]).reset_index(drop=True)

    # -- Pagination --
    total_pages = max(1, math.ceil(max(1, len(filtered)) / page_size))
    if "page" not in st.session_state:
        st.session_state.page = 1
    if "selected_id" not in st.session_state:
        st.session_state.selected_id = ""
    st.session_state.page = min(max(1, int(st.session_state.page)), total_pages)
    with st.sidebar:
        st.session_state.page = int(st.number_input("ページ", min_value=1, max_value=total_pages, value=st.session_state.page, step=1))
        st.markdown("---")
        st.metric("日次送信上限", f"{int(state.get('today_count', 0) or 0)}/{int(settings.get('daily_limit', 10))}")
        st.metric("Prepared", int(df["status"].str.startswith("prepared", na=False).sum()))
        st.metric("表示件数", f"{len(filtered)}/{len(df)}")
        st.caption(f"操作ログ: `{actions_path}`")
        st.markdown("---")
        st.markdown(
            '<div style="font-size:0.8em;color:#64748b;">'
            "<b>操作ヒント</b><br>"
            "テーブルの行をクリックで選択<br>"
            "Prefill → 確認 → Sent/Skip"
            "</div>",
            unsafe_allow_html=True,
        )

    if filtered.empty:
        st.warning("条件に一致するリードがありません。")
        return

    # ===================================================================
    # Dashboard header: metrics bar
    # ===================================================================
    st.markdown("## 営業オペレーターダッシュボード")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("要確認", int((df["status"] == "prepared_review_needed").sum()))
    m2.metric("完全準備済み", int((df["status"] == "prepared_full").sum()))
    m3.metric("部分入力", int((df["status"] == "prepared_partial").sum()))
    today_sent = int(actions_df[actions_df["action"] == "mark_sent"].drop_duplicates(subset=["salon_id"], keep="last").shape[0]) if not actions_df.empty else 0
    today_skip = int(actions_df[actions_df["action"] == "mark_skip"].drop_duplicates(subset=["salon_id"], keep="last").shape[0]) if not actions_df.empty else 0
    m4.metric("本日送信", today_sent)
    m5.metric("本日スキップ", today_skip)

    # ===================================================================
    # Main layout: lead list (left) + detail panel (right)
    # ===================================================================
    start = (st.session_state.page - 1) * page_size
    page_df = filtered.iloc[start : start + page_size].copy().reset_index(drop=True)
    if not st.session_state.selected_id or st.session_state.selected_id not in set(filtered["id"].tolist()):
        st.session_state.selected_id = str(filtered.iloc[0]["id"])

    c1, c2 = st.columns([1.4, 1.0], gap="large")

    # ---------------------------------------------------------------
    # Left column: interactive lead table
    # ---------------------------------------------------------------
    with c1:
        st.markdown(
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
            f'<span style="font-size:1.1rem;font-weight:700;color:#1e293b;">リード一覧</span>'
            f'<span style="font-size:0.85em;color:#64748b;">ページ {st.session_state.page}/{total_pages} ({len(filtered)}件)</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        show = page_df[["id", "name", "domain", "status_display", "confidence", "tag", "reason", "last_action"]].rename(
            columns={"id": "ID", "name": "店名", "domain": "ドメイン", "status_display": "ステータス", "confidence": "信頼度", "tag": "タグ", "reason": "理由", "last_action": "最終操作"}
        )

        # Pre-select the current row in the dataframe
        current_row_idx = None
        for i, rid in enumerate(page_df["id"].tolist()):
            if str(rid) == str(st.session_state.selected_id):
                current_row_idx = i
                break

        default_selection = [current_row_idx] if current_row_idx is not None else []

        event = st.dataframe(
            show,
            use_container_width=True,
            hide_index=True,
            height=520,
            on_select="rerun",
            selection_mode="single-row",
            key="lead_table",
        )

        # Handle row selection
        selected_rows = event.selection.rows if event and event.selection else []
        if selected_rows:
            selected_idx = selected_rows[0]
            if selected_idx < len(page_df):
                new_id = str(page_df.iloc[selected_idx]["id"])
                if new_id != st.session_state.selected_id:
                    st.session_state.selected_id = new_id

        # Pagination buttons
        p1, p2, p3 = st.columns([1, 2, 1])
        with p1:
            if st.button("← 前ページ", use_container_width=True, disabled=(st.session_state.page <= 1)):
                st.session_state.page -= 1
                st.rerun()
        with p3:
            if st.button("次ページ →", use_container_width=True, disabled=(st.session_state.page >= total_pages)):
                st.session_state.page += 1
                st.rerun()

    # ---------------------------------------------------------------
    # Right column: detail panel
    # ---------------------------------------------------------------
    row = filtered[filtered["id"].astype(str) == str(st.session_state.selected_id)].iloc[0]
    row_id = str(row["id"])
    is_aidnet_source = source == "aidnet"
    status = str(row.get("status", ""))
    tag = str(row.get("tag", ""))
    confidence = str(row.get("confidence", ""))
    target_url = str(row.get("final_step_url", "")).strip() or str(row.get("contact_url", "")).strip()
    date_hint = parse_queue_date(queue_path) if queue_path and queue_path.exists() else datetime.now(JST).strftime("%Y%m%d")
    shots = discover_screenshots(SCREENSHOTS_DIR, row_id, date_hint)
    shot_folder = next((p.parent for p in shots.values() if p is not None), SCREENSHOTS_DIR / date_hint)

    with c2:
        # Detail header with badges
        salon_name = row.get("name", "") or row_id
        badges_html = " ".join([
            status_badge_html(status),
            tag_badge_html(tag),
            confidence_dot(confidence),
        ])
        st.markdown(
            f'<div class="detail-header">'
            f'<h2>{salon_name}</h2>'
            f'<div class="badges">{badges_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Review warning
        if status == "prepared_review_needed" or tag == "EXCLUDE_CLEAR":
            st.markdown(
                '<div class="review-warning">⚠ 要確認: このリードは目視チェックを推奨します。</div>',
                unsafe_allow_html=True,
            )

        # Evidence
        evidence_val = str(row.get("evidence", "")).strip()
        if evidence_val and evidence_val != "-":
            st.markdown(
                f'<div class="evidence-box"><b>Evidence:</b> {evidence_val}</div>',
                unsafe_allow_html=True,
            )

        # URL section
        urls = [
            ("ドメイン", str(row.get("domain", "") or "-"), False),
            ("問い合わせURL", str(row.get("contact_url", "")), True),
            ("最終ステップURL", target_url, True),
        ]
        for label, value, is_link in urls:
            if value and value != "-":
                if is_link:
                    st.markdown(
                        f'<div class="url-section"><div class="url-label">{label}</div><a href="{value}" target="_blank">{value}</a></div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div class="url-section"><div class="url-label">{label}</div>{value}</div>',
                        unsafe_allow_html=True,
                    )

        # Expandable: additional URLs
        with st.expander("その他URL", expanded=False):
            orig = str(row.get("original_url", "")).strip()
            demo = str(row.get("demo_url", "")).strip()
            if orig:
                st.markdown(f"**元URL:** {orig}")
            else:
                st.markdown("**元URL:** -")
            if demo:
                st.markdown(f"**デモURL:** {demo}")
            else:
                st.markdown("**デモURL:** -")

        # Technical details
        with st.expander("技術詳細", expanded=False):
            st.write(f"**stop_state:** `{row.get('stop_state', '') or '-'}`")
            st.write(f"**missing_required_fields:** `{row.get('missing_required_fields', '') or '-'}`")
            st.write(f"**detected_platform:** `{row.get('detected_platform', '') or '-'}`")
            st.write(f"**notes:** {row.get('notes', '') or row.get('reason', '') or '-'}")
            st.write(f"**flags:** {row.get('flags', '') or '-'}")

        # ---------------------------------------------------------------
        # Action buttons
        # ---------------------------------------------------------------
        st.markdown("---")
        if is_aidnet_source:
            st.info("Aidnet CSV表示では Prefill は利用可能です。Sent/Skip は無効です。")

        if st.button(
            "▶ Run Playwright Prefill（送信なし）",
            type="primary",
            use_container_width=True,
            disabled=not target_url,
        ):
            with st.spinner("Playwrightを実行中..."):
                prefill_queue = Path("") if is_aidnet_source else (queue_path or Path(""))
                code, out, err, payload = run_prefill_subprocess(row_id, prefill_queue, final_url=target_url, keep_open=True)
            _append_action(actions_path, {"timestamp": datetime.now(JST).isoformat(timespec="seconds"), "salon_id": row_id, "salon_name": row.get("name", ""), "domain": row.get("domain", ""), "status": status, "action": "open_prefill", "reason": payload.get("reason", "") if payload else "", "note": f"exit={code}", "final_step_url": target_url, "stop_state": payload.get("stop_state", "") if payload else ""})
            if code == 0:
                st.success(f"完了: status={payload.get('status','')} stop_state={payload.get('stop_state','')}")
            elif code == 2:
                st.warning(f"スキップ: {payload.get('reason', 'skipped') if payload else 'skipped'}")
            else:
                st.error(f"失敗: {payload.get('reason', err[:160]) if payload else err[:160]}")
            if out.strip():
                st.code(out.strip(), language="text")
            if err.strip():
                st.code(err.strip(), language="text")

        # URL open buttons
        b1, b2, b3 = st.columns(3)
        if b1.button("🔗 Demo", use_container_width=True, disabled=not row.get("demo_url", "")):
            open_url(str(row.get("demo_url", "")))
        if b2.button("🔗 Original", use_container_width=True, disabled=not row.get("original_url", "")):
            open_url(str(row.get("original_url", "")))
        if b3.button("🔗 Contact", use_container_width=True, disabled=not row.get("contact_url", "")):
            open_url(str(row.get("contact_url", "")))

        # Judgment section
        st.markdown("##### 判定")
        skip_reason = st.selectbox("Skip理由", ["address_required", "login_required", "bot_protection", "dead_site", "other"])
        note = st.text_input("メモ", value="")

        ids = filtered["id"].astype(str).tolist()
        next_id = ids[min(ids.index(row_id) + 1, len(ids) - 1)] if row_id in ids else ids[0]

        d1, d2, d3 = st.columns(3)
        if d1.button("✅ Sent", use_container_width=True, type="primary", disabled=is_aidnet_source):
            _append_action(actions_path, {"timestamp": datetime.now(JST).isoformat(timespec="seconds"), "salon_id": row_id, "salon_name": row.get("name", ""), "domain": row.get("domain", ""), "status": status, "action": "mark_sent", "reason": "", "note": note, "final_step_url": target_url, "stop_state": row.get("stop_state", "")})
            st.session_state.selected_id = next_id
            st.rerun()
        if d2.button("⏭ Skip", use_container_width=True, disabled=is_aidnet_source):
            _append_action(actions_path, {"timestamp": datetime.now(JST).isoformat(timespec="seconds"), "salon_id": row_id, "salon_name": row.get("name", ""), "domain": row.get("domain", ""), "status": status, "action": "mark_skip", "reason": skip_reason, "note": note, "final_step_url": target_url, "stop_state": row.get("stop_state", "")})
            st.session_state.selected_id = next_id
            st.rerun()
        if d3.button("↩ Undo", use_container_width=True):
            ok, msg = _undo_last(actions_path)
            (st.toast if ok else st.warning)(msg)
            if ok:
                st.rerun()

        if st.button("📂 スクリーンショットフォルダを開く", use_container_width=True):
            open_folder(shot_folder)

    # ===================================================================
    # Bottom section: screenshots + guide
    # ===================================================================
    st.markdown("---")

    with st.expander("操作ガイド", expanded=False):
        st.markdown("""
**基本フロー:**
1. 左でフィルタ（表示モード / ステータス / タグ / 検索）
2. テーブルの行をクリックでリード選択
3. 右で「Run Playwright Prefill」を実行
4. スクショ確認後「Sent」または「Skip」で記録

**ステータス凡例:**
- **完全入力済み**: フォーム入力完了、送信ボタン検出済み
- **部分入力済み**: 一部フィールド未充足
- **外部フォーム**: Google Forms等の外部サービス
- **要確認**: 自動処理で判断不能、目視確認必要
- **ログイン必須 / Bot保護 / サイト不通**: 自動スキップ対象
        """)

    st.markdown(
        f'<div style="font-size:0.9em;font-weight:600;color:#1e293b;margin-bottom:8px;">スクリーンショット（ID: {row_id}）</div>',
        unsafe_allow_html=True,
    )
    cols = st.columns(4)
    for i, step in enumerate(["01", "02", "03", "04"]):
        with cols[i]:
            st.caption(f"{step}: {SCREENSHOT_LABELS.get(step, step)}")
            p = shots.get(step)
            if p and p.exists():
                st.image(str(p), use_container_width=True)
                if st.button(f"{step}を開く", key=f"shot_{row_id}_{step}", use_container_width=True):
                    _open_file(p)
            else:
                st.info("未取得")


if __name__ == "__main__":
    main()
