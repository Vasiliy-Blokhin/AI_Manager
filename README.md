# AI Manager Service v2

Сервис для централизованного управления локальными ИИ-моделями на **Intel Arc B580 (12 ГБ)**.
Windows, без Docker — запуск из консоли / автозапуск при старте системы.

## Модели (реестр по умолчанию, все — без цензуры)

| # | Назначение | Модель | Бэкенд | Размер в VRAM |
|---|---|---|---|---|
| 1 | Код (Python, JS) | Qwen2.5-Coder-14B-Instruct-abliterated (Q4_K_M) | llama.cpp (Vulkan/SYCL, 100% GPU) | ~9 ГБ |
| 1a | Код / агент (основная для local_code_worker) | GPT-OSS-20B (MoE, UD-Q4_K_XL) | llama.cpp (Vulkan, авто-подбор GPU/CPU) | ~11 ГБ |
| 2 | Текст (русский) | Qwen2.5-14B_Uncensored_Instruct (Q4_K_M) | llama.cpp (Vulkan/SYCL, 100% GPU) | ~9 ГБ |
| 2a | Текст (русский, контент для auto_posting) | Qwen3.6-35B-A3B (MoE, UD-Q4_K_XL) | llama.cpp (Vulkan, авто-подбор GPU/CPU) | ~12 ГБ + RAM |
| 3 | Графика (текст→изображение) | DreamShaper 8 (SD 1.5) | OpenVINO (GPU) | ~4 ГБ |
| 3a | Графика (обложки, кадры видео, до 1536px) | FLUX.1-schnell INT8 (Apache 2.0) | OpenVINO (GPU) | ~12 ГБ |
| 4 | Речь (TTS, ru/en, клонирование голоса) | Qwen3-TTS 1.7B (Apache 2.0) | llama.cpp `llama-tts` (Vulkan) | ~2 ГБ |

Активна всегда не более одной модели; запуск модели автоматически выгружает остальные.

**Очередь задач.** Каждый запрос к модели — задача в SQLite (`data/ai_manager.db`) с проектом,
статусом и временем. Модель не переключается, пока по ней есть задачи; запросы к другим
моделям ждут своей очереди. Лимиты: ожидание и выполнение по 60 минут (настраивается).
Укажите `model` в запросе — модель запустится сама; заголовок `X-Project` — имя проекта.
Обзор: `GET /api/queue`, история: `GET /api/jobs`. Подробно — `docs/QUEUE.md`, `SPEC.md` (4.5).

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

# синтез речи (активна TTS-модель qwen3-tts); speaker — файл-образец из папки voices/
curl -X POST -H "X-API-Password: ваш_пароль" -H "Content-Type: application/json"      -d '{"text": "Привет! Это проверка.", "language": "ru"}'      localhost:8000/api/tts

# очередь: что выполняется и кто ждёт
curl -H "X-API-Password: ваш_пароль" localhost:8000/api/queue

# выключить всё (409 model_busy, если по модели есть задачи)
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
| `AIM_HF_CACHE` | `~/.cache/ai-manager` | Кэш весов (HF_HOME). На этой машине на C: мало места — укажите папку на D: |
| `AIM_AUTO_INSTALL` | `false` | Автоустановка моделей при старте |
| `AIM_GPU_LAYERS` | `99` | Сколько слоёв выгружать на GPU (`-ngl`), если в реестре не задано |
| `AIM_AUTOSTART` | `true` | Запускать модель из поля `model` запроса `/v1`, перезапускать упавший llama-server |
| `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM` | `1` | Обход выключенного Resizable BAR на Arc (см. ниже) |
| AIM_PUBLIC_HOST | авто (IP LAN) | Адрес сервиса для других устройств (иначе определяется автоматически) |
| `AIM_DB` | `data/ai_manager.db` | БД задач (SQLite) |
| `AIM_JOB_WAIT_TIMEOUT` | `3600` | Сколько задача может ждать в очереди, сек (потом `expired`) |
| `AIM_JOB_RUN_TIMEOUT` | `3600` | Сколько задача может выполняться, сек (потом `timeout`) |
| `AIM_JOB_RETENTION_DAYS` | `14` | Сколько дней хранить историю задач |
| `AIM_MODEL_START_TIMEOUT` | `1200` | Срок запуска модели, сек (потом `timeout`) |
| `AIM_STUCK_GRACE` | `300` | Поток задачи не завершился через столько сек после таймаута → перезапуск сервиса |
| `AIM_IMAGE_STALL_TIMEOUT` | `600` | Шаг генерации картинки дольше стольких сек → перезапуск (`0` — выкл.) |
| `AIM_STICKY_SEC` | `20` | После задачи модель держится столько сек, чтобы следующий шаг агента не ждал чужую модель (`0` — выкл.) |
| `AIM_STICKY_MAX_SEC` | `180` | Удержание не даётся, если задача к другой модели ждёт дольше (защита от голодания) |

## Производительность на Arc B580: Resizable BAR

На этой машине **Resizable BAR выключен** (BAR-окно видеокарты 256 МБ вместо ~12 ГБ).
Intel Arc без ReBAR работает в разы медленнее: prefill gpt-oss-20b падал до ~36 ток/с.
Программный обход — `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1` (в `.env` и в реестре
gpt-oss-20b): prefill 800–1700 ток/с, генерация 28–34 ток/с.

Лучшее решение — включить в BIOS **Above 4G Decoding** и **Re-Size BAR Support**
(для Ryzen 5000 нужна свежая прошивка платы; Windows должна быть установлена в режиме UEFI).
Проверка: Intel Graphics Software → «Resizable BAR: Enabled». После включения обход
можно оставить или убрать — сравните скорость.

Реестр gpt-oss-20b настроен на `ngl: null` (llama-server сам решает, сколько
MoE-экспертов держать на CPU, чтобы всё влезло в VRAM), 32k контекста, 1 слот — см. `SPEC.md`.

## Графические бэкенды для Arc B580

- **llama.cpp Vulkan** (по умолчанию, скрипт `download_llama_server.ps1` качает
  сборку `win-vulkan-x64`) — не требует дополнительных рантаймов.
- **llama.cpp SYCL** (быстрее на Arc) — скачайте сборку `win-sycl-x64` и установите
  Intel oneAPI Base Toolkit; укажите путь в `LLAMA_SERVER_BIN`.
- Графическая модель работает через **OpenVINO** (`optimum-intel`), устройство GPU
  выбирается автоматически (Arc).

## Агент для кода в браузере — local_code_worker

Соседний проект `local_code_worker` — веб-агент по мотивам Claude Code поверх этого
сервиса (модель gpt-oss-20b, запускается автоматически). См. его README.

## Работа из VS Code (Continue) — правки по всему репозиторию

Сервис предоставляет OpenAI-совместимый endpoint `/v1` — его используют
расширения Continue, Cline, Roo Code и др. Пошаговая инструкция (установка
Continue, первый запуск, режим Agent для правок по всему репозиторию) —
в **`docs/CONTINUE.md`**.

Кратко:
1. Установите Continue в VS Code.
2. В `~/.continue/config.yaml` добавьте модель: `provider: openai`,
   `apiBase: http://&lt;IP_сервера&gt;:8000/v1`, `apiKey: пароль из .env`,
   `model: qwen25-coder-14b-unc` (имя из реестра).
3. Откройте папку проекта, чат — `Ctrl+L`, инлайн-правки — `Ctrl+I`,
   правки сразу в нескольких файлах — режим **Agent**.

## Доступ с других устройств локальной сети

По умолчанию сервис слушает все интерфейсы (`AIM_HOST=0.0.0.0`), поэтому с
другого компьютера/ноутбука в той же сети доступны веб-интерфейс, API и
endpoint `/v1` для Continue.

1. На сервере **один раз** запустите от имени администратора
   `scripts\open_firewall.bat` — создаст правило брандмауэра и покажет
   адреса сервера (например `http://192.168.1.50:8000`).
2. С другого устройства открывайте этот адрес; пароль — из `.env`.
3. Внутренний llama-server по-прежнему слушает только `127.0.0.1`: наружу
   смотрит только AI Manager — единая точка авторизации и контроля.

## Структура

```
ai-manager/
├── README.md
├── SPEC.md
├── requirements.txt
├── .env.example
├── config/
│   └── models.registry.json
├── docs/
│   └── CONTINUE.md
├── scripts/
│   ├── setup.bat
│   ├── run.bat
│   ├── open_firewall.bat
│   └── (автозапуск, скачивание llama-server)
└── app/
    ├── main.py                 # FastAPI: веб + REST API + middleware пароля
    ├── manager.py              # жизненный цикл, контроль «одна активная модель»
    ├── registry.py
    ├── schemas.py
    ├── openai_proxy.py
    ├── backends/
    │   ├── base.py
    │   ├── llama_server.py     # GGUF+GPU
    │   ├── llama_tts.py        # текст→речь (Qwen3-TTS через llama-tts)
    │   └── openvino_sd.py      # текст→картинка (SD/SDXL/FLUX)
    └── static/
        └── index.html
```

Подробная спецификация API и форматов — в `SPEC.md`.
