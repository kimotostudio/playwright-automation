"""Message generator with template-based personalization and plain-text wrapping."""

from __future__ import annotations

import json
import logging
import re
import textwrap
from typing import Dict

logger = logging.getLogger(__name__)

SEPARATOR = "────────────────"
URL_RE = re.compile(r"https?://[^\s]+")
SEPARATOR_RE = re.compile(r"^[\s\-_=*/・／─—–―]+$")


class MessageGenerator:
    """Generates personalized messages from template and sender info."""

    def __init__(
        self,
        template_path: str,
        sender_info_path: str,
        wrap_message: bool = True,
        wrap_width: int = 56,
        debug: bool = False,
    ):
        self.template = self._load_template(template_path)
        self.sender_info = self._load_sender_info(sender_info_path)
        self.wrap_message = bool(wrap_message)
        self.wrap_width = max(40, min(int(wrap_width), 60))
        self.debug = bool(debug)

    def _load_template(self, path: str) -> str:
        with open(path, "r", encoding="utf-8-sig") as f:
            template = f.read()
        logger.info(f"[MESSAGE] Template loaded: {len(template)} chars")
        return template

    def _load_sender_info(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8-sig") as f:
            info = json.load(f)
        logger.info(f"[MESSAGE] Sender info loaded: {info.get('company', 'unknown')}")
        return info

    @staticmethod
    def _is_separator_paragraph(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        return all(ch in {"─", "-", "_", "=", "*", "／", "/", "・", " "} for ch in stripped)

    def _wrap_line_keep_url(self, line: str) -> str:
        if not line.strip():
            return ""

        urls: list[str] = []

        def _mask_url(match: re.Match[str]) -> str:
            token = f"__URL_TOKEN_{len(urls)}__"
            urls.append(match.group(0))
            return token

        masked = URL_RE.sub(_mask_url, line)
        wrapped = textwrap.fill(
            masked,
            width=self.wrap_width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        restored = wrapped
        for idx, url in enumerate(urls):
            restored = restored.replace(f"__URL_TOKEN_{idx}__", url)
        return restored

    def _wrap_paragraph(self, text: str) -> str:
        if not text.strip():
            return ""
        if self._is_separator_paragraph(text):
            return text

        # Keep existing line breaks; wrap each line softly.
        lines = text.split("\n")
        wrapped_lines = []
        for line in lines:
            if not line.strip():
                wrapped_lines.append("")
                continue
            wrapped_lines.append(self._wrap_line_keep_url(line))
        return "\n".join(wrapped_lines)

    def _format_message(self, raw: str) -> str:
        text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()

        # Preserve paragraph structure with blank lines.
        paragraphs = text.split("\n\n")
        if self.wrap_message:
            paragraphs = [self._wrap_paragraph(p) for p in paragraphs]

        return "\n\n".join(paragraphs).strip()

    @staticmethod
    def _contains_non_ascii(text: str) -> bool:
        return any(ord(ch) > 127 for ch in text)

    def sanitize_message_for_legacy_encodings(self, text: str) -> str:
        """Normalize text to reduce mojibake risk on legacy encoded forms."""
        if not text:
            return ""

        replacements = {
            "─": "-",
            "—": "-",
            "–": "-",
            "―": "-",
            "〜": "~",
            "～": "~",
            "…": "...",
            "“": '"',
            "”": '"',
            "’": "'",
        }
        normalized = "".join(replacements.get(ch, ch) for ch in text)

        # Keep paragraph structure; normalize visual separators to ASCII.
        out_lines: list[str] = []
        for line in normalized.split("\n"):
            stripped = line.strip()
            if stripped and SEPARATOR_RE.match(stripped):
                out_lines.append("-----")
                continue
            clean_line = "".join(ch for ch in line if ch == "\t" or ch >= " " or ch in {"\n", "\r"})
            out_lines.append(clean_line)

        cleaned = "\n".join(out_lines)
        if self.debug:
            logger.debug("[MESSAGE_DEBUG] sanitized repr(message[:200])=%s", repr(cleaned[:200]))
        return cleaned

    def _warn_if_mojibake_like(self, message: str) -> None:
        q_count = message.count("?")
        replacement_count = message.count("�")
        high_question_rate = q_count >= max(8, len(message) // 20)
        if replacement_count > 0 or high_question_rate:
            logger.warning(
                "[MESSAGE] Potential mojibake detected: question_marks=%s replacement_chars=%s len=%s",
                q_count,
                replacement_count,
                len(message),
            )

    @staticmethod
    def _normalize_key(value: str) -> str:
        normalized = str(value or "").strip().lower()
        normalized = normalized.replace("（", "(").replace("）", ")").replace("　", "")
        return normalized.replace(" ", "")

    @classmethod
    def _pick_field(cls, row: dict, keys: list[str]) -> str:
        if not isinstance(row, dict):
            return ""
        normalized = {cls._normalize_key(k): v for k, v in row.items()}
        for key in keys:
            value = row.get(key) if isinstance(row, dict) else None
            if value is not None and str(value).strip():
                return str(value).strip()
            alt = normalized.get(cls._normalize_key(key))
            if alt is not None and str(alt).strip():
                return str(alt).strip()
        return ""

    def resolve_lead_fields(self, row: dict) -> Dict[str, str]:
        """Resolve lead fields from row with robust Japanese header fallbacks."""
        salon_name = self._pick_field(row, ["店名", "名称", "サロン名", "店舗名", "salon_name", "name"])
        demo_url = self._pick_field(
            row,
            ["url(デモ)", "url(デモページ)", "url（デモ）", "url（デモページ）", "demo_url", "url_demo"],
        )
        old_url = self._pick_field(row, ["url(旧)", "URL", "url", "url（旧）", "old_url"])
        return {
            "salon_name": salon_name,
            "demo_url": demo_url,
            "old_url": old_url,
        }

    def generate(self, salon_name: str, demo_url: str) -> str:
        message = self.template.format(
            salon_name=salon_name,
            demo_url=demo_url,
        )
        message = self._format_message(message)
        message = self.sanitize_message_for_legacy_encodings(message)
        if self.debug:
            logger.debug("[MESSAGE_DEBUG] repr(message[:200])=%s", repr(message[:200]))
            logger.debug("[MESSAGE_DEBUG] contains_non_ascii=%s", self._contains_non_ascii(message))
        self._warn_if_mojibake_like(message)
        logger.info(
            f"[MESSAGE] Generated message for {salon_name} ({len(message)} chars, "
            f"wrap={self.wrap_message}, width={self.wrap_width})"
        )
        return message

    def generate_subject(self, salon_name: str) -> str:
        subject_template = str(self.sender_info.get("subject_template") or self.sender_info.get("subject") or "").strip()
        if subject_template:
            try:
                return subject_template.format(salon_name=salon_name)
            except Exception:
                return subject_template
        return f"【ご確認】{salon_name}様向けのWebデザイン案"

    def get_sender_field(self, field: str) -> str:
        return self.sender_info.get(field, "")
