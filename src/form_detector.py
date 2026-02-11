"""Form detection and auto-fill for Japanese contact forms.

Implements robust selector strategies and safe SEMI_AUTO behavior support.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

CONTACT_PATH_PATTERNS = [
    "/contact",
    "/inquiry",
    "/お問い合わせ",
    "/お問合せ",
    "/otoiawase",
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

# Name filling policy
DISPLAY_NAME = "KIMOTO STUDIO"
SURNAME = "木許"
GIVEN_NAME = "裕輔"
COMPANY_NAME = "KIMOTO STUDIO"
FURIGANA_SEI = "キモト"
FURIGANA_MEI = "ユウスケ"

REQUIRED_MARKERS = ("必須", "*", "＊")

FIELD_PATTERNS = {
    "name": {
        "labels": ["お名前", "氏名", "ご氏名", "名前", "Name", "name"],
        "attributes": ["name", "your-name", "customer-name", "fullname", "your_name", "onamae"],
        "placeholders": ["お名前", "氏名", "Name", "フルネーム"],
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
        "labels": ["メール", "メールアドレス", "E-mail", "e-mail", "Email", "email"],
        "attributes": ["email", "your-email", "mail", "e-mail", "your_email"],
        "placeholders": ["メールアドレス", "example@example.com", "Email", "email"],
    },
    "phone": {
        "labels": ["電話", "TEL", "tel", "携帯", "連絡先", "電話番号"],
        "attributes": ["tel", "phone", "telephone", "your-tel", "your_phone", "mobile"],
        "placeholders": ["電話番号", "090-1234-5678", "TEL", "携帯"],
    },
    "subject": {
        "labels": ["件名", "タイトル", "subject", "Subject", "お問い合わせ種別"],
        "attributes": ["subject", "your-subject", "title"],
        "placeholders": ["件名", "Subject"],
    },
    "message": {
        "labels": [
            "お問い合わせ内容",
            "ご相談内容",
            "内容",
            "メッセージ",
            "本文",
            "message",
            "Message",
        ],
        "attributes": ["message", "your-message", "content", "body", "inquiry", "your_message", "comment"],
        "placeholders": ["お問い合わせ内容", "ご相談内容", "メッセージ", "本文", "Message"],
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

    @staticmethod
    def _escape_css_text(text: str) -> str:
        return str(text).replace("\\", "\\\\").replace("'", "\\'")

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
            if tag not in {"input", "textarea", "select"}:
                return False
            if tag == "input":
                input_type = ((await target.get_attribute("type")) or "text").lower()
                if input_type in {"hidden", "submit", "button", "image", "reset", "file"}:
                    return False
            return True
        except Exception:
            return False

    async def _has_contact_form(self) -> bool:
        try:
            if await self.page.locator("form").count() > 0:
                return True
            if await self.page.locator("textarea:visible").count() > 0:
                return True
            if await self.page.locator("input[type='email']:visible").count() > 0:
                return True
            if await self.page.locator("input[type='text']:visible, input[type='tel']:visible").count() >= 2:
                return True
            return False
        except Exception:
            return False

    async def find_contact_page(self, base_url: str) -> Optional[str]:
        logger.info("[FORM] Searching contact page: %s", base_url)

        # Strategy 0: current URL itself might already be a contact form page.
        try:
            response = await self.page.goto(base_url, timeout=self.timeout, wait_until="domcontentloaded")
            if response and response.status < 400 and await self._has_contact_form():
                logger.info("[FORM] Contact page found directly: %s", self.page.url)
                return self.page.url
        except Exception:
            pass

        # Strategy 1: common contact path probing.
        for path in CONTACT_PATH_PATTERNS:
            target = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
            try:
                response = await self.page.goto(target, timeout=self.timeout, wait_until="domcontentloaded")
                if response and response.status < 400 and await self._has_contact_form():
                    logger.info("[FORM] Contact page found via path: %s", self.page.url)
                    return self.page.url
            except Exception:
                continue

        # Strategy 2: follow contact-like links from homepage.
        try:
            await self.page.goto(base_url, timeout=self.timeout, wait_until="domcontentloaded")
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning("[FORM] Home load failed %s: %s", base_url, e)
            return None

        for text in CONTACT_LINK_TEXTS:
            escaped = self._escape_css_text(text)
            selector = f"a:has-text('{escaped}')"
            try:
                links = self.page.locator(selector)
                count = await links.count()
                for i in range(min(count, 5)):
                    href = await links.nth(i).get_attribute("href")
                    if not href:
                        continue
                    candidate = urljoin(self.page.url, href)
                    try:
                        response = await self.page.goto(candidate, timeout=self.timeout, wait_until="domcontentloaded")
                        if response and response.status < 400 and await self._has_contact_form():
                            logger.info("[FORM] Contact page found via link text '%s': %s", text, self.page.url)
                            return self.page.url
                    except Exception:
                        continue
            except Exception:
                continue

        # Strategy 3: href-based fallback search.
        try:
            all_links = self.page.locator("a[href]")
            count = await all_links.count()
            for i in range(min(count, 150)):
                href = await all_links.nth(i).get_attribute("href")
                if not href:
                    continue
                href_low = href.lower()
                if not any(token in href_low for token in ["contact", "inquiry", "otoiawase", "toiawase", "mail", "form"]):
                    continue
                candidate = urljoin(self.page.url, href)
                try:
                    response = await self.page.goto(candidate, timeout=self.timeout, wait_until="domcontentloaded")
                    if response and response.status < 400 and await self._has_contact_form():
                        logger.info("[FORM] Contact page found via href: %s", self.page.url)
                        return self.page.url
                except Exception:
                    continue
        except Exception:
            pass

        logger.warning("[FORM] No contact page found: %s", base_url)
        return None

    async def detect_form_fields(self) -> Tuple[Dict[str, Locator], dict]:
        detected: Dict[str, Locator] = {}
        form_map: Dict[str, str] = {}
        stats = {"total_checked": len(FIELD_PATTERNS), "found": 0}

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
            selector = f"input[aria-label*='{escaped}'], textarea[aria-label*='{escaped}'], select[aria-label*='{escaped}']"
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
                    inner = label.locator("input:visible, textarea:visible, select:visible")
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
                    loc = container.locator("input:visible, textarea:visible, select:visible")
                    if await self._is_fillable(loc):
                        return loc.first, f"nearby-text:{escaped}"
            except Exception:
                continue

        # Strategy 6: field type fallback.
        fallback_map = {
            "message": "textarea:visible",
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
                    if any(marker in label_text for marker in REQUIRED_MARKERS):
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
            return any(marker in str(wrapper_text) for marker in REQUIRED_MARKERS)
        except Exception:
            return False

    async def fill_form(self, fields: Dict[str, Locator], message: str, subject: str) -> Tuple[bool, dict]:
        stats = {"total_fields": len(fields), "filled": 0, "failed": 0, "skipped_optional": 0, "field_details": {}}

        # Name filling policy
        field_values = {
            "name": DISPLAY_NAME,
            "name_sei": SURNAME,
            "name_mei": GIVEN_NAME,
            "furigana": f"{FURIGANA_SEI} {FURIGANA_MEI}",
            "furigana_sei": FURIGANA_SEI,
            "furigana_mei": FURIGANA_MEI,
            "email": self.sender_info.get("email", ""),
            "phone": self.sender_info.get("phone", ""),
            "subject": subject,
            "message": message,
            "company": COMPANY_NAME,
        }

        # Pre-check required furigana fields only.
        furigana_required = {}
        for key in ["furigana", "furigana_sei", "furigana_mei"]:
            if key in fields:
                furigana_required[key] = await self._is_required(fields[key])

        for field_type, locator in fields.items():
            value = field_values.get(field_type, "")
            if not value:
                continue

            if field_type in furigana_required and not furigana_required[field_type]:
                stats["skipped_optional"] += 1
                stats["field_details"][field_type] = "skipped_optional_furigana"
                continue

            try:
                await locator.click()
                await asyncio.sleep(0.1)
                tag_name = (await locator.evaluate("el => el.tagName.toLowerCase()"))
                if tag_name == "select":
                    # Let handle_dropdowns() own select fields by default.
                    if field_type in {"subject"}:
                        stats["skipped_optional"] += 1
                        stats["field_details"][field_type] = "skipped_select_handled_later"
                        continue
                    await locator.select_option(label=value, timeout=1000)
                else:
                    await locator.fill(value)
                await asyncio.sleep(0.1)
                stats["filled"] += 1
                stats["field_details"][field_type] = "filled"
                logger.info("[FORM] filled %s", field_type)
            except Exception as e:
                stats["failed"] += 1
                stats["field_details"][field_type] = f"failed:{str(e)[:120]}"
                logger.warning("[FORM] fill failed %s: %s", field_type, e)

        has_name = "name" in fields or "name_sei" in fields or "name_mei" in fields
        has_contact = "email" in fields and "message" in fields
        success = stats["filled"] >= 2 and (has_name or has_contact)

        logger.info(
            "[FORM] fill complete: filled=%s failed=%s skipped_optional=%s",
            stats["filled"],
            stats["failed"],
            stats["skipped_optional"],
        )
        return success, stats

    async def handle_checkboxes(self) -> None:
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
                        logger.info("[FORM] checked consent checkbox by label: %s", text)
                        return
            except Exception:
                continue

    async def handle_dropdowns(self) -> None:
        preferred_tokens = ["その他", "other", "一般"]
        placeholder_tokens = ["選択", "お選び", "choose", "--"]

        try:
            selects = self.page.locator("select:visible")
            count = await selects.count()
            for i in range(count):
                select = selects.nth(i)
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
                    logger.info("[FORM] dropdown selected: %s", final_value)
        except Exception as e:
            logger.warning("[FORM] dropdown handling error: %s", e)

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
        confirm_texts = ["確認", "確認画面へ", "内容確認", "確認する"]
        final_submit_texts = ["送信", "送信する", "送信内容を送信", "確定", "この内容で送信", "Submit"]

        def selectors_from_text(text: str) -> list[str]:
            escaped = self._escape_css_text(text)
            return [
                f"button:has-text('{escaped}')",
                f"input[type='submit'][value*='{escaped}']",
                f"input[type='button'][value*='{escaped}']",
                f"a:has-text('{escaped}')",
            ]

        for text in confirm_texts:
            for selector in selectors_from_text(text):
                try:
                    loc = self.page.locator(selector)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        logger.info("[FORM] confirm button found: %s", selector)
                        return loc.first, selector, True
                except Exception:
                    continue

        for text in final_submit_texts:
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
                    text = ((await loc.first.inner_text()) or "").strip().lower()
                except Exception:
                    text = ((await loc.first.get_attribute("value")) or "").strip().lower()

                is_confirm = any(token in text for token in ["確認", "confirm"])
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
            "input[type='submit'][value*='送信']",
            "input[type='submit'][value*='確定']",
            "a:has-text('送信')",
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
