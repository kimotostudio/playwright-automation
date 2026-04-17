from __future__ import annotations

from typing import Iterable

TARGET_GOOD_SPIRITUAL_SOLO = "GOOD_SPIRITUAL_SOLO"
TARGET_OK_RELAX_BEAUTY = "OK_RELAX_BEAUTY"
TARGET_BORDERLINE = "BORDERLINE"
TARGET_EXCLUDE_CLEAR = "EXCLUDE_CLEAR"

EXCLUDE_MEDICAL = "medical_clinical"
EXCLUDE_PORTAL = "portal_listing"
EXCLUDE_ASSOCIATION = "association_school"
EXCLUDE_GYM_BODY = "gym_seikotsu_shinkyu"
EXCLUDE_RELIGION = "religion_organization"
EXCLUDE_OTHER = "other_non_salon"

EXCLUDE_REASON_KEYS = [
    EXCLUDE_MEDICAL,
    EXCLUDE_PORTAL,
    EXCLUDE_ASSOCIATION,
    EXCLUDE_GYM_BODY,
    EXCLUDE_RELIGION,
    EXCLUDE_OTHER,
]

MEDICAL_TOKENS = [
    "クリニック",
    "心療内科",
    "精神科",
    "医療法人",
    "病院",
    "診療所",
    "臨床心理",
    "公認心理師",
    "心理相談室",
    "カウンセリングオフィス",
    "相談センター",
]

PORTAL_DOMAIN_TOKENS = [
    "hotpepper.jp",
    "ekiten.jp",
    "my-best.com",
    "jmty.jp",
    "ameba.jp",
    "ameblo.jp",
    "amebaownd.com",
    "mise-repo.com",
    "setsuritsu-senmon.com",
]

ASSOCIATION_TOKENS = ["協会", "連盟", "スクール", "講座", "教室", "道場", "団体"]
GYM_BODY_TOKENS = ["整骨院", "整体", "鍼灸", "ジム", "パーソナルトレーニング"]
RELIGION_TOKENS = ["教会", "寺院", "神社", "宗教", "金光教"]
OTHER_NON_SALON_TOKENS = ["ギター教室", "料理教室", "学習支援", "英会話"]

POSITIVE_TOKENS = [
    "ヒーリング",
    "スピリチュアル",
    "チャネリング",
    "占い",
    "アロマ",
    "よもぎ蒸し",
    "リラクゼーション",
    "プライベートサロン",
    "自宅サロン",
    "女性専用",
]

SOLO_HINTS = ["個人", "ひとり", "一人"]
CORPORATE_TOKENS = ["株式会社", "有限会社", "合同会社", "inc", "llc"]


def _collect_texts(lead: dict) -> tuple[str, str, str]:
    name = str(
        lead.get("name")
        or lead.get("salon_name")
        or lead.get("店名")
        or lead.get("店舗名")
        or lead.get("名称")
        or ""
    )
    domain = str(lead.get("domain") or "").lower()
    url = str(
        lead.get("url")
        or lead.get("contact_url")
        or lead.get("effective_url")
        or lead.get("url_old")
        or lead.get("url(旧)")
        or ""
    ).lower()
    return name, domain, url


def _add_matches(
    container: list[str],
    texts: Iterable[str],
    tokens: list[str],
    prefix: str,
) -> list[str]:
    matched: list[str] = []
    lower_texts = [str(t).lower() for t in texts]
    for token in tokens:
        token_l = token.lower()
        if any(token_l in t for t in lower_texts):
            matched.append(token)
            container.append(f"{prefix}:{token}")
    return matched


def classify_lead(lead: dict) -> dict:
    name, domain, url = _collect_texts(lead)
    joined = " ".join([name, domain, url]).lower()
    debug_tokens: list[str] = []
    reasons: list[str] = []
    score = 0

    medical_hits = _add_matches(debug_tokens, [name, domain, url], MEDICAL_TOKENS, "medical")
    if medical_hits:
        reasons.append(EXCLUDE_MEDICAL)
        score -= 90

    portal_hits = _add_matches(debug_tokens, [domain, url], PORTAL_DOMAIN_TOKENS, "portal")
    if portal_hits:
        reasons.append(EXCLUDE_PORTAL)
        score -= 80

    assoc_hits = _add_matches(debug_tokens, [name, domain, url], ASSOCIATION_TOKENS, "association")
    if assoc_hits:
        reasons.append(EXCLUDE_ASSOCIATION)
        score -= 45

    gym_hits = _add_matches(debug_tokens, [name, domain, url], GYM_BODY_TOKENS, "body")
    if gym_hits:
        reasons.append(EXCLUDE_GYM_BODY)
        score -= 25

    religion_hits = _add_matches(debug_tokens, [name, domain, url], RELIGION_TOKENS, "religion")
    if religion_hits:
        reasons.append(EXCLUDE_RELIGION)
        score -= 35

    other_hits = _add_matches(debug_tokens, [name, domain, url], OTHER_NON_SALON_TOKENS, "other")
    if other_hits:
        reasons.append(EXCLUDE_OTHER)
        score -= 25

    positive_hits = _add_matches(debug_tokens, [name, domain, url], POSITIVE_TOKENS, "positive")
    score += min(len(positive_hits) * 18, 60)
    if "サロン" in joined:
        debug_tokens.append("positive:サロン")
        score += 10
    if "サロン" in joined and any(k in joined for k in ["ヒーリング", "スピリチュアル", "チャネリング", "占い"]):
        debug_tokens.append("positive:スピリチュアル系サロン")
        score += 20
    if any(h in joined for h in SOLO_HINTS):
        debug_tokens.append("positive:個人運営")
        score += 8
    if not portal_hits and any(c in joined for c in CORPORATE_TOKENS):
        debug_tokens.append("negative:corporate_hint")
        score -= 15

    score = max(-100, min(100, score))
    reason_set = sorted(set(reasons))

    if EXCLUDE_MEDICAL in reason_set:
        label = TARGET_EXCLUDE_CLEAR
    elif EXCLUDE_PORTAL in reason_set and score <= -20:
        label = TARGET_EXCLUDE_CLEAR
    elif score >= 45 and not reason_set:
        label = TARGET_GOOD_SPIRITUAL_SOLO
    elif score >= 15 and EXCLUDE_MEDICAL not in reason_set and EXCLUDE_PORTAL not in reason_set:
        label = TARGET_OK_RELAX_BEAUTY
    elif score <= -40:
        label = TARGET_EXCLUDE_CLEAR
    else:
        label = TARGET_BORDERLINE

    return {
        "target_label": label,
        "exclude_reason": reason_set,
        "target_score": int(score),
        "debug_tokens": debug_tokens[:24],
    }
