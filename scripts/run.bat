@echo off
chcp 65001 >nul
REM ==== Запуск AI Manager (с авто-перезапуском при падении) ====
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
echo [%date% %time%] Запуск AI Manager...
python -m uvicorn app.main:app --host %HOST% --port %PORT%
echo [%date% %time%] Сервис остановлен, перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop