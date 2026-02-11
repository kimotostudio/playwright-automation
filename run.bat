@echo off
REM Spiritual Salon Automation - Daily Runner (Windows)
REM Usage: run.bat [--test]

cd /d "%~dp0"

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python not found. Install Python 3.10+
    exit /b 1
)

REM Check dependencies
python -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
    playwright install chromium
)

REM Test mode
if "%1"=="--test" (
    echo Running in TEST MODE (limit: 2)
    python -c "import json; f=open('config/settings.json','r'); s=json.load(f); f.close(); s['test_mode']=True; f=open('config/settings.json','w'); json.dump(s,f,indent=2); f.close()"
)

REM Run
python src/main.py

REM Reset test mode
if "%1"=="--test" (
    python -c "import json; f=open('config/settings.json','r'); s=json.load(f); f.close(); s['test_mode']=False; f=open('config/settings.json','w'); json.dump(s,f,indent=2); f.close()"
)

pause
