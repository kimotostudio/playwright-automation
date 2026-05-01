#!/bin/bash
# Playwright Automation - Daily Runner
# Usage: ./run.sh [--test]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Check Python
if ! command -v python &> /dev/null; then
    echo "Error: Python not found. Install Python 3.10+"
    exit 1
fi

# Check dependencies
if ! python -c "import playwright" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    playwright install chromium
fi

# Test mode
if [ "$1" == "--test" ]; then
    echo "Running in TEST MODE (limit: 2)"
    python -c "
import json
with open('config/settings.json', 'r', encoding='utf-8') as f:
    s = json.load(f)
s['test_mode'] = True
with open('config/settings.json', 'w', encoding='utf-8') as f:
    json.dump(s, f, ensure_ascii=False, indent=2)
"
fi

# Run
python src/main.py

# Reset test mode
if [ "$1" == "--test" ]; then
    python -c "
import json
with open('config/settings.json', 'r', encoding='utf-8') as f:
    s = json.load(f)
s['test_mode'] = False
with open('config/settings.json', 'w', encoding='utf-8') as f:
    json.dump(s, f, ensure_ascii=False, indent=2)
"
fi
