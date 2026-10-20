@echo off
cd /d "%~dp0"
if exist "dist\SteamSDA\SteamSDA.exe" (
    start "" "dist\SteamSDA\SteamSDA.exe"
    exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "main.py"
    exit /b
)
py -3 -c "import PyQt6, requests, psutil, pyzipper" >nul 2>&1
if errorlevel 1 (
    echo Install dependencies first: py -3 -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" pyw -3 "main.py"
