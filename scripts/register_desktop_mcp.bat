@echo off
rem Adds the delegation MCP server to Claude Desktop (chat). ASCII only: cmd mangles UTF-8 in .bat.
rem Run from Explorer, NOT from a terminal inside Claude. Waits until Claude is fully closed,
rem writes the config (scripts\register_desktop_mcp.py) and starts Claude again.
cd /d "%~dp0.."

echo Quit Claude completely: tray icon (bottom right) - Quit.
echo Waiting for Claude to close...
:wait
powershell -NoProfile -Command "if (Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*WindowsApps*' -or $_.Path -like '*AnthropicClaude*' }) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto wait
)
rem Claude rewrites its config on exit - let it finish
timeout /t 3 /nobreak >nul

"%~dp0..\venv\Scripts\python.exe" "%~dp0register_desktop_mcp.py"
if errorlevel 1 (
    pause
    exit /b 1
)

echo Starting Claude...
start "" explorer.exe "shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude"
echo Done: ai-manager-delegate should appear in Settings - Developer.
pause
