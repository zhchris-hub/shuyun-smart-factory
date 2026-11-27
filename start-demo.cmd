@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Create the Python environment using the instructions in README.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py --demo
if errorlevel 1 pause
