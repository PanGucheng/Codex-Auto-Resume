@echo off
setlocal
where.exe pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" /d "%~dp0" pyw.exe -3 "%~dp0main.pyw"
    exit /b
)
where.exe pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" /d "%~dp0" pythonw.exe "%~dp0main.pyw"
    exit /b
)
echo Python 3.10 or later with Tcl/Tk is required.
pause
exit /b 1
