@echo off
echo ============================================
echo   NJ Lead Pipeline - Run
echo ============================================
echo.

REM Check virtual environment exists
if not exist .venv\Scripts\activate.bat (
    echo ERROR: Virtual environment not found.
    echo Run setup.bat first.
    echo.
    pause
    exit /b 1
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Check LLM credentials before starting
echo Checking LLM configuration...
njlead check-llm
if errorlevel 1 (
    echo.
    echo ERROR: LLM credentials are not configured.
    echo Edit .env and add your API key, then try again.
    echo.
    pause
    exit /b 1
)

echo.
echo Starting pipeline...
echo.
njlead run

echo.
echo ============================================
echo   Run complete. Check the output above.
echo ============================================
echo.
pause
