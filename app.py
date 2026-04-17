"""Launcher for local staff review dashboard.

Run:
    python app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from streamlit.web import cli as stcli


def main() -> int:
    dashboard = Path(__file__).resolve().parent / "src" / "staff_review_app.py"
    if not dashboard.exists():
        dashboard = Path(__file__).resolve().parent / "staff_review_app.py"
    if not dashboard.exists():
        dashboard = Path(__file__).resolve().parent / "tools" / "review_dashboard.py"
    # staff_review_app.py has its own bootstrap guard.
    os.environ["STAFF_REVIEW_APP_BOOTSTRAP"] = "1"
    sys.argv = ["streamlit", "run", str(dashboard)]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
