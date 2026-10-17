@echo off
chcp 65001 >nul
REM ==== Запуск AI Manager (с авто-перезапуском при падении и зависании) ====
cd /d "%~dp0.."
if not exist .env ( echo Нет файла .env — сначала выполните scripts\setup.bat & pause & exit /b 1 )
call venv\Scripts\activate.bat

set "HOST=0.0.0.0"
set "PORT=8000"
for /f "usebackq eol=# tokens=1,2 delims==" %%A in (".env") do (
    if /I "%%A"=="AIM_HOST" set "HOST=%%B"
    if /I "%%A"=="AIM_PORT" set "PORT=%%B"
)
echo Сервис слушает %HOST%:%PORT%

:loop
REM Внешний сторож: убивает процесс, если /health не отвечает (один экземпляр)
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0watchdog.ps1" -Port %PORT%
echo [%date% %time%] Запуск AI Manager...
REM --app-dir с путём проекта — по нему сторож и restart.bat находят именно этот процесс
python -m uvicorn app.main:app --app-dir "%CD%" --host %HOST% --port %PORT%
echo [%date% %time%] Сервис остановлен (код %errorlevel%), перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
