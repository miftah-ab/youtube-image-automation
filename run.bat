@echo off
setlocal enabledelayedexpansion

echo ====================================================================
echo             YOUTUBE IMAGE AUTOMATION SYSTEM - WINDOWS LAUNCHER
echo ====================================================================
echo.

cd /d "%~dp0"

:: Check for Python
set "PY_EXE="
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PY_EXE=python"
) else (
    where py >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        set "PY_EXE=py"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else if exist "%ProgramFiles%\Python311\python.exe" (
        set "PY_EXE=%ProgramFiles%\Python311\python.exe"
    )
)

if "%PY_EXE%"=="" (
    echo [ERROR] Python was not found on your system.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [INFO] Using Python: %PY_EXE%
%PY_EXE% --version
echo.

:: Setup Virtual Environment
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment (.venv)...
    %PY_EXE% -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PIP=.venv\Scripts\pip.exe"
set "VENV_STREAMLIT=.venv\Scripts\streamlit.exe"

:: Check and copy .env if missing
if not exist ".env" (
    if exist ".env.example" (
        echo [INFO] Creating initial .env from .env.example...
        copy /y ".env.example" ".env" >nul
        echo [NOTICE] Created .env file. Please edit .env to provide your NOTION_API_KEY.
    )
)

:: Install dependencies
if not exist ".venv\Lib\site-packages\streamlit" (
    echo [INFO] Installing required dependencies from requirements.txt...
    "%VENV_PIP%" install --upgrade pip
    "%VENV_PIP%" install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
    echo [INFO] Installing Playwright Chromium browser...
    .venv\Scripts\playwright.exe install chromium
)

:: Verify Playwright Chromium
if not exist "%LOCALAPPDATA%\ms-playwright" (
    echo [INFO] Ensuring Playwright Chromium is installed...
    .venv\Scripts\playwright.exe install chromium
)

echo.
echo [INFO] Starting Local Dashboard on http://127.0.0.1:8501 ...
echo [INFO] Press Ctrl+C in this console window to shut down the server.
echo.

"%VENV_STREAMLIT%" run app.py --server.address=127.0.0.1 --server.port=8501

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Application exited with error code %ERRORLEVEL%.
    pause
)

endlocal
