@echo off
setlocal
set PYTHONUTF8=1
set "EXPORT_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
if exist "%EXPORT_PYTHON%" (
    start "" "%EXPORT_PYTHON%" "%~dp0export_chat.py"
    exit /b
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0export_chat.py"
    exit /b
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0export_chat.py"
    exit /b
)
echo Python with Tkinter was not found.
echo Install Python 3.10 or newer, then try again.
pause
