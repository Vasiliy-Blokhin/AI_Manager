@echo off
chcp 65001 >nul
REM ==== Перезапуск AI Manager (только свой процесс, не трогая другие Python) ====
cd /d "%~dp0.."

REM Процесс uvicorn этого проекта: app.main:app + путь проекта (--app-dir) в командной строке.
REM wmic в новых Windows удалён — ищем через PowerShell (CIM).
powershell -NoProfile -Command "$root=(Get-Location).Path; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*app.main:app*' -and $_.CommandLine -like \"*$root*\" } | ForEach-Object { Write-Host ('Найден AI Manager PID=' + $_.ProcessId + ', останавливаем...'); taskkill /T /F /PID $_.ProcessId | Out-Null }"

echo Если сервис запущен через run.bat (автозапуск), он поднимется сам через 5 секунд.
echo Иначе запустите scripts\run.bat.
