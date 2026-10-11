# AI Manager Service v2

Сервис для централизованного управления локальными ИИ-моделями на **Intel Arc B580 (12 ГБ)**.
Windows, без Docker — запуск из консоли / автозапуск при старте системы.

## Модели (реестр по умолчанию, все — без цензуры)

| # | Назначение | Модель | Бэкенд | Размер в VRAM |
|---|---|---|---|---|
| 1 | Код (Python, JS) | Qwen2.5-Coder-14B-Instruct-abliterated (Q4_K_M) | llama.cpp (Vulkan/SYCL, 100% GPU) | ~9 ГБ |
| 1a | Код / агент (основная для local_code_worker) | GPT-OSS-20B (MoE, UD-Q4_K_XL) | llama.cpp (Vulkan, авто-подбор GPU/CPU) | ~11 ГБ |
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
| `AIM_GPU_LAYERS` | `99` | Сколько слоёв выгружать на GPU (`-ngl`), если в реестре не задано |
| `AIM_AUTOSTART` | `true` | Запускать модель из поля `model` запроса `/v1`, перезапускать упавший llama-server |
| `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM` | `1` | Обход выключенного Resizable BAR на Arc (см. ниже) |
| AIM_PUBLIC_HOST | авто (IP LAN) | Адрес сервиса для других устройств (иначе определяется автоматически) |

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
    │   └── openvino_sd.py      # текст→картинка
    └── static/
        └── index.html
```

Подробная спецификация API и форматов — в `SPEC.md`.
