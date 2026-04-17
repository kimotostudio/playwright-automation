"""Rate limiter with atomic state persistence and JST reset."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Dict, Set
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")

DEFAULT_STATE = {
    "last_run_date": "",
    "today_count": 0,   # sent only
    "total_sent": 0,
    "last_lead_id": "",
    "completed_ids": [],  # sent only
}


class RateLimiter:
    """Manages daily sent limits with atomic state updates."""

    def __init__(self, state_path: str, daily_limit: int = 10, ledger_ids: Set[str] | None = None):
        self.state_path = state_path
        self.daily_limit = daily_limit
        self.ledger_ids = {str(x) for x in (ledger_ids or set())}
        self.state = self._load_state()
        self._check_date_reset()
        self._merge_ledger_ids()

    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                state = DEFAULT_STATE.copy()
                state.update(payload if isinstance(payload, dict) else {})
                if not isinstance(state.get("completed_ids"), list):
                    state["completed_ids"] = []
                state["completed_ids"] = [str(x) for x in state["completed_ids"]]
                state["today_count"] = int(state.get("today_count", 0))
                state["total_sent"] = int(state.get("total_sent", 0))
                logger.info(f"[RATE_LIMITER] State loaded: {state['today_count']}/{self.daily_limit} today")
                return state
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.error(f"[RATE_LIMITER] Corrupt state file ({e}), rebuilding default state")
        return DEFAULT_STATE.copy()

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp_path = f"{self.state_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.state_path)

    def _check_date_reset(self) -> None:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        if self.state.get("last_run_date") != today:
            old = int(self.state.get("today_count", 0))
            self.state["last_run_date"] = today
            self.state["today_count"] = 0
            self._save_state()
            if old > 0:
                logger.info(f"[RATE_LIMITER] JST day rollover: today_count {old} -> 0")

    def _merge_ledger_ids(self) -> None:
        merged = 0
        for salon_id in self.ledger_ids:
            if salon_id not in self.state["completed_ids"]:
                self.state["completed_ids"].append(salon_id)
                merged += 1
        if merged:
            self._save_state()
            logger.info(f"[RATE_LIMITER] Merged {merged} sent IDs from ledger")

    def can_submit(self) -> bool:
        return int(self.state["today_count"]) < int(self.daily_limit)

    def remaining(self) -> int:
        return max(0, int(self.daily_limit) - int(self.state["today_count"]))

    def record_submission(self, lead_id: str) -> None:
        salon_id = str(lead_id)
        self.state["today_count"] = int(self.state["today_count"]) + 1
        self.state["total_sent"] = int(self.state["total_sent"]) + 1
        self.state["last_lead_id"] = salon_id
        if salon_id not in self.state["completed_ids"]:
            self.state["completed_ids"].append(salon_id)
        self._save_state()
        logger.info(
            f"[RATE_LIMITER] Sent recorded: {self.state['today_count']}/{self.daily_limit} today, "
            f"total_sent={self.state['total_sent']}"
        )

    def record_prepared(self, lead_id: str) -> None:
        """Prepared does not affect sent counters or completed_ids."""
        self.state["last_lead_id"] = str(lead_id)
        self._save_state()

    def record_skip(self, lead_id: str) -> None:
        """Skip does not mark completed_ids (sent only)."""
        self.state["last_lead_id"] = str(lead_id)
        self._save_state()

    def is_completed(self, lead_id: str) -> bool:
        salon_id = str(lead_id)
        return salon_id in self.state["completed_ids"] or salon_id in self.ledger_ids

    def get_stats(self) -> Dict[str, object]:
        return {
            "date": self.state.get("last_run_date", ""),
            "today_count": int(self.state.get("today_count", 0)),
            "daily_limit": int(self.daily_limit),
            "remaining": self.remaining(),
            "total_sent": int(self.state.get("total_sent", 0)),
            "completed_leads": len(self.state.get("completed_ids", [])),
        }
