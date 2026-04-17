@echo off
REM Local review dashboard launcher (Windows)

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python not found. Install Python 3.10+
    exit /b 1
)

python -c "import streamlit, pandas" >nul 2>&1
if errorlevel 1 (
    echo Installing dashboard dependencies...
    pip install -r requirements.txt
)

python app.py
