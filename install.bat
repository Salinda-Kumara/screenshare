@echo off
title LAN Screen Share - Installer
echo ============================================
echo   LAN Screen Share - Windows Installer
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Download from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] Installing LAN Screen Share...
echo.
pip install -e "%~dp0" --quiet
if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed. Trying with --user flag...
    pip install -e "%~dp0" --user --quiet
    if errorlevel 1 (
        echo [ERROR] Installation failed completely.
        pause
        exit /b 1
    )
)

echo [2/3] Verifying installation...
python -c "from config import SCREEN_PORT; from gui_app import App; print('  OK')"
if errorlevel 1 (
    echo [ERROR] Verification failed.
    pause
    exit /b 1
)

echo [3/3] Creating Desktop shortcut...
set SCRIPT_DIR=%~dp0
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'LAN Screen Share.lnk')); $s.TargetPath = 'pythonw'; $s.Arguments = '-c \"from gui_app import main; main()\"'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.Description = 'LAN Screen Share'; $s.Save()"

echo.
echo ============================================
echo   Installation complete!
echo.
echo   You can now run the app by:
echo     1. Double-click "LAN Screen Share" on Desktop
echo     2. Run: lanshare
echo     3. Run: python gui_app.py
echo ============================================
echo.
pause
