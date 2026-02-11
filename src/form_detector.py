"""Form detection and auto-fill for Japanese salon contact pages.

Supports: Jimdo, Wix, Peraichi, Ameba Ownd, generic HTML forms.
Enhanced with: split name fields, dropdown handling, aria-label, form_map logging.
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from playwright.async_api import Page, Locator, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# Patterns for finding contact pages
CONTACT_PATH_PATTERNS = [
    "/contact",
    "/inquiry",
    "/お問い合わせ",
    "/otoiawase",
    "/mail",
    "/form",
    "/ask",
    "/contact-us",
    "/enquiry",
    "/toiawase",
    "/soudan",
    "/相談",
]

# Link text patterns for finding contact page links
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
    "お気軽に",
    "ご連絡",
]

# Expanded field detection patterns (追い②)
FIELD_PATTERNS = {
    "name": {
        "labels": [
            "お名前", "名前", "氏名", "ご氏名", "name", "Name", "NAME",
            "お客様名", "ご担当者名", "ご氏名（フルネーム）", "お名前（フルネーム）",
        ],
        "attributes": [
            "name", "your-name", "customer-name", "fullname", "your_name",
            "field-name", "contact-name", "氏名", "onamae",
        ],
        "placeholders": [
            "お名前", "名前", "氏名", "Your Name", "例）山田太郎",
            "例：山田太郎", "フルネーム",
        ],
    },
    "name_sei": {
        "labels": ["姓", "苗字", "名字", "Last Name", "last name"],
        "attributes": ["sei", "last-name", "lastname", "family-name", "name_sei", "your-sei"],
        "placeholders": ["姓", "例）山田", "苗字"],
    },
    "name_mei": {
        "labels": ["名", "First Name", "first name"],
        "attributes": ["mei", "first-name", "firstname", "given-name", "name_mei", "your-mei"],
        "placeholders": ["名", "例）太郎"],
    },
    "email": {
        "labels": [
            "メールアドレス", "メール", "email", "Email", "E-mail", "e-mail",
            "Eメール", "ご連絡先メール", "E-MAIL", "連絡先メール",
        ],
        "attributes": [
            "email", "your-email", "mail", "e-mail", "your_email",
            "contact-email", "field-email",
        ],
        "placeholders": [
            "メールアドレス", "example@example.com", "email", "Email",
            "例）example@example.com", "メール",
        ],
    },
    "phone": {
        "labels": [
            "電話番号", "電話", "TEL", "tel", "お電話", "ご連絡先",
            "携帯", "携帯番号", "連絡先電話番号", "TEL（携帯）",
        ],
        "attributes": [
            "tel", "phone", "telephone", "your-tel", "your_phone",
            "mobile", "contact-phone", "携帯",
        ],
        "placeholders": [
            "電話番号", "090-1234-5678", "TEL", "ハイフンなし",
            "例）09012345678", "携帯番号",
        ],
    },
    "subject": {
        "labels": ["件名", "タイトル", "subject", "Subject", "ご用件", "お問い合わせ種類"],
        "attributes": ["subject", "your-subject", "title", "件名"],
        "placeholders": ["件名", "Subject", "ご用件"],
    },
    "message": {
        "labels": [
            "お問い合わせ内容", "メッセージ", "内容", "本文", "message", "Message",
            "ご相談内容", "備考", "お問合せ内容", "ご質問・ご要望",
            "ご用件の詳細", "詳細", "お問い合わせ", "ご質問内容",
        ],
        "attributes": [
            "message", "your-message", "content", "body", "inquiry",
            "your_message", "comment", "remarks", "naiyo",
        ],
        "placeholders": [
            "お問い合わせ内容", "メッセージ", "ご自由にお書きください", "Message",
            "こちらにご記入ください", "お問い合わせ内容をご記入ください",
        ],
    },
    "company": {
        "labels": ["会社名", "法人名", "団体名", "organization", "company", "屋号"],
        "attributes": ["company", "organization", "your-company", "company-name"],
        "placeholders": ["会社名", "法人名", "Company", "屋号"],
    },
}


class FormDetector:
    """Detects and fills contact forms on Japanese websites."""

    def __init__(self, page: Page, sender_info: dict, timeout: int = 30):
        self.page = page
        self.sender_info = sender_info
        self.timeout = timeout * 1000  # ms

    async def find_contact_page(self, base_url: str) -> Optional[str]:
        """Find the contact page URL from a salon website."""
        logger.info(f"[FORM] Searching for contact page: {base_url}")

        # Strategy 1: Try common URL paths
        for path in CONTACT_PATH_PATTERNS:
            url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
            try:
                response = await self.page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
                if response and response.status == 200:
                    has_form = await self.page.locator("form").count() > 0
                    has_textarea = await self.page.locator("textarea").count() > 0
                    has_inputs = await self.page.locator("input[type='text'], input[type='email']").count() > 0
                    if has_form or has_textarea or has_inputs:
                        logger.info(f"[FORM] Contact page found via path: {url}")
                        return url
            except (PlaywrightTimeout, Exception):
                continue

        # Strategy 2: Go to homepage and look for contact links
        try:
            await self.page.goto(base_url, timeout=self.timeout, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        except (PlaywrightTimeout, Exception) as e:
            logger.error(f"[ERROR] Cannot load homepage {base_url}: {e}")
            return None

        for link_text in CONTACT_LINK_TEXTS:
            try:
                links = self.page.locator(f"a:has-text('{link_text}')")
                count = await links.count()
                if count > 0:
                    href = await links.first.get_attribute("href")
                    if href:
                        contact_url = urljoin(base_url, href)
                        logger.info(f"[FORM] Contact link found: '{link_text}' -> {contact_url}")
                        return contact_url
            except Exception:
                continue

        # Strategy 3: Check all links for contact-related hrefs
        try:
            all_links = await self.page.locator("a[href]").all()
            for link in all_links:
                try:
                    href = await link.get_attribute("href")
                    if href:
                        href_lower = href.lower()
                        for pattern in CONTACT_PATH_PATTERNS:
                            if pattern.lower() in href_lower:
                                contact_url = urljoin(base_url, href)
                                logger.info(f"[FORM] Contact URL found in href: {contact_url}")
                                return contact_url
                except Exception:
                    continue
        except Exception:
            pass

        logger.warning(f"[FORM] No contact page found for {base_url}")
        return None

    async def detect_form_fields(self) -> Tuple[Dict[str, Optional[Locator]], dict]:
        """Detect form fields on the current page.

        Returns:
            (fields_dict, form_map) - fields maps type->Locator, form_map is for logging.
        """
        detected = {}
        form_map = {}
        stats = {"total_checked": 0, "found": 0}

        for field_type, patterns in FIELD_PATTERNS.items():
            stats["total_checked"] += 1
            locator, selector_used = await self._find_field(field_type, patterns)
            if locator:
                detected[field_type] = locator
                form_map[field_type] = selector_used
                stats["found"] += 1
                logger.info(f"[FORM] Field detected: {field_type} via {selector_used}")

        logger.info(f"[FORM] Detection complete: {stats['found']}/{stats['total_checked']} fields found")
        form_map["_stats"] = stats
        return detected, form_map

    async def _find_field(self, field_type: str, patterns: dict) -> Tuple[Optional[Locator], str]:
        """Find a form field using multiple detection strategies.

        Returns:
            (locator, selector_description) or (None, "").
        """

        # Strategy 1: Match by input name/id attribute
        for attr_val in patterns["attributes"]:
            for selector in [
                f"input[name='{attr_val}']",
                f"input[id='{attr_val}']",
                f"input[name*='{attr_val}']",
                f"input[id*='{attr_val}']",
                f"textarea[name='{attr_val}']",
                f"textarea[id='{attr_val}']",
                f"textarea[name*='{attr_val}']",
            ]:
                try:
                    loc = self.page.locator(selector)
                    if await loc.count() > 0:
                        if await loc.first.is_visible():
                            return loc.first, f"attr:{selector}"
                except Exception:
                    continue

        # Strategy 2: Match by placeholder text
        for placeholder in patterns["placeholders"]:
            for tag in ["input", "textarea"]:
                try:
                    loc = self.page.locator(f"{tag}[placeholder*='{placeholder}']")
                    if await loc.count() > 0:
                        if await loc.first.is_visible():
                            return loc.first, f"placeholder:{placeholder}"
                except Exception:
                    continue

        # Strategy 3: Match by aria-label
        for label_text in patterns["labels"]:
            try:
                loc = self.page.locator(f"input[aria-label*='{label_text}'], textarea[aria-label*='{label_text}']")
                if await loc.count() > 0 and await loc.first.is_visible():
                    return loc.first, f"aria-label:{label_text}"
            except Exception:
                continue

        # Strategy 4: Match by associated label text
        for label_text in patterns["labels"]:
            try:
                label = self.page.locator(f"label:has-text('{label_text}')")
                if await label.count() > 0:
                    for_attr = await label.first.get_attribute("for")
                    if for_attr:
                        target = self.page.locator(f"#{for_attr}")
                        if await target.count() > 0 and await target.first.is_visible():
                            return target.first, f"label-for:{label_text}->{for_attr}"
                    # Input inside label
                    inner_input = label.first.locator("input, textarea")
                    if await inner_input.count() > 0:
                        return inner_input.first, f"label-inner:{label_text}"
            except Exception:
                continue

        # Strategy 5: Match by nearby text node (text before input)
        for label_text in patterns["labels"]:
            try:
                # Look for text followed by input within common containers
                containers = self.page.locator(f"div:has-text('{label_text}'), td:has-text('{label_text}'), p:has-text('{label_text}')")
                count = await containers.count()
                for ci in range(min(count, 3)):
                    container = containers.nth(ci)
                    inner = container.locator("input:visible, textarea:visible")
                    if await inner.count() > 0:
                        return inner.first, f"nearby-text:{label_text}"
            except Exception:
                continue

        # Strategy 6: Type-specific fallbacks
        if field_type == "message":
            try:
                textareas = self.page.locator("textarea:visible")
                if await textareas.count() > 0:
                    return textareas.first, "fallback:textarea"
            except Exception:
                pass

        if field_type == "email":
            try:
                email_input = self.page.locator("input[type='email']:visible")
                if await email_input.count() > 0:
                    return email_input.first, "fallback:type=email"
            except Exception:
                pass

        if field_type == "phone":
            try:
                tel_input = self.page.locator("input[type='tel']:visible")
                if await tel_input.count() > 0:
                    return tel_input.first, "fallback:type=tel"
            except Exception:
                pass

        return None, ""

    async def fill_form(self, fields: Dict[str, Locator], message: str, subject: str) -> Tuple[bool, dict]:
        """Fill detected form fields with sender information.

        Handles split name fields (姓/名) and other Japanese patterns.
        """
        stats = {"total_fields": len(fields), "filled": 0, "failed": 0, "field_details": {}}

        # Split sender name for sei/mei fields
        full_name = self.sender_info.get("name", "")
        name_parts = full_name.split() if full_name else ["", ""]
        sei = name_parts[0] if len(name_parts) >= 1 else full_name
        mei = name_parts[1] if len(name_parts) >= 2 else ""

        field_values = {
            "name": full_name,
            "name_sei": sei,
            "name_mei": mei,
            "email": self.sender_info.get("email", ""),
            "phone": self.sender_info.get("phone", ""),
            "subject": subject,
            "message": message,
            "company": self.sender_info.get("company", ""),
        }

        for field_type, locator in fields.items():
            value = field_values.get(field_type, "")
            if not value:
                continue
            try:
                await locator.click()
                await asyncio.sleep(0.3)
                await locator.fill(value)
                await asyncio.sleep(0.2)
                stats["filled"] += 1
                stats["field_details"][field_type] = "filled"
                logger.info(f"[FORM] Filled {field_type}: {value[:30]}...")
            except Exception as e:
                stats["failed"] += 1
                stats["field_details"][field_type] = f"failed: {e}"
                logger.error(f"[ERROR] Failed to fill {field_type}: {e}")

        # If split name fields exist but full name field doesn't, count as success
        has_name = "name" in fields or ("name_sei" in fields and "name_mei" in fields)
        has_contact = "email" in fields or "message" in fields
        success = stats["filled"] >= 2 and (has_name or has_contact)

        logger.info(f"[FORM] Fill complete: {stats['filled']}/{stats['total_fields']} fields filled")
        return success, stats

    async def handle_checkboxes(self) -> None:
        """Handle common checkbox patterns (privacy policy, consent, etc.)."""
        checkbox_patterns = [
            "input[type='checkbox'][name*='privacy']",
            "input[type='checkbox'][name*='consent']",
            "input[type='checkbox'][name*='agree']",
            "input[type='checkbox'][name*='policy']",
            "input[type='checkbox'][name*='check']",
            "input[type='checkbox'][name*='confirm']",
            "input[type='checkbox'][name*='accept']",
        ]
        for pattern in checkbox_patterns:
            try:
                checkboxes = self.page.locator(pattern)
                count = await checkboxes.count()
                for i in range(count):
                    cb = checkboxes.nth(i)
                    if not await cb.is_checked():
                        await cb.check()
                        logger.info(f"[FORM] Checkbox checked: {pattern}")
            except Exception:
                continue

        # Label-based checkboxes (common in Japanese forms)
        privacy_labels = [
            "プライバシーポリシー",
            "個人情報",
            "同意",
            "利用規約",
            "承諾",
            "個人情報保護方針",
            "個人情報保護",
            "に同意",
            "プライバシー",
        ]
        for label_text in privacy_labels:
            try:
                label = self.page.locator(f"label:has-text('{label_text}')")
                if await label.count() > 0:
                    cb = label.first.locator("input[type='checkbox']")
                    if await cb.count() > 0 and not await cb.first.is_checked():
                        await cb.first.check()
                        logger.info(f"[FORM] Privacy checkbox checked: {label_text}")
            except Exception:
                continue

        # Also try standalone checkboxes near privacy text
        try:
            all_checkboxes = self.page.locator("input[type='checkbox']:visible")
            count = await all_checkboxes.count()
            if count == 1:
                # If there's only one visible checkbox, it's likely a consent checkbox
                cb = all_checkboxes.first
                if not await cb.is_checked():
                    await cb.check()
                    logger.info("[FORM] Single checkbox checked (likely consent)")
        except Exception:
            pass

    async def handle_dropdowns(self) -> None:
        """Handle dropdown menus (category/inquiry type) common in Japanese forms."""
        dropdown_labels = [
            "お問い合わせ種別",
            "お問い合わせ種類",
            "カテゴリ",
            "ご用件",
            "種別",
            "お問合せ種別",
        ]
        safe_options = ["その他", "その他のお問い合わせ", "一般的なお問い合わせ", "ご質問"]

        for label_text in dropdown_labels:
            try:
                label = self.page.locator(f"label:has-text('{label_text}')")
                if await label.count() == 0:
                    continue

                for_attr = await label.first.get_attribute("for")
                select = None
                if for_attr:
                    select = self.page.locator(f"select#{for_attr}")
                else:
                    select = label.first.locator("select")

                if select and await select.count() > 0:
                    # Try safe options first
                    options = await select.first.locator("option").all()
                    selected = False
                    for safe_opt in safe_options:
                        for opt in options:
                            text = (await opt.inner_text()).strip()
                            if safe_opt in text:
                                value = await opt.get_attribute("value")
                                if value:
                                    await select.first.select_option(value=value)
                                    logger.info(f"[FORM] Dropdown selected: {label_text} -> {text}")
                                    selected = True
                                    break
                        if selected:
                            break

                    # If no safe option found, select first non-empty option
                    if not selected and len(options) > 1:
                        for opt in options[1:]:
                            value = await opt.get_attribute("value")
                            if value and value.strip():
                                text = (await opt.inner_text()).strip()
                                await select.first.select_option(value=value)
                                logger.info(f"[FORM] Dropdown fallback: {label_text} -> {text}")
                                break
            except Exception as e:
                logger.warning(f"[FORM] Dropdown handling failed for {label_text}: {e}")
                continue

        # Also try detecting standalone select elements with common names
        select_name_patterns = [
            "select[name*='category']",
            "select[name*='type']",
            "select[name*='subject']",
            "select[name*='kind']",
        ]
        for pattern in select_name_patterns:
            try:
                selects = self.page.locator(pattern)
                if await selects.count() > 0:
                    select = selects.first
                    # Check if already has a selection
                    current = await select.input_value()
                    if not current or current == "":
                        options = await select.locator("option").all()
                        if len(options) > 1:
                            # Select first non-empty
                            for opt in options[1:]:
                                value = await opt.get_attribute("value")
                                if value and value.strip():
                                    await select.select_option(value=value)
                                    text = (await opt.inner_text()).strip()
                                    logger.info(f"[FORM] Standalone dropdown: {pattern} -> {text}")
                                    break
            except Exception:
                continue

    async def detect_captcha(self) -> bool:
        """Check if the page has a CAPTCHA."""
        captcha_indicators = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            ".g-recaptcha",
            "#recaptcha",
            "[data-sitekey]",
            "iframe[title*='reCAPTCHA']",
        ]
        for selector in captcha_indicators:
            try:
                if await self.page.locator(selector).count() > 0:
                    logger.warning("[FORM] CAPTCHA detected - will skip submission")
                    return True
            except Exception:
                continue
        return False

    async def find_submit_button(self) -> Tuple[Optional[Locator], str, bool]:
        """Find the form submit button.

        Returns:
            (locator, selector_string, is_confirm_step)
            - locator: the button Locator (or None)
            - selector_string: CSS selector used to find it (for resume_submit)
            - is_confirm_step: True if button leads to a confirmation page (確認),
              False if it's a direct send (送信) button.
        """
        # Confirm-step selectors (clicking leads to confirmation page, NOT final send)
        confirm_selectors = [
            "button:has-text('確認画面へ')",
            "button:has-text('確認画面へ進む')",
            "button:has-text('確認する')",
            "button:has-text('確認')",
            "input[value='確認画面へ']",
            "input[value='確認画面へ進む']",
            "input[value='確認する']",
            "input[value='確認']",
            "a:has-text('確認画面へ')",
            "a:has-text('確認')",
        ]
        # Direct-send selectors (clicking submits the form)
        # Keep broad type=submit selectors near the end so explicit confirm labels win.
        send_selectors = [
            "button:has-text('送信')",
            "button:has-text('送信する')",
            "button:has-text('送る')",
            "button:has-text('Submit')",
            "input[value='送信']",
            "input[value='送信する']",
            "a:has-text('送信')",
            "button[type='submit']",
            "input[type='submit']",
        ]

        # Try confirm-step first to avoid mistaking "確認" buttons as final send.
        for selector in confirm_selectors:
            try:
                loc = self.page.locator(selector)
                if await loc.count() > 0 and await loc.first.is_visible():
                    logger.info(f"[FORM] Submit button found (confirm step): {selector}")
                    return loc.first, selector, True
            except Exception:
                continue

        # Then try direct-send selectors.
        for selector in send_selectors:
            try:
                loc = self.page.locator(selector)
                if await loc.count() > 0 and await loc.first.is_visible():
                    try:
                        text = (await loc.first.inner_text()).strip().lower()
                    except Exception:
                        text = ((await loc.first.get_attribute("value")) or "").strip().lower()
                    if any(k in text for k in ["確認", "confirm"]):
                        logger.info(f"[FORM] Submit button found (confirm-like text): {selector}")
                        return loc.first, selector, True
                    logger.info(f"[FORM] Submit button found (direct send): {selector}")
                    return loc.first, selector, False
            except Exception:
                continue

        # Fallback: any button inside a form
        try:
            form_buttons = self.page.locator("form button:visible")
            if await form_buttons.count() > 0:
                return form_buttons.last, "form button:visible (fallback)", False
        except Exception:
            pass

        logger.warning("[FORM] No submit button found")
        return None, "", False

    async def handle_confirmation_page(self) -> bool:
        """Handle two-step submission (confirm -> send) common in Japanese forms."""
        await asyncio.sleep(2)

        confirm_send_selectors = [
            "button:has-text('送信')",
            "button:has-text('送信する')",
            "input[value='送信']",
            "input[value='送信する']",
            "button:has-text('この内容で送信')",
            "button:has-text('上記の内容で送信')",
            "button:has-text('上記内容で送信する')",
            "a:has-text('送信する')",
        ]

        page_text = await self.page.inner_text("body")
        confirmation_keywords = ["確認", "以下の内容", "送信してよろしいですか", "入力内容の確認", "内容をご確認"]
        is_confirmation = any(kw in page_text for kw in confirmation_keywords)

        if is_confirmation:
            logger.info("[FORM] Confirmation page detected")
            for selector in confirm_send_selectors:
                try:
                    loc = self.page.locator(selector)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click()
                        logger.info(f"[FORM] Clicked final submit: {selector}")
                        await asyncio.sleep(2)
                        return True
                except Exception:
                    continue

        return False
