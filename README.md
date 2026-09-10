# AI Manager Service v2

Сервис для централизованного управления локальными ИИ-моделями на **Intel Arc B580 (12 ГБ)**.
Windows, без Docker — запуск из консоли / автозапуск при старте системы.

## Модели (реестр по умолчанию, все — без цензуры)

| # | Назначение | Модель | Бэкенд | Размер в VRAM |
|---|---|---|---|---|
| 1 | Код (Python, JS) | Qwen2.5-Coder-14B-Instruct-abliterated (Q4_K_M) | llama.cpp (Vulkan/SYCL, 100% GPU) | ~9 ГБ |
| 2 | Текст (русский) | Qwen2.5-14B_Uncensored_Instruct (Q4_K_M) | llama.cpp (Vulkan/SYCL, 100% GPU) | ~9 ГБ |
| 3 | Графика (текст→изображение) | DreamShaper 8 (SD 1.5) | OpenVINO (GPU) | ~4 ГБ |

Активна всегда не более одной модели; запуск модели автоматически выгружает остальные.

## Установка (Windows)

1. Установите **Python 3.11+** (при установке отметьте «Add to PATH»)
   и свежий драйвер Intel Arc.
2. Распакуйте архив, откройте консоль в папке проекта и выполните:

```bat
scripts\setup.bat
```

Скрипт: создаст виртуальное окружение `venv`, установит зависимости, создаст `.env`,
скачает готовый бинарник `llama-server` (Vulkan — работает на Arc из коробки).

3. Отредактируйте `.env` — обязательно задайте пароль (`AIM_PASSWORD`).

## Запуск

Ручной запуск:

```bat
scripts\run.bat
```

Сервис будет доступен по адресу `http://127.0.0.1:8000` (веб-интерфейс),
документация API — `/docs`. Скрипт сам перезапускает сервис при падении.

## Автозапуск при старте Windows

```bat
scripts\install_autostart.bat
```

Создаёт задачу планировщика «AI-Manager», запускающую `run.bat` при входе
пользователя (с правами администратора). Удалить: `scripts\remove_autostart.bat`.

## Пароль

Пароль задаётся в `.env` (`AIM_PASSWORD=...`). Все запросы к `/api/*` требуют
заголовок `X-API-Password`. Веб-интерфейс спросит пароль при первом открытии.

## Быстрый старт (API)

```bash
# список моделей со статусами
curl -H "X-API-Password: ваш_пароль" localhost:8000/api/models

# скачать и установить модель (первый раз — долго, ~9 ГБ)
curl -X POST -H "X-API-Password: ваш_пароль" -H "Content-Type: application/json" \
     -d '{"name": "qwen25-coder-14b-unc"}' localhost:8000/api/models/install

# запустить (остальные выключатся автоматически)
curl -X POST -H "X-API-Password: ваш_пароль" localhost:8000/api/models/qwen25-coder-14b-unc/start

# текстовый запрос (единый формат для всех текстовых моделей)
curl -X POST -H "X-API-Password: ваш_пароль" -H "Content-Type: application/json" \
     -d '{"messages":[{"role":"user","content":"Напиши на Python быструю сортировку"}]}' \
     localhost:8000/api/chat

# генерация изображения (активна графическая модель)
curl -X POST -H "X-API-Password: ваш_пароль" -H "Content-Type: application/json" \
     -d '{"prompt": "закат над горами, фотореализм", "steps": 25}' \
     localhost:8000/api/image

# выключить всё
curl -X POST -H "X-API-Password: ваш_пароль" localhost:8000/api/models/stop
```

## Переменные окружения (.env)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AIM_PASSWORD` | — (обяз.) | Пароль доступа к API |
| `AIM_HOST` / `AIM_PORT` | `127.0.0.1` / `8000` | Адрес сервиса |
| `AIM_REGISTRY` | `config/models.registry.json` | Реестр моделей |
| `LLAMA_SERVER_BIN` | `llama-server` | Путь к llama-server (Vulkan/SYCL для Arc) |
| `AIM_PORT_RANGE` | `8100-8199` | Порты для llama-server |
| `AIM_HF_CACHE` | `~/.cache/ai-manager` | Кэш весов (HF_HOME) |
| `AIM_AUTO_INSTALL` | `false` | Автоустановка моделей при старте |
| `AIM_GPU_LAYERS` | `99` | Сколько слоёв выгружать на GPU (`-ngl`) |

## Графические бэкенды для Arc B580

- **llama.cpp Vulkan** (по умолчанию, скрипт `download_llama_server.ps1` качает
  сборку `win-vulkan-x64`) — не требует дополнительных рантаймов.
- **llama.cpp SYCL** (быстрее на Arc) — скачайте сборку `win-sycl-x64` и установите
  Intel oneAPI Base Toolkit; укажите путь в `LLAMA_SERVER_BIN`.
- Графическая модель работает через **OpenVINO** (`optimum-intel`), устройство GPU
  выбирается автоматически (Arc).

## Структура

```
ai-manager/
├── README.md, SPEC.md, requirements.txt, .env.example
├── config/models.registry.json
├── scripts/          # setup.bat, run.bat, автозапуск, скачивание llama-server
└── app/
    ├── main.py       # FastAPI: веб + REST API + middleware пароля
    ├── manager.py    # жизненный цикл, контроль «одна активная модель»
    ├── registry.py, schemas.py
    ├── backends/     # base.py, llama_server.py (GGUF+GPU), openvino_sd.py (текст→картинка)
    └── static/index.html
```

Подробная спецификация API и форматов — в `SPEC.md`.
