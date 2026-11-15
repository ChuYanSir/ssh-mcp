@echo off
rem Double-click to open the ssh-mcp desktop app (no console window).
rem Default config = ..\servers.yaml (project root); it is created on first run.
rem Pass your own config path to override:  start-desktop.cmd D:\path\servers.yaml
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" (
  echo venv not found at .venv\Scripts\pythonw.exe
  echo Create it first:  python -m venv .venv  ^&^&  .venv\Scripts\pip install -e .
  pause
  exit /b 1
)
if "%~1"=="" (
  start "" ".venv\Scripts\pythonw.exe" -m ssh_mcp.desktop --config "%~dp0..\servers.yaml"
) else (
  start "" ".venv\Scripts\pythonw.exe" -m ssh_mcp.desktop %*
)
