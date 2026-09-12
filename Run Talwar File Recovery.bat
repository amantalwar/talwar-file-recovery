@echo off
rem Launches Talwar File Recovery without a console window.
rem Requires Python 3.10+ from python.org (which installs the "py" launcher).
where pyw >nul 2>&1
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0talwar_file_recovery.py"
) else (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
)
