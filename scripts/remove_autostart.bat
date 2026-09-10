@echo off
chcp 65001 >nul
schtasks /Delete /TN "AI-Manager" /F
pause
