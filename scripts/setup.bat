@echo off
chcp 65001 >nul
REM ==== Установка AI Manager (Windows) ====
setlocal
cd /d "%~dp0.."

echo [1/4] Создание виртуального окружения...
if not exist venv (
    py -3.11 -m venv venv 2>nul || python -m venv venv
)

echo [2/4] Установка Python-зависимостей...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/4] Настройка .env...
if not exist .env (
    copy .env.example .env >nul
    echo     Создан .env — ОБЯЗАТЕЛЬНО задайте пароль AIM_PASSWORD!
)

echo [4/4] Скачивание llama-server (Vulkan, для Intel Arc)...
where llama-server >nul 2>nul
if errorlevel 1 (
    powershell -ExecutionPolicy Bypass -File scripts\download_llama_server.ps1
) else (
    echo     llama-server уже в PATH, пропуск.
)

echo.
echo ==== Готово ====
echo Задайте пароль в .env, затем запустите: scripts\run.bat
pause
