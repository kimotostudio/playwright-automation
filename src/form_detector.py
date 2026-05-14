"""Form detection and auto-fill for Japanese contact forms.

Implements robust selector strategies and safe SEMI_AUTO behavior support.
"""

from __future__ import annotations

import asyncio
import logging
from html.parser import HTMLParser
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

CONTACT_PATH_PATTERNS = [
    "/contact",
    "/inquiry",
    "/toiawase",
    "/otoiawase",
    "/お問い合わせ",
    "/お問合せ",
    "/mail",
    "/form",
    "/ask",
    "/contact-us",
    "/enquiry",
    "/toiawase",
]

CONTACT_LINK_TEXTS = [
    "お問い合わせ",
    "お問合せ",
    "問い合わせ",
    "問合せ",
    "ご相談",
    "contact",
    "Contact",
    "CONTACT",
    "メールフォーム",
]

PRIORITY_CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/inquiry",
    "/toiawase",
    "/お問い合わせ",
    "/問い合わせ",
    "/reservation",
    "/予約",
    "/access",
    "/company",
    "/profile",
]

WORDPRESS_CONTACT_HINTS = [
    "wp",
    "contact-form",
    "contactform",
    "wpforms",
    "mw-wp-form",
]

INTERNAL_CONTACT_TEXT_TOKENS = [
    "お問い合わせ",
    "問合せ",
    "問い合わせ",
    "予約",
    "ご予約",
    "contact",
    "reserve",
    "form",
    "mail",
    "line",
]

HIGH_PRIORITY_DISCOVERY_TOKENS = [
    "お問い合わせ",
    "contact",
    "form",
    "予約",
    "reserve",
    "booking",
    "inquiry",
    "ご相談",
    "申し込み",
    "entry",
]

LOW_PRIORITY_DISCOVERY_TOKENS = [
    "privacy",
    "policy",
    "terms",
    "company",
    "profile",
]

EXTERNAL_FORM_DISCOVERY_HINTS = [
    "docs.google.com/forms",
    "form.run",
    "reserva",
    "select-type",
    "coubic",
    "tol-app",
    "stores.jp",
    "airreserve",
    "jotform",
    "typeform",
]

CONFIRM_BUTTON_TEXTS = [
    "確認",
    "確認画面へ",
    "内容確認",
    "内容を確認",
    "確認する",
    "Confirm",
    "Next",
    "次へ",
]

FINAL_SUBMIT_TEXTS = [
    "送信",
    "送信する",
    "送信内容を送信",
    "確定",
    "この内容で送信",
    "Submit",
    "Send",
]

CAPTCHA_TEXT_TOKENS = [
    "captcha",
    "recaptcha",
    "hcaptcha",
    "私はロボットではありません",
    "ロボットではありません",
]

SALES_PROHIBITED_TOKENS = [
    "営業お断り",
    "営業目的",
    "営業メール",
    "営業行為",
    "セールス禁止",
    "セールスお断り",
    "売り込み",
    "勧誘お断り",
    "sales prohibited",
    "no sales",
    "no solicitation",
]


def classify_submit_text(text: str) -> str:
    """Classify visible button text without clicking anything."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return ""
    if any(token.lower() in normalized for token in CONFIRM_BUTTON_TEXTS):
        return "confirm"
    if any(token.lower() in normalized for token in FINAL_SUBMIT_TEXTS):
        return "submit"
    return ""


def detect_sales_prohibited_text(text: str) -> bool:
    normalized = (text or "").lower()
    return any(token.lower() in normalized for token in SALES_PROHIBITED_TOKENS)


class _StaticFormHTMLParser(HTMLParser):
    """Small local-only HTML analyzer used by unit tests and dry diagnostics."""

    def __init__(self) -> None:
        super().__init__()
        self.controls: List[Dict[str, str]] = []
        self.labels_by_for: Dict[str, str] = {}
        self.buttons: List[Dict[str, str]] = []
        self.has_form = False
        self.has_captcha = False
        self._label_stack: List[Dict[str, object]] = []
        self._button_stack: List[Dict[str, object]] = []
        self._text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "form":
            self.has_form = True
        if tag == "label":
            self._label_stack.append({"for": attrs_dict.get("for", ""), "text": [], "controls": []})
        if tag == "button":
            self._button_stack.append({"text": [], "attrs": attrs_dict})
        if tag == "iframe":
            haystack = " ".join(attrs_dict.values()).lower()
            if any(token in haystack for token in ("captcha", "recaptcha", "hcaptcha")):
                self.has_captcha = True
        if tag in {"input", "textarea", "select"} or self._is_custom_textbox(attrs_dict):
            control = self._control_meta(tag, attrs_dict)
            self.controls.append(control)
            if self._label_stack:
                self._label_stack[-1]["controls"].append(len(self.controls) - 1)
        if tag == "input" and attrs_dict.get("type", "").lower() in {"submit", "button"}:
            self.buttons.append({"text": attrs_dict.get("value", "")})
        text_haystack = " ".join(attrs_dict.values()).lower()
        if any(token.lower() in text_haystack for token in CAPTCHA_TEXT_TOKENS):
            self.has_captcha = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "label" and self._label_stack:
            label = self._label_stack.pop()
            text = " ".join(str(part).strip() for part in label["text"] if str(part).strip())
            if label["for"]:
                self.labels_by_for[str(label["for"])] = text
            for control_index in label["controls"]:
                self.controls[int(control_index)]["label"] = text
        if tag == "button" and self._button_stack:
            button = self._button_stack.pop()
            text = " ".join(str(part).strip() for part in button["text"] if str(part).strip())
            self.buttons.append({"text": text})

    def handle_data(self, data: str) -> None:
        self._text_parts.append(data)
        if self._label_stack:
            self._label_stack[-1]["text"].append(data)
        if self._button_stack:
            self._button_stack[-1]["text"].append(data)

    @staticmethod
    def _is_custom_textbox(attrs: Dict[str, str]) -> bool:
        return attrs.get("contenteditable", "").lower() == "true" or attrs.get("role", "").lower() == "textbox"

    @staticmethod
    def _control_meta(tag: str, attrs: Dict[str, str]) -> Dict[str, str]:
        return {
            "tag": tag,
            "type": attrs.get("type", ""),
            "name": attrs.get("name", ""),
            "id": attrs.get("id", ""),
            "placeholder": attrs.get("placeholder", ""),
            "aria_label": attrs.get("aria-label", ""),
            "role": attrs.get("role", ""),
            "contenteditable": attrs.get("contenteditable", ""),
            "label": "",
        }

    def text(self) -> str:
        return " ".join(part.strip() for part in self._text_parts if part.strip())


def analyze_static_form_html(html: str) -> Dict[str, object]:
    """Analyze local mock HTML without Playwright or network access."""
    parser = _StaticFormHTMLParser()
    parser.feed(html or "")
    for control in parser.controls:
        control_id = control.get("id", "")
        if control_id and not control.get("label"):
            control["label"] = parser.labels_by_for.get(control_id, "")

    field_counts: Dict[str, int] = {}
    for control in parser.controls:
        field_type = FormDetector._classify_control(control)
        if field_type and field_type != "unknown":
            field_counts[field_type] = field_counts.get(field_type, 0) + 1

    confirm_count = 0
    submit_count = 0
    for button in parser.buttons:
        kind = classify_submit_text(button.get("text", ""))
        if kind == "confirm":
            confirm_count += 1
        elif kind == "submit":
            submit_count += 1

    page_text = parser.text()
    has_captcha = parser.has_captcha or any(
        token.lower() in page_text.lower() for token in CAPTCHA_TEXT_TOKENS
    )
    return {
        "has_form": parser.has_form,
        "fields": field_counts,
        "field_types": sorted(field_counts),
        "confirm_button_count": confirm_count,
        "final_submit_button_count": submit_count,
        "has_captcha": has_captcha,
        "sales_prohibited": detect_sales_prohibited_text(page_text),
    }


# Public placeholder fallback policy. Real sender details belong only in the
# ignored local config/sender_info.json file.
DISPLAY_NAME = "担当者"
SURNAME = "担当"
GIVEN_NAME = "者"
COMPANY_NAME = "KIMOTO STUDIO"
FURIGANA_SEI = "タントウ"
FURIGANA_MEI = "シャ"

REQUIRED_MARKERS = ("必須", "*", "＊")
REQUIRED_TEXT_MARKERS = ("必須", "*", "＊", "required", "Required")

ADDRESS_KEYWORDS = [
    "郵便番号",
    "〒",
    "zip",
    "postal",
    "都道府県",
    "県",
    "府",
    "都",
    "pref",
    "prefecture",
    "state",
    "市区町村",
    "住所",
    "番地",
    "建物",
    "マンション",
    "号室",
    "address",
    "city",
    "street",
]

JST = ZoneInfo("Asia/Tokyo")

FIELD_PATTERNS = {
    "name": {
        "labels": ["お名前", "氏名", "ご氏名", "名前", "担当者名", "Name", "name", "Full Name"],
        "attributes": ["name", "your-name", "customer-name", "fullname", "full-name", "your_name", "onamae"],
        "placeholders": ["お名前", "氏名", "名前", "Name", "Full Name", "フルネーム"],
    },
    "name_sei": {
        "labels": ["姓", "氏", "苗字", "Last Name", "last name", "Family Name"],
        "attributes": ["sei", "last-name", "lastname", "family-name", "name_sei", "surname"],
        "placeholders": ["姓", "氏", "苗字"],
    },
    "name_mei": {
        "labels": ["名", "First Name", "first name", "Given Name"],
        "attributes": ["mei", "first-name", "firstname", "given-name", "name_mei"],
        "placeholders": ["名"],
    },
    "furigana": {
        "labels": ["フリガナ", "ふりがな", "カナ", "お名前(カナ)", "氏名(カナ)"],
        "attributes": ["furigana", "kana", "name-kana", "name_kana"],
        "placeholders": ["フリガナ", "ふりがな", "カナ"],
    },
    "furigana_sei": {
        "labels": ["セイ", "姓フリガナ", "氏フリガナ", "姓(カナ)", "Last Name Kana"],
        "attributes": ["sei-kana", "last-kana", "surname-kana", "kana_sei", "furigana_sei"],
        "placeholders": ["セイ", "姓フリガナ"],
    },
    "furigana_mei": {
        "labels": ["メイ", "名フリガナ", "名(カナ)", "First Name Kana"],
        "attributes": ["mei-kana", "first-kana", "given-kana", "kana_mei", "furigana_mei"],
        "placeholders": ["メイ", "名フリガナ"],
    },
    "email": {
        "labels": ["メール", "メールアドレス", "メールアドレス（確認）", "E-mail", "e-mail", "Email", "email", "Mail Address"],
        "attributes": ["email", "your-email", "mail", "e-mail", "your_email", "mailaddress", "mail-address"],
        "placeholders": ["メールアドレス", "example@example.com", "Email", "email", "mail@example.com"],
    },
    "phone": {
        "labels": ["電話", "TEL", "tel", "携帯", "連絡先", "電話番号", "Phone", "Phone Number"],
        "attributes": ["tel", "phone", "telephone", "your-tel", "your_phone", "mobile", "phone-number"],
        "placeholders": ["電話番号", "090-1234-5678", "TEL", "携帯", "Phone", "Phone Number"],
    },
    "subject": {
        "labels": ["件名", "タイトル", "subject", "Subject", "お問い合わせ種別", "Inquiry Type", "Category"],
        "attributes": ["subject", "your-subject", "title", "category", "inquiry-type"],
        "placeholders": ["件名", "Subject", "Category"],
    },
    "message": {
        "labels": [
            "お問い合わせ内容",
            "お問い合わせ",
            "問い合わせ内容",
            "ご相談内容",
            "相談内容",
            "ご質問",
            "内容",
            "メッセージ",
            "本文",
            "備考",
            "message",
            "Message",
            "Comments",
        ],
        "attributes": ["message", "your-message", "content", "body", "inquiry", "your_message", "comment", "comments", "description", "question"],
        "placeholders": ["お問い合わせ内容", "問い合わせ内容", "ご相談内容", "メッセージ", "本文", "Message", "Comments", "Question"],
    },
    "company": {
        "labels": ["会社名", "屋号", "企業名", "organization", "company", "法人名"],
        "attributes": ["company", "organization", "your-company", "company-name", "corp"],
        "placeholders": ["会社名", "屋号", "Company"],
    },
}


class FormDetector:
    """Detect and fill contact forms on Japanese websites."""

    def __init__(self, page: Page, sender_info: dict, timeout: int = 30):
        self.page = page
        self.sender_info = sender_info
        self.timeout = int(timeout) * 1000
        self.last_contact_evidence = ""
        self.last_pages_visited = 0
        self.last_candidate_contact_links_found = 0
        self.last_candidate_urls: List[str] = []

    @staticmethod
    def _escape_css_text(text: str) -> str:
        return str(text).replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _combined_meta_text(meta: dict) -> str:
        parts = [
            str(meta.get("label", "")),
            str(meta.get("name", "")),
            str(meta.get("id", "")),
            str(meta.get("placeholder", "")),
            str(meta.get("aria_label", "")),
            str(meta.get("type", "")),
            str(meta.get("tag", "")),
        ]
        return " ".join(parts)

    @classmethod
    def _classify_control(cls, meta: dict) -> str:
        full_text = cls._combined_meta_text(meta)
        text_lower = full_text.lower()

        if any(token in full_text for token in ["メール", "メールアドレス", "e-mail"]) or "email" in text_lower:
            return "email"
        if any(token in full_text for token in ["電話", "tel", "携帯", "連絡先"]) or "phone" in text_lower:
            return "phone"

        if any(token in full_text for token in ADDRESS_KEYWORDS) or any(
            token in text_lower for token in ["zip", "postal", "prefecture", "address", "city", "street"]
        ):
            return "address"

        if any(token in full_text for token in ["同意", "プライバシー", "個人情報", "利用規約"]) or (
            str(meta.get("type", "")).lower() == "checkbox"
        ):
            return "consent_checkbox"

        if any(token in full_text for token in ["会社", "屋号", "法人"]) or any(
            token in text_lower for token in ["company", "organization", "corporation"]
        ):
            return "company"
        if any(token in full_text for token in ["件名", "お問い合わせ種別"]) or any(
            token in text_lower for token in ["subject", "inquiry type", "category", "title"]
        ):
            return "subject"
        if any(token in full_text for token in ["内容", "本文", "メッセージ", "問い合わせ内容", "お問い合わせ内容"]) or any(
            token in text_lower for token in ["message", "comments", "comment", "question", "inquiry"]
        ):
            return "message"

        kana_hit = any(token in full_text for token in ["フリガナ", "ふりがな", "カナ", "セイ", "メイ"])
        if kana_hit and any(token in full_text for token in ["姓", "氏", "セイ", "last", "family", "surname"]):
            return "furigana_sei"
        if kana_hit and any(token in full_text for token in ["名", "メイ", "first", "given"]):
            return "furigana_mei"
        if kana_hit:
            return "furigana"

        if any(token in full_text for token in ["氏名", "お名前", "名前"]) or "full name" in text_lower:
            return "name"
        if any(token in full_text for token in ["姓", "氏", "苗字"]) or any(
            token in text_lower for token in ["last", "family", "surname"]
        ):
            return "name_sei"
        if (any(token in full_text for token in ["名"]) or any(token in text_lower for token in ["first", "given"])) and not kana_hit:
            return "name_mei"
        if "name" in text_lower:
            return "name"

        if str(meta.get("type", "")).lower() == "date" or any(token in full_text for token in ["日付", "date", "希望日"]):
            return "date"

        return "unknown"

    @classmethod
    def _describe_control(cls, meta: dict, classification: str) -> str:
        label = str(meta.get("label", "")).strip()
        name = str(meta.get("name", "")).strip()
        cid = str(meta.get("id", "")).strip()
        placeholder = str(meta.get("placeholder", "")).strip()
        token = label or name or cid or placeholder or str(meta.get("tag", "")).strip() or "field"
        return f"{classification}:{token[:80]}"

    async def inspect_required_fields(self) -> dict:
        controls = await self.page.evaluate(
            """
            () => {
              const collectLabel = (el) => {
                const id = el.getAttribute('id');
                if (id) {
                  const byFor = document.querySelector(`label[for="${id.replace(/"/g, '\\"')}"]`);
                  if (byFor && byFor.innerText) return byFor.innerText.trim();
                }
                const wrapped = el.closest('label');
                if (wrapped && wrapped.innerText) return wrapped.innerText.trim();
                const near = el.closest('td,th,div,p,li,dt,dd,tr');
                return near && near.innerText ? near.innerText.trim().slice(0, 120) : '';
              };
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const isRequired = (el, labelText) => {
                if (el.hasAttribute('required')) return true;
                if ((el.getAttribute('aria-required') || '').toLowerCase() === 'true') return true;
                const hit = (labelText || '');
                return /必須|\\*|＊|Required|required/.test(hit);
              };
              const nodes = Array.from(
                document.querySelectorAll('input, textarea, select, [contenteditable="true"], [role="textbox"]')
              ).filter(isVisible);
              return nodes.map((el, idx) => {
                const tag = (el.tagName || '').toLowerCase();
                const type = ((el.getAttribute('type') || '') + '').toLowerCase();
                const labelText = collectLabel(el);
                const required = isRequired(el, labelText);
                return {
                  idx,
                  tag,
                  type,
                  id: el.getAttribute('id') || '',
                  name: el.getAttribute('name') || '',
                  placeholder: el.getAttribute('placeholder') || '',
                  aria_label: el.getAttribute('aria-label') || '',
                  label: labelText || '',
                  required,
                };
              });
            }
            """
        )

        detected_required_fields = []
        address_required_fields = []
        for meta in controls:
            classification = self._classify_control(meta)
            if meta.get("required"):
                desc = self._describe_control(meta, classification)
                detected_required_fields.append(desc)
                if classification == "address":
                    address_required_fields.append(desc)

        return {
            "detected_required_fields": detected_required_fields,
            "address_required_fields": address_required_fields,
            "required_count": len(detected_required_fields),
        }

    async def _is_fillable(self, locator: Locator) -> bool:
        try:
            if await locator.count() == 0:
                return False
            target = locator.first
            if not await target.is_visible():
                return False
            if await target.is_disabled():
                return False
            tag = (await target.evaluate("el => el.tagName.toLowerCase()"))
            is_editable = (await target.get_attribute("contenteditable") or "").lower() == "true"
            role = (await target.get_attribute("role") or "").lower()
            if tag not in {"input", "textarea", "select"} and not is_editable and role != "textbox":
                return False
            if tag == "input":
                input_type = ((await target.get_attribute("type")) or "text").lower()
                if input_type in {"hidden", "submit", "button", "image", "reset", "file", "radio", "checkbox"}:
                    return False
            return True
        except Exception:
            return False

    async def _has_contact_form(self) -> bool:
        try:
            if await self.page.locator("form").count() > 0:
                return True
            if await self.page.locator("textarea:visible, [contenteditable='true']:visible, [role='textbox']:visible").count() > 0:
                return True
            if await self.page.locator("input[type='email']:visible").count() > 0:
                return True
            if await self.page.locator("input[type='text']:visible, input[type='tel']:visible").count() >= 2:
                return True
            return False
        except Exception:
            return False

    async def _has_contact_form_in_first_viewport(self) -> bool:
        """True if form/input/textarea exists and is visible in initial viewport."""
        try:
            return bool(
                await self.page.evaluate(
                    """
                    () => {
                      const vh = window.innerHeight || 0;
                      const vw = window.innerWidth || 0;
                      const nodes = Array.from(
                        document.querySelectorAll('form, input, textarea, [contenteditable="true"], [role="textbox"]')
                      );
                      for (const el of nodes) {
                        const tag = (el.tagName || '').toLowerCase();
                        if (tag === 'input') {
                          const t = ((el.getAttribute('type') || 'text') + '').toLowerCase();
                          if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(t)) {
                            continue;
                          }
                        }
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const inViewport = rect.top < vh && rect.bottom > 0 && rect.left < vw && rect.right > 0;
                        if (!inViewport) continue;
                        return true;
                      }
                      return false;
                    }
                    """
                )
            )
        except Exception:
            return False

    async def dismiss_cookie_banners(self) -> List[str]:
        """Best-effort cookie/CMP consent click.

        Limits clicks to cookie/consent related containers/selectors so normal
        form submit actions are never targeted.
        """
        script = """
        () => {
          const clicked = [];
          const ACCEPT_TOKENS = [
            'accept', 'agree', 'consent', 'allow', 'ok', 'got it',
            '同意', '同意する', '承諾', '許可', '許可する', '了解', '承知',
            'すべて許可', '全て許可', '同意して続行', '同意して閉じる'
          ];
          const ROOT_SELECTORS = [
            '#onetrust-banner-sdk',
            '.fc-consent-root',
            '.ot-sdk-container',
            '.cookie-banner',
            '.cookie-consent',
            '.cmp-wrapper',
            '.qc-cmp2-container',
            '.didomi-popup-container',
            '[id*="cookie"]',
            '[class*="cookie"]',
            '[id*="consent"]',
            '[class*="consent"]',
            '[id*="gdpr"]',
            '[class*="gdpr"]',
            '[id*="cmp"]',
            '[class*="cmp"]',
            '[aria-label*="cookie" i]',
            '[role="dialog"]',
            '[role="alertdialog"]'
          ];
          const DIRECT_ACCEPT_SELECTORS = [
            '#onetrust-accept-btn-handler',
            '.ot-sdk-accept-all',
            '.fc-cta-consent',
            '[data-testid*="accept" i]',
            '[id*="accept" i]',
            '[class*="accept" i]'
          ];

          const norm = (s) => ((s || '') + '').toLowerCase().replace(/\\s+/g, '');
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          const looksLikeAccept = (el) => {
            const text = norm(
              el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || ''
            );
            if (!text) return false;
            return ACCEPT_TOKENS.some(t => text.includes(norm(t)));
          };
          const safeClick = (el, label) => {
            try {
              if (!isVisible(el)) return false;
              el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
              if (typeof el.click === 'function') el.click();
              clicked.push(label);
              return true;
            } catch (_e) {
              return false;
            }
          };

          // 1) Vendor/direct accept buttons
          for (const sel of DIRECT_ACCEPT_SELECTORS) {
            const nodes = Array.from(document.querySelectorAll(sel));
            for (const el of nodes.slice(0, 2)) {
              if (looksLikeAccept(el) || sel.includes('onetrust') || sel.includes('fc-cta-consent')) {
                safeClick(el, `direct:${sel}`);
              }
            }
          }

          // 2) Buttons only inside cookie/consent-ish roots
          const roots = [];
          for (const sel of ROOT_SELECTORS) {
            for (const el of Array.from(document.querySelectorAll(sel))) {
              roots.push({ el, sel });
            }
          }

          const seen = new Set();
          for (const item of roots) {
            const root = item.el;
            if (!root || seen.has(root)) continue;
            seen.add(root);

            const rootText = norm(root.innerText || root.textContent || '');
            const cookieish = rootText.includes('cookie') || rootText.includes('同意') || rootText.includes('プライバシー') || rootText.includes('consent');
            if (!cookieish && !/cookie|consent|gdpr|cmp/i.test(item.sel || '')) continue;

            const btns = Array.from(root.querySelectorAll('button, a, input[type="button"], input[type="submit"]'));
            for (const btn of btns.slice(0, 12)) {
              if (looksLikeAccept(btn)) {
                safeClick(btn, `root:${item.sel}`);
              }
            }
          }

          return clicked;
        }
        """

        clicked_labels: List[str] = []
        # page.frames includes main frame; dedupe by object id
        frames = []
        for fr in [self.page.main_frame, *self.page.frames]:
            if any(id(fr) == id(x) for x in frames):
                continue
            frames.append(fr)

        for frame in frames:
            try:
                labels = await frame.evaluate(script)
                if isinstance(labels, list):
                    clicked_labels.extend([str(x) for x in labels if str(x).strip()])
            except Exception:
                continue

        if clicked_labels:
            logger.info("[FORM] cookie consent clicked: %s", "; ".join(clicked_labels[:6]))
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=2500)
            except Exception:
                pass
        return clicked_labels

    async def find_contact_page(
        self,
        base_url: str,
        max_internal_links: int = 80,
        max_pages_to_try: int = 8,
        contact_link_text_keywords: Optional[Sequence[str]] = None,
        allow_querystring_urls: bool = True,
    ) -> Optional[str]:
        logger.info("[FORM] Searching contact page: %s", base_url)
        candidates: dict[str, tuple[int, str]] = {}
        self.last_contact_evidence = ""
        self.last_pages_visited = 0
        self.last_candidate_contact_links_found = 0
        self.last_candidate_urls = []
        keyword_list = [
            str(k).strip().lower()
            for k in (contact_link_text_keywords or INTERNAL_CONTACT_TEXT_TOKENS)
            if str(k).strip()
        ]

        def add_candidate(url: str, priority: int, source: str) -> None:
            parsed = urlparse(url)
            if not parsed.scheme or parsed.scheme not in {"http", "https"}:
                return
            if not allow_querystring_urls and parsed.query:
                return
            normalized = parsed._replace(fragment="").geturl()
            current = candidates.get(normalized)
            if current is None or priority < current[0]:
                candidates[normalized] = (priority, source)

        # Strategy 0: current URL candidate first.
        add_candidate(base_url, 5, "base_url")

        # Strategy 1: explicit path priority + sitemap.
        for i, path in enumerate(PRIORITY_CONTACT_PATHS):
            target = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
            add_candidate(target, 10 + i, f"priority_path:{path}")
        add_candidate(urljoin(base_url.rstrip("/") + "/", "sitemap"), 15, "sitemap")
        add_candidate(urljoin(base_url.rstrip("/") + "/", "sitemap.xml"), 16, "sitemap_xml")

        # Strategy 2: remaining common path probing.
        for i, path in enumerate(CONTACT_PATH_PATTERNS):
            if path in PRIORITY_CONTACT_PATHS:
                continue
            target = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
            add_candidate(target, 40 + i, f"path:{path}")

        # Strategy 3: collect href candidates from homepage and prioritize by link text / wordpress hints.
        try:
            await self.page.goto(base_url, timeout=self.timeout, wait_until="domcontentloaded")
            await self.dismiss_cookie_banners()
            await asyncio.sleep(0.3)
            link_entries = await self.page.evaluate(
                """
                () => Array.from(document.querySelectorAll('a[href]')).slice(0, 1200).map(a => {
                  const href = a.getAttribute('href') || '';
                  const text = ((a.innerText || a.textContent || '') + '').trim();
                  const inHeader = !!a.closest('header');
                  const inFooter = !!a.closest('footer');
                  const inNav = !!a.closest('nav');
                  return { href, text, inHeader, inFooter, inNav };
                })
                """
            )

            base_host = urlparse(self.page.url).netloc.lower()
            internal_probe_added = 0

            for entry in link_entries:
                href = str(entry.get("href", "") or "")
                if not href:
                    continue
                text = str(entry.get("text", "") or "").strip()
                text_low = text.lower()
                href_low = href.lower()
                candidate = urljoin(self.page.url, href)
                candidate_low = candidate.lower()
                candidate_host = urlparse(candidate).netloc.lower()
                is_internal = bool(candidate_host) and candidate_host == base_host
                in_header = bool(entry.get("inHeader"))
                in_footer = bool(entry.get("inFooter"))
                in_nav = bool(entry.get("inNav"))
                zone = "header" if in_header else "footer" if in_footer else "nav" if in_nav else "body"
                zone_bonus = -3 if (in_header or in_footer or in_nav) else 0

                if any(hint in candidate_low for hint in EXTERNAL_FORM_DISCOVERY_HINTS):
                    add_candidate(candidate, 11, f"external_form_link:zone={zone}")

                # Aggressive recall: probe many internal links with keyword priority.
                if is_internal and internal_probe_added < max(10, int(max_internal_links)):
                    configured_hit = next((tok for tok in keyword_list if tok and (tok in text_low or tok in href_low)), "")
                    high_hit = next((tok for tok in HIGH_PRIORITY_DISCOVERY_TOKENS if tok in text_low or tok in href_low), "")
                    low_hit = next((tok for tok in LOW_PRIORITY_DISCOVERY_TOKENS if tok in text_low or tok in href_low), "")
                    if configured_hit or high_hit:
                        hit = configured_hit or high_hit
                        add_candidate(
                            candidate,
                            max(12, 20 + zone_bonus + internal_probe_added // 6),
                            f"anchor_text={text[:50] or href[:50]}|token={hit}|zone={zone}",
                        )
                        internal_probe_added += 1
                    elif low_hit:
                        add_candidate(
                            candidate,
                            max(35, 52 + zone_bonus + internal_probe_added // 6),
                            f"anchor_text={text[:50] or href[:50]}|token={low_hit}|zone={zone}",
                        )
                        internal_probe_added += 1

                # Backward-compatible broad token probing.
                if is_internal and internal_probe_added < max(10, int(max_internal_links)) and any(
                    tok in text_low for tok in INTERNAL_CONTACT_TEXT_TOKENS
                ):
                    add_candidate(candidate, 22 + zone_bonus + internal_probe_added // 10, "internal_text_probe")
                    internal_probe_added += 1

                # Highest priority: visible Japanese "お問い合わせ" style links.
                if "お問い合わせ" in text or "お問合せ" in text or "問い合わせ" in text or "問合せ" in text:
                    add_candidate(candidate, 12 + zone_bonus, f"anchor_text={text[:50] or href[:50]}|found=お問い合わせ")
                    continue

                # WordPress contact form pages.
                if any(hint in candidate_low or hint in href_low for hint in WORDPRESS_CONTACT_HINTS) and any(
                    token in candidate_low or token in href_low for token in ["contact", "inquiry", "toiawase", "otoiawase"]
                ):
                    add_candidate(candidate, 18 + zone_bonus, "wordpress_contact_hint")
                    continue

                # General contact/reserve tokens in href/text.
                if any(
                    token in candidate_low or token in href_low
                    for token in ["contact", "inquiry", "toiawase", "otoiawase", "mail", "form", "reserve", "reservation"]
                ):
                    add_candidate(candidate, 55, "href_contact_token")
                    continue
                if any(tok in text_low for tok in INTERNAL_CONTACT_TEXT_TOKENS):
                    add_candidate(candidate, 58, "text_contact_token")
                    continue
        except Exception as e:
            logger.warning("[FORM] Home load failed %s: %s", base_url, e)

        # Final candidate evaluation in priority order.
        ordered = sorted(candidates.items(), key=lambda x: x[1][0])
        self.last_candidate_contact_links_found = len(ordered)
        self.last_candidate_urls = [url for url, _meta in ordered]
        logger.info(
            "[FORM] candidate links found=%s max_pages_to_try=%s allow_querystring_urls=%s",
            self.last_candidate_contact_links_found,
            max_pages_to_try,
            allow_querystring_urls,
        )
        fallback_url = None
        fallback_source = ""
        base_normalized = base_url.rstrip("/")
        max_try = max(1, int(max_pages_to_try))
        pages_visited = 0
        for candidate, (priority, source) in ordered:
            if pages_visited >= max_try:
                break
            try:
                response = await self.page.goto(candidate, timeout=self.timeout, wait_until="domcontentloaded")
                pages_visited += 1
                self.last_pages_visited = pages_visited
                await self.dismiss_cookie_banners()
                if not response or response.status >= 400:
                    continue
                response_url = self.page.url
                response_is_base = response_url.rstrip("/") == base_normalized
                if fallback_url is None or (fallback_source == "base_url" and not response_is_base):
                    fallback_url = self.page.url
                    fallback_source = source
                if await self._has_contact_form_in_first_viewport():
                    self.last_contact_evidence = source
                    logger.info(
                        "[FORM] Contact page selected: %s (priority=%s, source=%s)",
                        self.page.url,
                        priority,
                        source,
                    )
                    return self.page.url
                if await self._has_contact_form():
                    self.last_contact_evidence = source
                    logger.info(
                        "[FORM] Contact page selected (relaxed): %s (priority=%s, source=%s)",
                        self.page.url,
                        priority,
                        source,
                    )
                    return self.page.url
                logger.info(
                    "[FORM] Candidate rejected (no form-like controls): %s (source=%s)",
                    candidate,
                    source,
                )
            except Exception:
                continue

        self.last_pages_visited = pages_visited
        if fallback_url:
            self.last_contact_evidence = f"fallback_candidate:{fallback_source}"
            logger.info("[FORM] Fallback contact candidate selected: %s (%s)", fallback_url, fallback_source)
            return fallback_url

        logger.warning("[FORM] No contact page found: %s", base_url)
        return None

    async def detect_form_fields(self) -> Tuple[Dict[str, Locator], dict]:
        detected: Dict[str, Locator] = {}
        form_map: Dict[str, str] = {}
        stats = {"total_checked": len(FIELD_PATTERNS), "found": 0}
        await self.dismiss_cookie_banners()

        for field_type, patterns in FIELD_PATTERNS.items():
            locator, selector_used = await self._find_field(field_type, patterns)
            if locator and await self._is_fillable(locator):
                detected[field_type] = locator
                form_map[field_type] = selector_used
                stats["found"] += 1
                logger.info("[FORM] detected field %s via %s", field_type, selector_used)

        form_map["_stats"] = stats
        logger.info("[FORM] detection complete: %s/%s", stats["found"], stats["total_checked"])
        return detected, form_map

    async def detect_form_presence(self) -> Tuple[bool, str, dict]:
        """Relaxed form presence detection for DETECT_ONLY mode.

        Rules:
        - has <form> OR textarea OR >=2 inputs OR submit-like button text
        - iframe src contains known form provider hints
        - if password input exists => no form (login_form)
        """
        meta = {
            "form_count": 0,
            "textarea_count": 0,
            "contenteditable_count": 0,
            "role_textbox_count": 0,
            "input_count": 0,
            "submit_like_button_count": 0,
            "password_count": 0,
            "iframe_form_count": 0,
            "sales_prohibited": False,
            "evidence": "",
        }
        try:
            counts = await self.page.evaluate(
                """
                () => {
                  const passwordCount = document.querySelectorAll('input[type="password"]').length;
                  const formCount = document.querySelectorAll('form').length;
                  const textareaCount = document.querySelectorAll('textarea').length;
                  const contenteditableCount = document.querySelectorAll('[contenteditable="true"]').length;
                  const roleTextboxCount = document.querySelectorAll('[role="textbox"]').length;
                  const inputCount = Array.from(document.querySelectorAll('input'))
                    .filter(el => ((el.getAttribute('type') || 'text') + '').toLowerCase() !== 'hidden')
                    .length;

                  const tokens = ['送信', '確認', 'submit', 'send', 'confirm', 'next', '次へ'];
                  let submitLikeButtonCount = 0;
                  for (const el of document.querySelectorAll('button, input[type="submit"], input[type="button"]')) {
                    const text = ((el.innerText || el.value || '') + '').toLowerCase();
                    if (tokens.some(t => text.includes(t.toLowerCase()))) {
                      submitLikeButtonCount += 1;
                    }
                  }

                  const iframeTokens = ['form', 'forms', 'google', 'reserva', 'select', 'jotform', 'typeform'];
                  let iframeFormCount = 0;
                  for (const f of document.querySelectorAll('iframe[src]')) {
                    const src = ((f.getAttribute('src') || '') + '').toLowerCase();
                    if (iframeTokens.some(t => src.includes(t))) {
                      iframeFormCount += 1;
                    }
                  }

                  const bodyText = ((document.body && document.body.innerText) || '').toLowerCase();
                  const salesTokens = [
                    '営業お断り',
                    '営業目的',
                    '営業メール',
                    '営業行為',
                    'セールス禁止',
                    'セールスお断り',
                    '売り込み',
                    '勧誘お断り',
                    'sales prohibited',
                    'no sales',
                    'no solicitation'
                  ];
                  const salesProhibited = salesTokens.some(t => bodyText.includes(t.toLowerCase()));

                  return {
                    passwordCount,
                    formCount,
                    textareaCount,
                    contenteditableCount,
                    roleTextboxCount,
                    inputCount,
                    submitLikeButtonCount,
                    iframeFormCount,
                    salesProhibited,
                  };
                }
                """
            )
            meta["password_count"] = int(counts.get("passwordCount", 0))
            meta["form_count"] = int(counts.get("formCount", 0))
            meta["textarea_count"] = int(counts.get("textareaCount", 0))
            meta["contenteditable_count"] = int(counts.get("contenteditableCount", 0))
            meta["role_textbox_count"] = int(counts.get("roleTextboxCount", 0))
            meta["input_count"] = int(counts.get("inputCount", 0))
            meta["submit_like_button_count"] = int(counts.get("submitLikeButtonCount", 0))
            meta["iframe_form_count"] = int(counts.get("iframeFormCount", 0))
            meta["sales_prohibited"] = bool(counts.get("salesProhibited", False))

            evidence = []
            if meta["form_count"] > 0:
                evidence.append("found=form tag")
            if meta["textarea_count"] > 0:
                evidence.append("found=textarea")
            if meta["contenteditable_count"] > 0:
                evidence.append(f"found=contenteditable:{meta['contenteditable_count']}")
            if meta["role_textbox_count"] > 0:
                evidence.append(f"found=role_textbox:{meta['role_textbox_count']}")
            if meta["input_count"] >= 2:
                evidence.append(f"found=inputs:{meta['input_count']}")
            if meta["submit_like_button_count"] > 0:
                evidence.append(f"found=submit_like_button:{meta['submit_like_button_count']}")
            if meta["iframe_form_count"] > 0:
                evidence.append(f"found=iframe_form_hint:{meta['iframe_form_count']}")

            if meta["password_count"] > 0:
                meta["evidence"] = "found=password_input"
                return False, "login_form", meta
            if meta["sales_prohibited"]:
                meta["evidence"] = "found=sales_prohibited"
                return False, "sales_prohibited", meta

            has_form_like = (
                meta["form_count"] > 0
                or meta["textarea_count"] > 0
                or meta["contenteditable_count"] > 0
                or meta["role_textbox_count"] > 0
                or meta["input_count"] >= 2
                or meta["submit_like_button_count"] > 0
                or meta["iframe_form_count"] > 0
            )
            if has_form_like:
                meta["evidence"] = "; ".join(evidence) if evidence else "found=form_like"
                return True, "form_detected", meta
            meta["evidence"] = "found=none"
            return False, "no_form_found", meta
        except Exception as e:
            logger.warning("[FORM] detect_form_presence error: %s", e)
            return False, "no_form_found", meta

    async def _find_field(self, field_type: str, patterns: dict) -> Tuple[Optional[Locator], str]:
        # Strategy 1: by name/id attributes.
        for attr_val in patterns.get("attributes", []):
            escaped = self._escape_css_text(attr_val)
            for selector in [
                f"input[name='{escaped}']",
                f"input[id='{escaped}']",
                f"input[name*='{escaped}']",
                f"input[id*='{escaped}']",
                f"textarea[name='{escaped}']",
                f"textarea[id='{escaped}']",
                f"textarea[name*='{escaped}']",
                f"textarea[id*='{escaped}']",
                f"select[name='{escaped}']",
                f"select[id='{escaped}']",
                f"select[name*='{escaped}']",
            ]:
                try:
                    loc = self.page.locator(selector)
                    if await self._is_fillable(loc):
                        return loc.first, f"attr:{selector}"
                except Exception:
                    continue

        # Strategy 2: placeholder.
        for text in patterns.get("placeholders", []):
            if len(str(text).strip()) <= 1:
                continue
            escaped = self._escape_css_text(text)
            for tag in ["input", "textarea"]:
                selector = f"{tag}[placeholder*='{escaped}']"
                try:
                    loc = self.page.locator(selector)
                    if await self._is_fillable(loc):
                        return loc.first, f"placeholder:{selector}"
                except Exception:
                    continue

        # Strategy 3: aria-label.
        for text in patterns.get("labels", []):
            escaped = self._escape_css_text(text)
            selector = (
                f"input[aria-label*='{escaped}'], textarea[aria-label*='{escaped}'], "
                f"select[aria-label*='{escaped}'], [contenteditable='true'][aria-label*='{escaped}'], "
                f"[role='textbox'][aria-label*='{escaped}']"
            )
            try:
                loc = self.page.locator(selector)
                if await self._is_fillable(loc):
                    return loc.first, f"aria-label:{escaped}"
            except Exception:
                continue

        # Strategy 4: label-for and label-inner mapping.
        for text in patterns.get("labels", []):
            escaped = self._escape_css_text(text)
            try:
                labels = self.page.locator(f"label:has-text('{escaped}')")
                count = await labels.count()
                for i in range(min(count, 8)):
                    label = labels.nth(i)
                    label_text = (await label.inner_text()).strip()
                    token = str(text).strip()
                    if len(token) == 1 and not label_text.startswith(token):
                        continue
                    if field_type in {"name_sei", "name_mei"} and any(
                        mark in label_text for mark in ["フリガナ", "ふりがな", "カナ", "セイ", "メイ"]
                    ):
                        continue
                    if field_type.startswith("furigana") and not any(
                        mark in label_text for mark in ["フリガナ", "ふりがな", "カナ", "セイ", "メイ"]
                    ):
                        continue
                    target_id = await label.get_attribute("for")
                    if target_id:
                        loc = self.page.locator(f"#{self._escape_css_text(target_id)}")
                        if await self._is_fillable(loc):
                            return loc.first, f"label-for:{escaped}->{target_id}"
                    inner = label.locator(
                        "input:visible, textarea:visible, select:visible, "
                        "[contenteditable='true']:visible, [role='textbox']:visible"
                    )
                    if await self._is_fillable(inner):
                        return inner.first, f"label-inner:{escaped}"
            except Exception:
                continue

        # Strategy 5: nearby text in common containers.
        for text in patterns.get("labels", []):
            escaped = self._escape_css_text(text)
            try:
                containers = self.page.locator(
                    f"div:has-text('{escaped}'), td:has-text('{escaped}'), th:has-text('{escaped}'), p:has-text('{escaped}')"
                )
                count = await containers.count()
                for i in range(min(count, 5)):
                    container = containers.nth(i)
                    loc = container.locator(
                        "input:visible, textarea:visible, select:visible, "
                        "[contenteditable='true']:visible, [role='textbox']:visible"
                    )
                    if await self._is_fillable(loc):
                        return loc.first, f"nearby-text:{escaped}"
            except Exception:
                continue

        # Strategy 6: field type fallback.
        fallback_map = {
            "message": "textarea:visible, [contenteditable='true']:visible, [role='textbox']:visible",
            "email": "input[type='email']:visible",
            "phone": "input[type='tel']:visible",
        }
        fallback_selector = fallback_map.get(field_type)
        if fallback_selector:
            try:
                loc = self.page.locator(fallback_selector)
                if await self._is_fillable(loc):
                    return loc.first, f"fallback:{fallback_selector}"
            except Exception:
                pass

        return None, ""

    async def _is_required(self, locator: Locator) -> bool:
        try:
            target = locator.first
            required_attr = await target.get_attribute("required")
            aria_required = (await target.get_attribute("aria-required") or "").lower()
            if required_attr is not None or aria_required == "true":
                return True

            field_id = await target.get_attribute("id")
            if field_id:
                labels = self.page.locator("label")
                count = await labels.count()
                for i in range(min(count, 100)):
                    label = labels.nth(i)
                    html_for = (await label.get_attribute("for") or "").strip()
                    if html_for != field_id:
                        continue
                    label_text = (await label.inner_text()).strip()
                    if any(marker in label_text for marker in REQUIRED_TEXT_MARKERS):
                        return True

            wrapper_text = (
                await target.evaluate(
                    """
                    (el) => {
                      const candidates = [];
                      const parent = el.closest('label,td,th,div,p,li,dt,dd,tr');
                      if (parent) candidates.push(parent.innerText || '');
                      const prev = el.previousElementSibling;
                      if (prev) candidates.push(prev.innerText || prev.textContent || '');
                      return candidates.join(' ');
                    }
                    """
                )
            )
            return any(marker in str(wrapper_text) for marker in REQUIRED_TEXT_MARKERS)
        except Exception:
            return False

    async def fill_form(self, fields: Dict[str, Locator], message: str, subject: str) -> Tuple[bool, dict]:
        stats = {
            "total_fields": len(fields),
            "filled": 0,
            "failed": 0,
            "skipped_optional": 0,
            "field_details": {},
            "detected_required_fields": [],
            "filled_fields": [],
            "missing_required_fields": [],
        }

        required_flags: Dict[str, bool] = {}
        for field_type, locator in fields.items():
            is_req = await self._is_required(locator)
            required_flags[field_type] = is_req
            if is_req:
                stats["detected_required_fields"].append(field_type)

        high_conf_fields = {"email", "phone", "message", "company"}
        has_split_name = ("name_sei" in fields) and ("name_mei" in fields)

        sender_display_name = str(self.sender_info.get("display_name") or self.sender_info.get("name") or DISPLAY_NAME).strip()
        sender_surname = str(self.sender_info.get("surname") or SURNAME).strip()
        sender_given_name = str(self.sender_info.get("given_name") or GIVEN_NAME).strip()
        sender_company = str(self.sender_info.get("company") or COMPANY_NAME).strip()
        sender_furigana_sei = str(self.sender_info.get("furigana_sei") or FURIGANA_SEI).strip()
        sender_furigana_mei = str(self.sender_info.get("furigana_mei") or FURIGANA_MEI).strip()
        if not sender_display_name:
            sender_display_name = DISPLAY_NAME
        if not sender_surname:
            sender_surname = SURNAME
        if not sender_given_name:
            sender_given_name = GIVEN_NAME
        if not sender_company:
            sender_company = COMPANY_NAME
        if not sender_furigana_sei:
            sender_furigana_sei = FURIGANA_SEI
        if not sender_furigana_mei:
            sender_furigana_mei = FURIGANA_MEI

        # Name policy:
        # - split (both found): surname/given
        # - otherwise: single display name on whichever name-like field exists
        field_values = {
            "name": sender_display_name,
            "name_sei": sender_surname if has_split_name else sender_display_name,
            "name_mei": sender_given_name if has_split_name else sender_display_name,
            "furigana": f"{sender_furigana_sei} {sender_furigana_mei}",
            "furigana_sei": sender_furigana_sei,
            "furigana_mei": sender_furigana_mei,
            "email": self.sender_info.get("email", ""),
            "phone": self.sender_info.get("phone", ""),
            "subject": subject,
            "message": message,
            "company": sender_company,
        }

        for field_type, locator in fields.items():
            value = field_values.get(field_type, "")
            if not value:
                continue

            # Kana fields are optional by default; fill only when required.
            if field_type in {"furigana", "furigana_sei", "furigana_mei"} and not required_flags.get(field_type, False):
                stats["skipped_optional"] += 1
                stats["field_details"][field_type] = "skipped_optional_furigana"
                continue

            should_fill = (
                required_flags.get(field_type, False)
                or field_type in high_conf_fields
                or field_type in {"name", "name_sei", "name_mei"}
            )
            if not should_fill:
                stats["skipped_optional"] += 1
                stats["field_details"][field_type] = "skipped_optional"
                continue

            try:
                await locator.click()
                await asyncio.sleep(0.1)
                tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "select":
                    # Select is handled in dropdown phase for required/safe defaults.
                    stats["skipped_optional"] += 1
                    stats["field_details"][field_type] = "skipped_select_handled_later"
                    continue
                await locator.fill(value)
                await asyncio.sleep(0.1)
                stats["filled"] += 1
                stats["filled_fields"].append(field_type)
                stats["field_details"][field_type] = "filled"
                logger.info("[FORM] filled %s", field_type)
            except Exception as e:
                stats["failed"] += 1
                stats["field_details"][field_type] = f"failed:{str(e)[:120]}"
                logger.warning("[FORM] fill failed %s: %s", field_type, e)

        for req_field in stats["detected_required_fields"]:
            status = str(stats["field_details"].get(req_field, ""))
            if not status.startswith("filled"):
                stats["missing_required_fields"].append(req_field)

        has_name = "name" in fields or "name_sei" in fields or "name_mei" in fields
        has_contact = "email" in fields and "message" in fields
        success = (stats["filled"] >= 2 or len(stats["filled_fields"]) >= 2) and (has_name or has_contact)

        logger.info(
            "[FORM] fill complete: filled=%s failed=%s skipped_optional=%s",
            stats["filled"],
            stats["failed"],
            stats["skipped_optional"],
        )
        return success, stats

    async def handle_checkboxes(self) -> list[str]:
        checked = []
        # Required visible checkboxes first.
        try:
            boxes = self.page.locator("input[type='checkbox']:visible")
            count = await boxes.count()
            for i in range(count):
                cb = boxes.nth(i)
                if await cb.is_checked():
                    continue
                if await self._is_required(cb):
                    await cb.check()
                    checked.append(f"required_checkbox_{i}")
                    logger.info("[FORM] checked required checkbox")
        except Exception:
            pass

        consent_labels = [
            "個人情報保護方針に同意",
            "個人情報保護方針",
            "プライバシーポリシー",
            "同意",
            "利用規約",
        ]
        for text in consent_labels:
            escaped = self._escape_css_text(text)
            try:
                labels = self.page.locator(f"label:has-text('{escaped}')")
                count = await labels.count()
                for i in range(min(count, 5)):
                    label = labels.nth(i)
                    cb = label.locator("input[type='checkbox']")
                    if await cb.count() > 0 and not await cb.first.is_checked():
                        await cb.first.check()
                        checked.append(f"consent:{text}")
                        logger.info("[FORM] checked consent checkbox by label: %s", text)
                        return checked
            except Exception:
                continue
        return checked

    async def handle_dropdowns(self, required_only: bool = True) -> list[str]:
        preferred_tokens = ["その他", "other", "一般"]
        placeholder_tokens = ["選択", "お選び", "choose", "--"]
        selected = []

        try:
            selects = self.page.locator("select:visible")
            count = await selects.count()
            for i in range(count):
                select = selects.nth(i)
                if required_only and not await self._is_required(select):
                    continue
                try:
                    current = (await select.input_value() or "").strip()
                except Exception:
                    current = ""

                if current:
                    continue

                options = select.locator("option")
                opt_count = await options.count()
                if opt_count == 0:
                    continue

                chosen_value = ""
                fallback_value = ""
                for oi in range(opt_count):
                    opt = options.nth(oi)
                    val = ((await opt.get_attribute("value")) or "").strip()
                    txt = ((await opt.inner_text()) or "").strip()
                    low = txt.lower()

                    if not val and not txt:
                        continue
                    if any(token in low for token in placeholder_tokens) and not val:
                        continue

                    if not fallback_value and val:
                        fallback_value = val

                    if any(token in low for token in preferred_tokens) and val:
                        chosen_value = val
                        break

                final_value = chosen_value or fallback_value
                if final_value:
                    await select.select_option(value=final_value)
                    selected.append(f"select_{i}:{final_value}")
                    logger.info("[FORM] dropdown selected: %s", final_value)
        except Exception as e:
            logger.warning("[FORM] dropdown handling error: %s", e)
        return selected

    async def fill_required_dates(self) -> list[str]:
        """Fill required date-like inputs with today's date."""
        today_dash = datetime.now(JST).strftime("%Y-%m-%d")
        today_slash = datetime.now(JST).strftime("%Y/%m/%d")
        date_hints = ["日付", "date", "希望日", "年月日", "来店日", "予約日"]
        filled = []

        try:
            inputs = self.page.locator("input:visible")
            count = await inputs.count()
            for i in range(count):
                field = inputs.nth(i)
                if not await self._is_required(field):
                    continue
                input_type = ((await field.get_attribute("type")) or "text").lower()
                meta_text = " ".join(
                    [
                        str(await field.get_attribute("name") or ""),
                        str(await field.get_attribute("id") or ""),
                        str(await field.get_attribute("placeholder") or ""),
                        str(await field.get_attribute("aria-label") or ""),
                    ]
                )
                is_date_like = (input_type == "date") or any(token in meta_text.lower() for token in [x.lower() for x in date_hints])
                if not is_date_like:
                    continue

                current = ""
                try:
                    current = (await field.input_value() or "").strip()
                except Exception:
                    current = ""
                if current:
                    continue

                try:
                    await field.fill(today_dash)
                    await asyncio.sleep(0.05)
                    check = (await field.input_value() or "").strip()
                    if not check:
                        await field.fill(today_slash)
                        await asyncio.sleep(0.05)
                        check = (await field.input_value() or "").strip()
                    if check:
                        filled.append(f"required_date_{i}")
                        logger.info("[FORM] filled required date: %s", check)
                except Exception:
                    continue
        except Exception as e:
            logger.warning("[FORM] required date fill error: %s", e)

        return filled

    async def validate_form_without_submit(self) -> dict:
        """Trigger safe client-side validation (no submit)."""
        payload = await self.page.evaluate(
            """
            () => {
              const firstForm = document.querySelector('form');
              let valid = true;
              if (firstForm && typeof firstForm.reportValidity === 'function') {
                valid = !!firstForm.reportValidity();
              }
              const invalids = Array.from(document.querySelectorAll(':invalid'));
              const errors = invalids.map((el) => {
                const id = el.getAttribute('id') || '';
                const name = el.getAttribute('name') || '';
                const placeholder = el.getAttribute('placeholder') || '';
                let label = '';
                if (id) {
                  const byFor = document.querySelector(`label[for="${id.replace(/"/g, '\\"')}"]`);
                  if (byFor && byFor.innerText) label = byFor.innerText.trim();
                }
                if (!label) {
                  const wrapped = el.closest('label');
                  if (wrapped && wrapped.innerText) label = wrapped.innerText.trim();
                }
                const token = label || name || id || placeholder || (el.tagName || '').toLowerCase();
                const message = el.validationMessage || 'invalid';
                return `${token}: ${message}`;
              });
              return { valid, errors };
            }
            """
        )
        errors = [str(x).strip() for x in (payload.get("errors") or []) if str(x).strip()]
        return {
            "valid": bool(payload.get("valid", True)) and not errors,
            "validation_errors": errors,
            "missing_required_fields": errors,
        }

    async def detect_captcha(self) -> bool:
        selectors = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            ".g-recaptcha",
            "#recaptcha",
            "[data-sitekey]",
            "iframe[title*='CAPTCHA']",
            "iframe[title*='reCAPTCHA']",
        ]
        for selector in selectors:
            try:
                if await self.page.locator(selector).count() > 0:
                    logger.warning("[FORM] captcha selector detected: %s", selector)
                    return True
            except Exception:
                continue

        try:
            body = (await self.page.inner_text("body")).lower()
            if any(
                token in body
                for token in [
                    "captcha",
                    "cloudflare",
                    "verify you are human",
                    "human verification",
                    "私はロボットではありません",
                ]
            ):
                logger.warning("[FORM] captcha/bot text detected")
                return True
        except Exception:
            pass

        return False

    async def find_submit_button(self) -> Tuple[Optional[Locator], str, bool]:
        """Find submit/confirm button.

        Returns:
            (locator, selector, is_confirm_step)
        """
        def selectors_from_text(text: str) -> list[str]:
            escaped = self._escape_css_text(text)
            return [
                f"button:has-text('{escaped}')",
                f"input[type='submit'][value*='{escaped}']",
                f"input[type='button'][value*='{escaped}']",
                f"a:has-text('{escaped}')",
            ]

        for text in CONFIRM_BUTTON_TEXTS:
            for selector in selectors_from_text(text):
                try:
                    loc = self.page.locator(selector)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        logger.info("[FORM] confirm button found: %s", selector)
                        return loc.first, selector, True
                except Exception:
                    continue

        for text in FINAL_SUBMIT_TEXTS:
            for selector in selectors_from_text(text):
                try:
                    loc = self.page.locator(selector)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        logger.info("[FORM] submit button found: %s", selector)
                        return loc.first, selector, False
                except Exception:
                    continue

        for selector in ["button[type='submit']", "input[type='submit']", "form button:visible", "form input[type='button']:visible"]:
            try:
                loc = self.page.locator(selector)
                if await loc.count() == 0 or not await loc.first.is_visible():
                    continue

                text = ""
                try:
                    text = ((await loc.first.inner_text()) or "").strip()
                except Exception:
                    text = ((await loc.first.get_attribute("value")) or "").strip()

                is_confirm = classify_submit_text(text) == "confirm"
                logger.info("[FORM] fallback submit button found: %s", selector)
                return loc.first, selector, is_confirm
            except Exception:
                continue

        logger.warning("[FORM] submit button not found")
        return None, "", False

    async def handle_confirmation_page(self) -> bool:
        """On confirmation page, click final submit if available."""
        await asyncio.sleep(1)

        try:
            body = (await self.page.inner_text("body")).lower()
        except Exception:
            body = ""

        confirm_markers = ["確認", "内容確認", "送信内容", "この内容で", "confirm"]
        if not any(marker.lower() in body for marker in confirm_markers):
            return False

        final_submit_selectors = [
            "button:has-text('送信')",
            "button:has-text('送信する')",
            "button:has-text('この内容で送信')",
            "button:has-text('送信内容を送信')",
            "button:has-text('確定')",
            "button:has-text('Submit')",
            "button:has-text('Send')",
            "input[type='submit'][value*='送信']",
            "input[type='submit'][value*='確定']",
            "input[type='submit'][value*='Submit']",
            "input[type='submit'][value*='Send']",
            "a:has-text('送信')",
            "a:has-text('Submit')",
            "a:has-text('Send')",
        ]

        for selector in final_submit_selectors:
            try:
                loc = self.page.locator(selector)
                if await loc.count() == 0 or not await loc.first.is_visible():
                    continue
                await loc.first.click()
                logger.info("[FORM] final submit clicked on confirmation page: %s", selector)
                await asyncio.sleep(1)
                return True
            except Exception:
                continue

        return False
