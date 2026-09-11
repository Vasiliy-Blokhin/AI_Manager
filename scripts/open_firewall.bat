@echo off
chcp 65001 >nul
REM ==== Открытие порта AI Manager в брандмауэре Windows + показ адресов LAN ====
REM Запустите ОТ ИМЕНИ АДМИНИСТРАТОРА (один раз).
setlocal
cd /d "%~dp0.."

set "PORT=8000"
for /f "usebackq eol=# tokens=1,2 delims==" %%A in (".env") do (
    if /I "%%A"=="AIM_PORT" set "PORT=%%B"
)

netsh advfirewall firewall show rule name="AI-Manager" >nul 2>nul
if %errorlevel%==0 goto :show

netsh advfirewall firewall add rule name="AI-Manager" dir=in action=allow protocol=TCP localport=%PORT%
if errorlevel 1 ( echo Не удалось создать правило — запустите от имени администратора. & pause & exit /b 1 )
echo Создано правило брандмауэра "AI-Manager" (входящий TCP порт %PORT%).

:show
echo.
echo Адреса этого компьютера в локальной сети (используйте в настройках Continue):
for /f "tokens=2 delims=: " %%I in ('ipconfig ^| findstr /R /C:"IPv4"') do echo     http://%%I:%PORT%
echo.
pause