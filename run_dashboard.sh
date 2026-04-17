#!/bin/bash
# Local review dashboard launcher

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python &> /dev/null; then
    echo "Error: Python not found. Install Python 3.10+"
    exit 1
fi

if ! python -c "import streamlit, pandas" 2>/dev/null; then
    echo "Installing dashboard dependencies..."
    pip install -r requirements.txt
fi

python app.py
