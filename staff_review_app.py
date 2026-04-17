"""Launcher for Staff Review Dashboard.

Run:
  python staff_review_app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from streamlit.web import cli as stcli


def main() -> int:
    app_path = Path(__file__).resolve().parent / "src" / "staff_review_app.py"
    os.environ["STAFF_REVIEW_APP_BOOTSTRAP"] = "1"
    sys.argv = ["streamlit", "run", str(app_path)]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
