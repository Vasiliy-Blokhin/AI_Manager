@echo off
rem Автозапуск Caddy (HTTPS-прокси) при входе в Windows.
rem Создаёт задачу планировщика "AI-Manager-Caddy". Запускать от имени администратора.
schtasks /Create /TN "AI-Manager-Caddy" /SC ONLOGON /RL HIGHEST /F ^
  /TR "\"D:\projects\AI_Manager\caddy\run.bat\""
schtasks /Change /TN "AI-Manager-Caddy" /RI 0 2>nul
echo.
echo Задача "AI-Manager-Caddy" создана: Caddy стартует при входе пользователя.
echo Проверить: schtasks /Query /TN "AI-Manager-Caddy"
pause
