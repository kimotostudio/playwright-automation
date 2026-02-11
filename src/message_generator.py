"""Message generator with template-based personalization and plain-text wrapping."""

from __future__ import annotations

import json
import logging
import textwrap
from typing import Dict

logger = logging.getLogger(__name__)

SEPARATOR = "────────────────"


class MessageGenerator:
    """Generates personalized messages from template and sender info."""

    def __init__(
        self,
        template_path: str,
        sender_info_path: str,
        wrap_message: bool = True,
        wrap_width: int = 56,
    ):
        self.template = self._load_template(template_path)
        self.sender_info = self._load_sender_info(sender_info_path)
        self.wrap_message = bool(wrap_message)
        self.wrap_width = max(40, min(int(wrap_width), 60))

    def _load_template(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            template = f.read()
        logger.info(f"[MESSAGE] Template loaded: {len(template)} chars")
        return template

    def _load_sender_info(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f)
        logger.info(f"[MESSAGE] Sender info loaded: {info.get('company', 'unknown')}")
        return info

    def _wrap_paragraph(self, text: str) -> str:
        if not text.strip():
            return ""

        # Keep existing line breaks; wrap each line softly.
        lines = text.split("\n")
        wrapped_lines = []
        for line in lines:
            if not line.strip():
                wrapped_lines.append("")
                continue
            wrapped = textwrap.wrap(
                line,
                width=self.wrap_width,
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            wrapped_lines.extend(wrapped if wrapped else [line])
        return "\n".join(wrapped_lines)

    def _format_message(self, raw: str) -> str:
        text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()

        # Preserve paragraph structure with blank lines.
        paragraphs = text.split("\n\n")
        if self.wrap_message:
            paragraphs = [self._wrap_paragraph(p) for p in paragraphs]

        body = "\n\n".join(paragraphs).strip()

        # Ensure plain-text separators exist for readability.
        if SEPARATOR not in body:
            body = f"{SEPARATOR}\n{body}\n{SEPARATOR}"

        return body

    def generate(self, salon_name: str, demo_url: str) -> str:
        message = self.template.format(
            salon_name=salon_name,
            demo_url=demo_url,
        )
        message = self._format_message(message)
        logger.info(
            f"[MESSAGE] Generated message for {salon_name} ({len(message)} chars, "
            f"wrap={self.wrap_message}, width={self.wrap_width})"
        )
        return message

    def generate_subject(self, salon_name: str) -> str:
        return f"【ご確認】{salon_name}様向けのWebデザイン案"

    def get_sender_field(self, field: str) -> str:
        return self.sender_info.get(field, "")
