@echo off
chcp 65001 >nul
REM ==== Перезапуск AI Manager (только свой процесс, не трогая другие Python) ====
cd /d "%~dp0.."

REM Находим PID процесса uvicorn app.main:app по имени окна
for /f "tokens=2" %%P in ('tasklist /fi "imagename eq python.exe" /fo list ^| findstr /i "PID:"') do (
    REM Проверяем командную строку каждого python-процесса
    for /f "tokens=*" %%C in ('wmic process where "processid=%%P" get commandline /value 2^>nul ^| findstr /i "app.main:app"') do (
        echo Найден AI Manager PID=%%P, останавливаем...
        taskkill /PID %%P /F
    )
)

echo Перезапуск через run.bat...
call scripts\run.bat