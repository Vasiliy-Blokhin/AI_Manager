# Запуск плагина «Локальные ИИ — делегирование» на другом компьютере

Плагин делегирует текстовые задачи локальным моделям AI Manager через его публичный
адрес **https://iox-ai-manager.duckdns.org:8443**. На другом компьютере нужны: Kimi Work,
Python 3.10+ и пароль от AI Manager.

Готовый архив для переноса: `local-ai-delegate-plugin.zip` (в корне этого репозитория).
Мастер-копия плагина: `C:\Users\vasiliy\AppData\Roaming\kimi-desktop\daimon-share\plugin-sources\personal\local-ai-delegate\`.

## Шаг 1. Python

Установите Python 3.10+ с https://www.python.org/downloads/ — при установке
отметьте **«Add python.exe to PATH»**. Проверка в новом окне терминала:

```bat
python --version
```

## Шаг 2. Перенести плагин

Распакуйте `local-ai-delegate-plugin.zip` в любую папку (например
`D:\plugins\local-ai-delegate`). Внутри должен быть `kimi.plugin.json`.

## Шаг 3. Зарегистрировать в Kimi Work этого компьютера

В командной строке (cmd), подставив путь к распакованной папке:

```bat
"%APPDATA%\kimi-desktop\daimon-bundle\bin\kimi-daimon.cmd" kimi-plugin register-personal "D:\plugins\local-ai-delegate" --share-dir "%APPDATA%\kimi-desktop\daimon-share" --json
```

В выводе должно быть `"ok": true`. После этого в клиенте Kimi Work откройте
страницу плагинов → вкладка **«Персональные» (Personal)** → нажмите **＋** на плитке
«Локальные ИИ — делегирование», чтобы установить. Перезапуск не нужен.

## Шаг 4. Настроить подключение

Создайте файл `%LOCALAPPDATA%\local-ai-delegate\config.json`, то есть
`C:\Users\<имя>\AppData\Local\local-ai-delegate\config.json`:

```json
{
  "url": "https://iox-ai-manager.duckdns.org:8443",
  "password": "пароль AIM_PASSWORD из .env AI Manager"
}
```

## Шаг 5. Проверка

Спросите агента: **«Проверь статус локальных ИИ»** — он вызовет `local_llm_status()`.
Должно быть «AI Manager: доступен» и список моделей. Затем можно просить делегировать
задачи: «сделай сводку этого лога через локальную модель» и т.п.

## Что важно знать

- Если этот компьютер находится в той же локальной сети, что и сервер, в `url` можно
  указать `http://192.168.0.23:8000` — будет быстрее и не зависит от интернета.
- Лимиты очереди и модели настраиваются на сервере (см. `README.md` AI Manager).
- Пароль хранится только в `config.json` на каждой машине; в архив плагина он не входит.
