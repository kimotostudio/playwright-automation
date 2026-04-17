"""Local-only staff review dashboard.

Run:
    python app.py
"""

from __future__ import annotations

import csv
import glob
import os
import re
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Locator, Page, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
DATA_DIR = PROJECT_ROOT / "data"
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"

# Append-only action log schema.
ACTION_FIELDS = ["timestamp", "salon_id", "salon_name", "domain", "action", "note"]
DONE_ACTIONS = {"mark_ok", "mark_bad", "mark_skip"}


def normalize_col(name: str) -> str:
    """Normalize column names for robust CSV mapping."""
    return re.sub(r"[\s_\-()（）・]+", "", str(name).strip().lower())


def resolve_column(columns: List[str], aliases: List[str]) -> Optional[str]:
    """Resolve actual CSV column from alias list."""
    mapping = {normalize_col(c): c for c in columns}
    for alias in aliases:
        key = normalize_col(alias)
        if key in mapping:
            return mapping[key]
    return None


def pick_latest_review_queue() -> Optional[Path]:
    files = sorted(RESULTS_DIR.glob("review_queue_*.csv"))
    if not files:
        return None
    canonical = [f for f in files if re.fullmatch(r"review_queue_\d{8}\.csv", f.name)]
    return canonical[-1] if canonical else files[-1]


def parse_date_from_filename(path: Path) -> str:
    m = re.search(r"review_queue_(\d{8})", path.name)
    return m.group(1) if m else datetime.now().strftime("%Y%m%d")


def action_csv_path(date_str: str) -> Path:
    return DATA_DIR / f"staff_actions_{date_str}.csv"


@st.cache_data(show_spinner=False)
def load_queue(path: str, mtime: float) -> pd.DataFrame:
    """Load review queue with robust mapping."""
    _ = mtime
    src = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    cols = list(src.columns)

    id_col = resolve_column(cols, ["salon_id", "id", "ID"])
    name_col = resolve_column(cols, ["salon_name", "店名", "店舗名", "名称", "name"])
    final_col = resolve_column(cols, ["final_step_url", "url", "URL"])
    contact_col = resolve_column(cols, ["contact_url", "url", "URL"])
    domain_col = resolve_column(cols, ["domain"])
    reason_col = resolve_column(cols, ["reason", "message", "notes"])
    status_col = resolve_column(cols, ["status"])

    out = pd.DataFrame(index=src.index)
    out["id"] = src[id_col].astype(str).str.strip() if id_col else (src.index + 1).astype(str)
    out["name"] = src[name_col].astype(str).str.strip() if name_col else ""
    out["final_step_url"] = src[final_col].astype(str).str.strip() if final_col else ""
    out["contact_url"] = src[contact_col].astype(str).str.strip() if contact_col else ""
    out["reason"] = src[reason_col].astype(str).str.strip() if reason_col else ""
    out["status"] = src[status_col].astype(str).str.strip() if status_col else ""

    if domain_col:
        out["domain"] = src[domain_col].astype(str).str.strip()
    else:
        out["domain"] = out["final_step_url"].map(
            lambda u: urlparse(str(u)).netloc.lower() if str(u).strip() else ""
        )

    final_norm = out["final_step_url"].astype(str).str.strip().str.lower().str.rstrip("/")
    contact_norm = out["contact_url"].astype(str).str.strip().str.lower().str.rstrip("/")
    same_as_contact = (final_norm != "") & (final_norm == contact_norm)
    out["effective_url"] = out["contact_url"].where((out["final_step_url"].str.len() == 0) | same_as_contact, out["final_step_url"])
    out["search_blob"] = (
        out["name"].astype(str)
        + " "
        + out["domain"].astype(str)
        + " "
        + out["effective_url"].astype(str)
        + " "
        + out["reason"].astype(str)
    ).str.lower()
    return out


@st.cache_data(show_spinner=False)
def load_actions(path: str, mtime: float) -> pd.DataFrame:
    _ = mtime
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=ACTION_FIELDS)
    return pd.read_csv(p, encoding="utf-8-sig", dtype=str).fillna("")


def latest_action_map(actions: pd.DataFrame) -> Dict[str, str]:
    latest: Dict[str, str] = {}
    if actions.empty:
        return latest
    for _, row in actions.iterrows():
        sid = str(row.get("salon_id", "")).strip()
        act = str(row.get("action", "")).strip()
        if sid:
            latest[sid] = act
    return latest


def append_action(path: Path, salon_id: str, salon_name: str, domain: str, action: str, note: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "salon_id": salon_id,
        "salon_name": salon_name,
        "domain": domain,
        "action": action,
        "note": note,
    }

    if not path.exists():
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=ACTION_FIELDS)
            writer.writeheader()
            writer.writerow(row)
        return

    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ACTION_FIELDS)
        writer.writerow(row)


def screenshot_priority(path: Path) -> tuple[int, str]:
    n = path.name
    if "_04_" in n:
        return (0, n)
    if "_03_" in n:
        return (1, n)
    if "_02_" in n:
        return (2, n)
    if "_01_" in n:
        return (3, n)
    return (4, n)


def find_screenshots(salon_id: str) -> List[Path]:
    pattern = str(SCREENSHOTS_DIR / "**" / f"{salon_id}_*.png")
    found = [Path(p) for p in glob.glob(pattern, recursive=True)]
    found.sort(key=screenshot_priority)
    return found


def split_screenshots(paths: List[Path]) -> Dict[str, List[Path]]:
    grouped: Dict[str, List[Path]] = {
        "Before fill": [],
        "After fill": [],
        "Before submit/confirm": [],
        "Confirmation page": [],
    }
    for p in paths:
        name = p.name
        if "_01_" in name:
            grouped["Before fill"].append(p)
        elif "_02_" in name:
            grouped["After fill"].append(p)
        elif "_03_" in name:
            grouped["Before submit/confirm"].append(p)
        elif "_04_" in name:
            grouped["Confirmation page"].append(p)
    return {k: v for k, v in grouped.items() if v}


def open_url(url: str) -> None:
    if url:
        webbrowser.open(url, new=2)


def first_locator(locators: List[Locator]) -> Optional[Locator]:
    for loc in locators:
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def fill_or_select(loc: Locator, value: str) -> bool:
    try:
        tag = str(loc.evaluate("el => (el.tagName || '').toLowerCase()"))
    except Exception:
        tag = ""

    if tag == "select":
        try:
            loc.select_option(value=value)
            return True
        except Exception:
            try:
                options = loc.locator("option")
                for i in range(options.count()):
                    opt = options.nth(i)
                    text = (opt.inner_text() or "").strip()
                    opt_value = (opt.get_attribute("value") or "").strip()
                    if value in {text, opt_value}:
                        loc.select_option(value=opt_value or None, label=text or None)
                        return True
            except Exception:
                pass
            return False

    try:
        loc.fill(value)
        return True
    except Exception:
        try:
            loc.click()
            loc.press("Control+a")
            loc.type(value, delay=15)
            return True
        except Exception:
            return False


def fill_text_with_labels(page: Page, labels: List[str], value: str) -> bool:
    for label in labels:
        try:
            loc = page.get_by_label(re.compile(label, re.IGNORECASE))
            if loc.count() > 0 and fill_or_select(loc.first, value):
                return True
        except Exception:
            continue
    return False


def parse_birth(value: str) -> tuple[str, str, str]:
    text = (value or "").strip().replace("/", "-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if not m:
        return "", "", ""
    y, mm, dd = m.group(1), f"{int(m.group(2)):02d}", f"{int(m.group(3)):02d}"
    return y, mm, dd


def fill_birth(page: Page, birth: str) -> bool:
    y, mm, dd = parse_birth(birth)
    if not y:
        return False
    iso = f"{y}-{mm}-{dd}"

    if fill_text_with_labels(page, ["生年月日", "誕生日", "birth"], iso):
        return True

    try:
        date_input = page.locator("input[type='date']")
        if date_input.count() > 0 and fill_or_select(date_input.first, iso):
            return True
    except Exception:
        pass

    year_loc = first_locator(
        [
            page.get_by_label(re.compile(r"(年|year)", re.IGNORECASE)),
            page.locator("select[name*='year' i], input[name*='year' i], select[id*='year' i], input[id*='year' i], select[name*='年' i], input[name*='年' i]"),
        ]
    )
    month_loc = first_locator(
        [
            page.get_by_label(re.compile(r"(月|month)", re.IGNORECASE)),
            page.locator("select[name*='month' i], input[name*='month' i], select[id*='month' i], input[id*='month' i], select[name*='月' i], input[name*='月' i]"),
        ]
    )
    day_loc = first_locator(
        [
            page.get_by_label(re.compile(r"(日|day)", re.IGNORECASE)),
            page.locator("select[name*='day' i], input[name*='day' i], select[id*='day' i], input[id*='day' i], select[name*='日' i], input[name*='日' i]"),
        ]
    )
    ok = False
    if year_loc:
        ok = fill_or_select(year_loc, y) or ok
    if month_loc:
        ok = fill_or_select(month_loc, str(int(mm))) or fill_or_select(month_loc, mm) or ok
    if day_loc:
        ok = fill_or_select(day_loc, str(int(dd))) or fill_or_select(day_loc, dd) or ok
    return bool(ok and year_loc and month_loc and day_loc)


def resolve_prefill_payload(record: dict) -> dict:
    payload = {
        "name": "KIMOTO STUDIO",
        "email": "kimoto.studio21@gmail.com",
        "birth": "1990-01-01",
    }
    for key in ("name", "email", "birth"):
        value = str(record.get(key, "")).strip()
        if value:
            payload[key] = value
    return payload


def open_and_prefill(url: str, payload: dict, debug_path: Path) -> dict:
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(locale="ja-JP")
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        fill_text_with_labels(page, ["お名前", "氏名", "名前", "name"], str(payload.get("name", "")).strip())
        fill_text_with_labels(page, ["メールアドレス", "メール", "email", "e-mail"], str(payload.get("email", "")).strip())
        fill_birth(page, str(payload.get("birth", "")).strip())
        page.wait_for_timeout(800)
        return {"playwright": playwright, "browser": browser, "context": context, "page": page}
    except Exception:
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(debug_path), full_page=True)
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass
        raise


def open_folder(path: Path) -> None:
    if not path.exists():
        return
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if os.sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])


def next_id(filtered_ids: List[str], current_id: str) -> Optional[str]:
    if not filtered_ids:
        return None
    try:
        idx = filtered_ids.index(current_id)
    except ValueError:
        return filtered_ids[0]
    if idx + 1 < len(filtered_ids):
        return filtered_ids[idx + 1]
    return None


def main() -> None:
    st.set_page_config(page_title="Review Dashboard", layout="wide")
    st.title("Review Dashboard")

    queue_default = str(pick_latest_review_queue() or (RESULTS_DIR / "review_queue_YYYYMMDD.csv"))
    queue_input = st.text_input("review_queue CSV", value=queue_default)
    queue_path = Path(queue_input)
    if not queue_path.exists():
        st.error(f"Queue file not found: {queue_path}")
        st.stop()

    queue_df = load_queue(str(queue_path), queue_path.stat().st_mtime)
    if queue_df.empty:
        st.warning("Queue is empty")
        st.stop()

    date_str = parse_date_from_filename(queue_path)
    actions_path = action_csv_path(date_str)
    actions_df = load_actions(str(actions_path), actions_path.stat().st_mtime if actions_path.exists() else 0.0)
    action_map = latest_action_map(actions_df)

    queue_df["last_action"] = queue_df["id"].map(lambda x: action_map.get(str(x), ""))
    queue_df["is_actioned"] = queue_df["last_action"].isin(DONE_ACTIONS)

    if "selected_id" not in st.session_state:
        st.session_state.selected_id = str(queue_df.iloc[0]["id"])
    if "preview_image" not in st.session_state:
        st.session_state.preview_image = ""
    if "_prefill_sessions" not in st.session_state:
        st.session_state._prefill_sessions = []

    left, right = st.columns([1.2, 1.0], vertical_alignment="top")

    with left:
        st.subheader("Lead List")
        search = st.text_input("Search", placeholder="name / domain / URL / reason").strip().lower()
        prepared_only = st.checkbox("prepared only", value=True)
        hide_actioned = st.checkbox("hide already actioned", value=True)
        exclude_bot = st.checkbox("exclude bot_protection/captcha", value=False)
        exclude_login = st.checkbox("exclude login_required", value=False)

        filtered = queue_df.copy()
        if prepared_only:
            filtered = filtered[filtered["status"].str.lower() == "prepared"]
        if hide_actioned:
            filtered = filtered[~filtered["is_actioned"]]
        if exclude_bot:
            filtered = filtered[~filtered["reason"].str.lower().str.contains("bot_protection|captcha", regex=True)]
        if exclude_login:
            filtered = filtered[~filtered["reason"].str.lower().str.contains("login_required|requires_login|login", regex=True)]
        if search:
            filtered = filtered[filtered["search_blob"].str.contains(re.escape(search), regex=True)]

        if filtered.empty:
            st.success("All done")
            st.stop()

        table = filtered[["id", "name", "domain", "reason", "status", "last_action"]]
        selected = st.dataframe(table, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row")

        if selected and selected.selection and selected.selection.rows:
            row_idx = int(selected.selection.rows[0])
            st.session_state.selected_id = str(filtered.iloc[row_idx]["id"])
        elif st.session_state.selected_id not in set(filtered["id"].astype(str)):
            st.session_state.selected_id = str(filtered.iloc[0]["id"])

    picked_rows = queue_df[queue_df["id"].astype(str) == str(st.session_state.selected_id)]
    picked = picked_rows.iloc[-1] if not picked_rows.empty else queue_df.iloc[0]

    with right:
        salon_id = str(picked["id"]).strip()
        salon_name = str(picked["name"]).strip()
        domain = str(picked["domain"]).strip()
        contact_url = str(picked["contact_url"]).strip()
        final_step_url_raw = str(picked["final_step_url"]).strip()
        effective_url = str(picked["effective_url"]).strip()
        final_step_url = effective_url if (not final_step_url_raw or final_step_url_raw.rstrip("/").lower() == contact_url.rstrip("/").lower()) else final_step_url_raw
        reason = str(picked["reason"]).strip()
        status = str(picked["status"]).strip()

        st.subheader(salon_name or f"Lead {salon_id}")
        st.write(f"**Domain**: {domain or '-'}")
        st.write(f"**contact_url**: {contact_url or '-'}")
        st.write(f"**final_step_url**: {final_step_url or '-'}")
        st.write(f"**reason**: {reason or '-'}")
        st.write(f"**status**: {status or '-'}")

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(
                "Open & Prefill (no submit)",
                use_container_width=True,
                type="primary",
                disabled=not bool(final_step_url),
            ):
                if not final_step_url:
                    st.warning("final_step_url is missing")
                else:
                    debug_path = RESULTS_DIR / "debug_prefill_failed.png"
                    try:
                        payload = resolve_prefill_payload(picked.to_dict())
                        session = open_and_prefill(final_step_url, payload, debug_path=debug_path)
                        st.session_state._prefill_sessions.append(session)
                        append_action(actions_path, salon_id, salon_name, domain, "open_final_url", final_step_url)
                    except PlaywrightTimeoutError as e:
                        st.error(f"Prefill failed (timeout): {e}")
                        st.warning(f"Debug screenshot: {debug_path}")
                    except Exception as e:
                        st.error(f"Prefill failed: {e}")
                        st.warning(f"Debug screenshot: {debug_path}")
        with c2:
            if st.button("Open contact URL", use_container_width=True):
                if contact_url:
                    append_action(actions_path, salon_id, salon_name, domain, "open_contact_url", contact_url)
                    open_url(contact_url)
                else:
                    st.warning("contact_url is missing")
        with c3:
            if st.button("Open screenshots folder", use_container_width=True):
                shots = find_screenshots(salon_id)
                folder = shots[0].parent if shots else SCREENSHOTS_DIR
                append_action(actions_path, salon_id, salon_name, domain, "open_screenshots", str(folder))
                open_folder(folder)

        st.markdown("### Screenshots")
        shots = find_screenshots(salon_id)
        grouped = split_screenshots(shots)
        if not grouped:
            st.warning("No screenshots found")
        else:
            tabs = st.tabs(list(grouped.keys()))
            for tab, (tab_name, images) in zip(tabs, grouped.items()):
                with tab:
                    for i, img in enumerate(images):
                        ic, pc = st.columns([5, 1])
                        with ic:
                            st.image(str(img), caption=img.name, width="stretch")
                        with pc:
                            if st.button("Preview", key=f"preview_{salon_id}_{tab_name}_{i}"):
                                st.session_state.preview_image = str(img)
                    if images and not st.session_state.preview_image:
                        st.session_state.preview_image = str(images[0])
            if st.session_state.preview_image:
                st.markdown("#### Large preview")
                st.image(st.session_state.preview_image, width="stretch")

        st.markdown("### Decision")
        skip_reason = st.radio(
            "skip_reason",
            ["address_required", "login_required", "bot_protection", "other"],
            horizontal=True,
        )
        note = st.text_input("note", value="")

        d1, d2, d3, d4 = st.columns(4)
        with d1:
            ok = st.button("OK", use_container_width=True, type="primary")
        with d2:
            manual = st.button("Needs manual", use_container_width=True)
        with d3:
            bad = st.button("Bad lead", use_container_width=True)
        with d4:
            skip = st.button("Skip", use_container_width=True)

        action = ""
        action_note = note.strip()
        if ok:
            action = "mark_ok"
        elif manual:
            action = "mark_needs_manual"
        elif bad:
            action = "mark_bad"
        elif skip:
            action = "mark_skip"
            action_note = f"skip_reason={skip_reason}" + (f" | {action_note}" if action_note else "")

        if action:
            append_action(actions_path, salon_id, salon_name, domain, action, action_note)
            ids = filtered["id"].astype(str).tolist() if "filtered" in locals() else []
            nxt = next_id(ids, salon_id)
            if nxt is None:
                st.success("All done")
                st.stop()
            st.session_state.selected_id = str(nxt)
            st.session_state.preview_image = ""
            st.rerun()


if __name__ == "__main__":
    main()
