@echo off
rem Открывает входящие TCP 8443 (Caddy/HTTPS) и TCP 8080 (HTTP->HTTPS редирект).
rem Старые правила 80/443 не нужны — убираем их.
rem Запускать ОДИН РАЗ от имени администратора (правой кнопкой -> Запуск от имени администратора).
netsh advfirewall firewall delete rule name="AI-Manager-Caddy-HTTPS" >nul 2>&1
netsh advfirewall firewall delete rule name="AI-Manager-HTTP-ACME" >nul 2>&1
netsh advfirewall firewall add rule name="AI-Manager-Caddy-HTTPS" dir=in action=allow protocol=TCP localport=8443
echo.
netsh advfirewall firewall add rule name="AI-Manager-HTTP" dir=in action=allow protocol=TCP localport=8080
echo.
echo Готово. Правила: AI-Manager-Caddy-HTTPS (8443), AI-Manager-HTTP (8080).
pause
