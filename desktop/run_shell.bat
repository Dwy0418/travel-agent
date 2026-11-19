@echo off
setlocal
cd /d "%~dp0.."
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0tray_shell.py"
) else (
  python "%~dp0tray_shell.py"
)
