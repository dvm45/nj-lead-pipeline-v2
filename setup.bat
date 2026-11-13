@echo off
echo ============================================
echo   NJ Lead Pipeline - Setup
echo ============================================
echo.

REM Check Python is installed and version is 3.11+
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download it from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Found Python %PYVER%

REM Create virtual environment if it doesn't exist
if not exist .venv (
    echo.
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM Activate and install
echo.
echo Installing dependencies...
call .venv\Scripts\activate.bat
pip install --upgrade pip >nul 2>&1
pip install -e ".[dev]"
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

REM Copy .env.example if .env doesn't exist
if not exist .env (
    echo.
    echo Creating .env from template...
    copy .env.example .env >nul
    echo IMPORTANT: Edit .env and add your AWS Bedrock API key.
)

REM Download NCES reference data
echo.
echo Downloading school reference data (one-time)...
njlead refresh-reference

echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo Next steps:
echo   1. Edit .env and add your AWS_BEARER_TOKEN_BEDROCK key
echo   2. Place your Google service account JSON at credentials\service-account.json
echo   3. Double-click run.bat to start the pipeline
echo.
pause
