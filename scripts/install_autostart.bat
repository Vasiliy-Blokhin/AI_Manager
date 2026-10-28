@echo off
chcp 65001 >nul
REM ==== Автозапуск AI Manager при входе в Windows ====
REM Требуются права администратора (запустите от имени администратора)
set "TASK=AI-Manager"
set "SCRIPT=%~dp0run.bat"
schtasks /Create /TN "%TASK%" /TR "\"%SCRIPT%\"" /SC ONLOGON /RL HIGHEST /F
REM schtasks по умолчанию останавливает задачу через 72 ч (22.10.2026 так остановился сервис) — снимаем лимит
powershell -NoProfile -Command "$t=Get-ScheduledTask -TaskName '%TASK%'; $t.Settings.ExecutionTimeLimit='PT0S'; $t.Settings.StopIfGoingOnBatteries=$false; $t.Settings.DisallowStartIfOnBatteries=$false; Set-ScheduledTask -InputObject $t | Out-Null"
if errorlevel 1 ( echo Ошибка. Запустите скрипт от имени администратора. ) else ( echo Задача "%TASK%" создана — сервис будет стартовать при входе в Windows. )
pause
