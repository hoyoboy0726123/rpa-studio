@echo off
setlocal
cd /d "%~dp0"

set "VENV_DIR=venv"
set "PY=%VENV_DIR%\Scripts\python.exe"
set "SENTINEL=%VENV_DIR%\.setup_complete"

rem ============================================================
rem  Already set up? (sentinel written only after a successful
rem  install) -> skip straight to launching the app.
rem ============================================================
if exist "%SENTINEL%" (
    echo [OK] Environment ready - skipping setup.
    goto run
)

echo [SETUP] First run: creating virtual environment with python + pip...

rem --- need a base Python on PATH to bootstrap the venv ---
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    echo         Install Python 3.10 - 3.13 first, and tick
    echo         "Add Python to PATH" in the installer.
    pause
    exit /b 1
)

if not exist "%PY%" (
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo [SETUP] Upgrading pip...
"%PY%" -m pip install --upgrade pip

echo [SETUP] Installing dependencies from requirements.txt (first run only)...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    echo         Check your internet connection and Python version (3.10 - 3.13).
    pause
    exit /b 1
)

echo [SETUP] Installing Playwright Chromium browser...
"%PY%" -m playwright install chromium

echo done> "%SENTINEL%"
echo [SETUP] Complete.

:run
echo [START] Launching RPA Studio...
"%PY%" main.py
endlocal
