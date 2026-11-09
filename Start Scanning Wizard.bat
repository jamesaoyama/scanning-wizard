@echo off
rem Starts Scanning Wizard without a black console window.
cd /d "%~dp0"
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "scanning_wizard.py"
    goto :eof
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "scanning_wizard.py"
    goto :eof
)
echo Python isn't installed on this PC.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then double-click this file again.
pause
