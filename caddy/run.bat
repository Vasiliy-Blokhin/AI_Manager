@echo off
chcp 65001 >nul
REM ==== Запуск Caddy (HTTPS-прокси AI Manager) с авто-перезапуском ====
REM Токен DuckDNS для DNS-01 challenge читается из duckdns.token (рядом с этим файлом).
cd /d "%~dp0"

set "DUCKDNS_TOKEN="
if exist duckdns.token (
    for /f "usebackq delims=" %%A in ("duckdns.token") do set "DUCKDNS_TOKEN=%%A"
)
if not defined DUCKDNS_TOKEN (
    echo [WARN] duckdns.token не найден — сертификат Let's Encrypt не выпустится.
    echo        Создайте файл duckdns.token с токеном API DuckDNS.
)

:loop
echo [%date% %time%] Запуск Caddy...
"duckdns_build.exe" run --config Caddyfile --adapter caddyfile
echo [%date% %time%] Caddy остановлен (код %errorlevel%), перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
