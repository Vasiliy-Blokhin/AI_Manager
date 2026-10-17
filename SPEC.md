# Спецификация AI Manager Service v2

## 1. Назначение

Сервис предоставляет единую точку входа для работы с тремя локальными
ИИ-моделями на одной видеокарте Intel Arc B580 (12 ГБ): проверка наличия,
установка, запуск/остановка, стандартизированные запросы. Windows, без Docker.

Инварианты:

1. **I1.** Активна не более одной модели. Запуск модели автоматически останавливает
   остальные (текстовые выгружаются из VRAM процессом `stop`, графическая —
   освобождением пайплайна). Допустимо состояние «все выключены».
2. **I2.** Все текстовые модели доступны через единый формат `POST /api/chat`,
   графическая — через `POST /api/image`, независимо от бэкенда.
3. **I3.** Запрос к `/api/chat` или `/api/image` при отсутствии активной модели
   возвращает `409 Conflict`; обращение к чужому типу эндпоинта (`/api/chat` при
   активной графической модели и наоборот) — `409` с кодом `wrong_model_type`.
4. **I4.** Любой запрос к `/api/*` без верного пароля отклоняется с `401`
   до обработки эндпоинта (middleware).
5. **I5.** Каждый запрос к модели (`/api/chat`, `/api/image`, `/api/tts`,
   `/v1/chat/completions`, `POST /api/jobs`) — задача в очереди и запись в БД
   (`data/ai_manager.db`). Подробно — раздел 4.5.
6. **I6.** Модель не переключается и не останавливается, пока по ней есть
   выполняющиеся или ожидающие задачи. Ручной `start`/`stop` в этом случае — `409 model_busy`.

## 2. Реестр моделей

Файл `config/models.registry.json`:

```json
{
  "models": [
    {
      "name": "qwen25-coder-14b-unc",
      "backend": "llama_server",
      "model_ref": "bartowski/Qwen2.5-Coder-14B-Instruct-abliterated-GGUF:Qwen2.5-Coder-14B-Instruct-abliterated-Q4_K_M.gguf",
      "display_name": "Qwen2.5-Coder-14B (unc)",
      "description": "Код: Python/JS, без цензуры (abliterated)",
      "ngl": 99, "context": 16384
    },
    {
      "name": "qwen25-14b-unc-russian",
      "backend": "llama_server",
      "model_ref": "bartowski/Qwen2.5-14B_Uncensored_Instruct-GGUF:Qwen2.5-14B_Uncensored_Instruct-Q4_K_M.gguf",
      "display_name": "Qwen2.5-14B Uncensored",
      "description": "Текст, в т.ч. русский, без цензуры",
      "ngl": 99, "context": 16384
    },
    {
      "name": "dreamshaper-8",
      "backend": "openvino_sd",
      "model_ref": "Lykon/dreamshaper-8",
      "display_name": "DreamShaper 8",
      "description": "Генерация изображений по запросу (SD 1.5)"
    }
  ]
}
```

| Поле | Обяз. | Описание |
|---|---|---|
| `name` | да | Уникальный идентификатор модели в сервисе |
| `backend` | да | `llama_server` (текст, GGUF), `openvino_sd` (картинки) или `llama_tts` (речь) |
| `model_ref` | да | llama_server: `repo:file` на HuggingFace; openvino_sd: `repo_id` |
| `ngl` | нет | Число слоёв на GPU (`-ngl`, по умолчанию `AIM_GPU_LAYERS`=99). `null` — не передавать, llama-server сам подгонит выгрузку под свободную VRAM (`--fit`) |
| `context` | нет | Размер контекста (`-c`, по умолчанию 16384) |
| `parallel` | нет | Число слотов (`-np`, по умолчанию 1: весь контекст и кэш промпта — одному пользователю) |
| `n_cpu_moe` | нет | MoE-эксперты первых N слоёв держать на CPU (`--n-cpu-moe`) |
| `flash_attn` | нет | `on` / `off` / `auto` (`-fa`) |
| `args` | нет | Дополнительные аргументы llama-server (список строк) |
| `env` | нет | Переменные окружения процесса llama-server (поверх `.env`) |
| `mmproj` | llama_tts | Файл аудио-кодека в том же репозитории (Qwen3-TTS) |
| `default_steps` / `default_guidance` / `max_side` | нет | openvino_sd: значения по умолчанию модели, ограничение стороны (1536) |
| `request_defaults` | нет | Параметры, подставляемые в каждый запрос `/v1/chat/completions` и `/api/chat`, если клиент их не задал (например `reasoning_effort`) |

Пример оптимизированной записи (gpt-oss-20b на Arc B580 без Resizable BAR):

```json
{
  "name": "gpt-oss-20b", "backend": "llama_server",
  "model_ref": "unsloth/gpt-oss-20b-GGUF:gpt-oss-20b-UD-Q4_K_XL.gguf",
  "ngl": null, "context": 32768, "parallel": 1, "flash_attn": "on",
  "args": ["-ub", "2048", "-b", "2048", "--load-mode", "none", "--jinja"],
  "env": {"GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM": "1"},
  "request_defaults": {"reasoning_effort": "low"}
}
```

Замеры на этой машине (Ryzen 5 5500, Arc B580, Vulkan; промпт 24.5k токенов):

| Конфигурация | Prefill, ток/с | Генерация, ток/с |
|---|---|---|
| Было: `-ngl 99 -c 100000`, 4 слота | 30–36 | 1–4 (VRAM переполнена) |
| `--fit`, 32k, 1 слот | 36 | 23 |
| + `GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1`, `-ub 2048` | **810** (до 1680 на коротком контексте) | **28–34** |

## 3. Авторизация

- Пароль задаётся в `.env` (`AIM_PASSWORD`), загружается через `python-dotenv`.
- Каждый запрос к `/api/*` должен содержать заголовок `X-API-Password: <пароль>`.
- **Также принимается** заголовок `Authorization: Bearer <пароль>` (используется IDE-расширениями).
- Проверка распространяется на эндпоинты `/v1/*`.
- Проверка — постоянное сравнение `hmac.compare_digest`, без утечек по времени.
- Неверный/отсутствующий пароль → `401 {"error":{"code":"unauthorized",...}}`.
- Веб-интерфейс хранит пароль в `localStorage` и отправляет его в заголовке;
  при `401` показывает форму ввода повторно.

## 4. REST API

Ошибки — единый формат: `{"error": {"code": "...", "message": "..."}}`.

### 4.1 Модели

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/models` | Список моделей со статусами |
| GET | `/api/models/{name}` | Одна модель со статусом |
| POST | `/api/models/install` | Проверить наличие; при отсутствии скачать и установить |
| POST | `/api/models/{name}/start` | Запустить модель (остановив остальные) |
| POST | `/api/models/stop` | Остановить активную модель (все выключены) |
| GET | `/api/status` | Краткий статус сервиса |

#### GET /api/models — ответ 200

```json
{
  "active": "qwen25-coder-14b-unc",
  "models": [
    {
      "name": "qwen25-coder-14b-unc",
      "backend": "llama_server",
      "display_name": "Qwen2.5-Coder-14B (unc)",
      "type": "text",
      "installed": true,
      "running": true,
      "pid": 12345,
      "endpoint": "http://127.0.0.1:8100"
    }
  ]
}
```

`active` = `null`, если все выключены. `type` — `text` или `image`.

#### POST /api/models/install — запрос `{"name": "..."}`

Ответ 200: `{"name": "...", "installed": true, "was_installed": false}`.
Операция синхронная (для ~9 ГБ моделей — десятки минут, держите таймаут запроса).

#### POST /api/models/{name}/start — ответ 200

```json
{"name": "qwen25-coder-14b-unc", "running": true, "stopped": ["qwen25-14b-unc-russian"]}
```

Ошибки: `404` не в реестре, `409` не установлена, `409 model_busy` — по активной
модели есть задачи (I6), `502` бэкенд не смог запуститься. Если запрошенная модель
уже активна и занята задачами — `200` без перезапуска.

#### POST /api/models/stop — ответ 200: `{"running": null}`

`409 model_busy`, если по активной модели есть задачи.

### 4.2 Текст: POST /api/chat

```json
{
  "messages": [
    {"role": "system", "content": "Ты — помощник."},
    {"role": "user", "content": "Привет!"}
  ],
  "temperature": 0.7,
  "max_tokens": 512,
  "stream": false
}
```

`messages` обязателен; `stream` только `false`. Ответ 200:

```json
{
  "model": "qwen25-coder-14b-unc",
  "backend": "llama_server",
  "content": "Привет! Чем помочь?",
  "usage": {"prompt_tokens": 25, "completion_tokens": 8, "total_tokens": 33}
}
```

Ошибки: `401` пароль, `409` нет активной модели / активна графическая,
`502` ошибка вывода.

### 4.3 Графика: POST /api/image

```json
{
  "prompt": "закат над горами, фотореализм",
  "negative_prompt": "размыто, искажения",
  "steps": 25,
  "width": 512,
  "height": 512,
  "seed": 42
}
```

`prompt` обязателен; `width`/`height` кратны 64, 256..1536 (иначе `400`);
`seed` и `guidance_scale` опциональны (по умолчанию — `default_guidance` модели). Ответ 200:

```json
{
  "model": "dreamshaper-8",
  "backend": "openvino_sd",
  "width": 512, "height": 512, "steps": 25, "seed": 42,
  "image_base64": "<PNG в base64>"
}
```

Ошибки: `400` параметры, `409` нет активной / активна текстовая, `502` ошибка генерации.

### 4.3a Речь: POST /api/tts

```json
{"text": "Привет!", "language": "ru", "speaker": "narrator.wav", "temperature": null}
```

`text` обязателен (до 4000 символов); `speaker` — имя файла-образца (wav/mp3, 5–15 с)
в папке `voices/` для клонирования голоса. Ответ 200:

```json
{"model": "qwen3-tts", "backend": "llama_tts", "format": "wav", "duration_sec": 4.2, "audio_base64": "..."}
```

Ошибки: `409` нет активной / активна модель другого типа, `502` ошибка синтеза.

### 4.4 OpenAI-совместимый прокси

Сервис предоставляет OpenAI-совместимые эндпоинты:

- **GET /v1/models** — все установленные текстовые модели (`active: true` у запущенной)
- **POST /v1/chat/completions** — запрос к текстовой модели
  (поддерживает параметр `stream=true` с SSE-стримингом, инструменты `tools`)

Поведение (`AIM_AUTOSTART=true`, по умолчанию):

- если поле `model` — установленная текстовая модель из реестра, она запускается
  автоматически (остальные останавливаются, I1); неизвестные имена игнорируются;
- если процесс llama-server активной модели упал — он перезапускается при следующем запросе;
- параметры `request_defaults` модели подставляются, если клиент их не передал;
  `cache_prompt=true` по умолчанию;
- стрим пробрасывается без изменений; обрыв соединения клиентом сразу отменяет
  генерацию в llama-server;
- assistant-сообщения с `tool_calls` и сообщения `tool` не отбрасываются, даже с пустым `content`.

При отсутствии активной текстовой модели (и `AIM_AUTOSTART=false`) возвращается код `409`.

Запрос идёт через очередь (4.5): модель из поля `model` запускается, когда до задачи
дойдёт очередь. Ответ содержит заголовок `X-Job-Id`.

### 4.5 Очередь задач

Все запросы к моделям проходят через планировщик (`app/jobs.py`) и учитываются в SQLite
(`AIM_DB`, по умолчанию `data/ai_manager.db`).

Правила:

- задачи к **текущей** модели выполняются первыми, по порядку поступления, до `parallel`
  штук одновременно (поле реестра; для не-llama бэкендов — 1);
- модель переключается **только** когда по текущей модели нет задач (I6); дальше берётся
  самая старая задача к другой модели;
- ожидание в очереди ограничено `AIM_JOB_WAIT_TIMEOUT` (по умолчанию 3600 с) → статус
  `expired`, HTTP `503`;
- выполнение (после запуска модели) ограничено `AIM_JOB_RUN_TIMEOUT` (3600 с) → статус
  `timeout`, HTTP `504`; генерация в llama-server прерывается (соединение закрывается).
  Картинки/речь (OpenVINO, llama-tts) прервать нельзя: клиент сразу получает `504`, но
  GPU-слот освобождается после фактического завершения;
- обрыв соединения клиентом → `cancelled` (и в очереди, и во время стрима).

Заголовки запроса (необязательные):

| Заголовок | Описание |
|---|---|
| `X-Project` | Имя проекта-источника (иначе поле `project`/`user` тела, иначе `ip:<адрес>`) |
| `X-Job-Timeout` | Лимит выполнения, сек (до 86400) |
| `X-Job-Wait-Timeout` | Лимит ожидания в очереди, сек (до 86400) |

Поле `model` теперь есть в `/api/chat`, `/api/image`, `/api/tts`: модель запускается
автоматически, когда подходит её очередь. Без `model` используется текущая модель
подходящего типа.

Статусы задачи: `queued → running → done | failed | cancelled | timeout`,
`queued → expired | cancelled`.

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/queue` | Выполняющиеся и ожидающие задачи, `switch_locked` |
| POST | `/api/jobs` | Асинхронная задача → `202 {"id", "status", "model", "position"}` |
| GET | `/api/jobs` | История: `?status=queued,running&project=&model=&limit=100` |
| GET | `/api/jobs/stats` | Сводка по проектам/моделям/статусам: `?hours=24` |
| GET | `/api/jobs/{id}` | Задача; для async — с `result` (`?include_result=false` — без него) |
| POST | `/api/jobs/{id}/cancel` | Отмена ожидающей или выполняющейся задачи |

`POST /api/jobs`:

```json
{
  "kind": "chat",                 // chat | image | tts
  "model": "qwen36-35b-a3b",      // необязательно
  "project": "auto_posting",      // необязательно
  "payload": {"messages": [{"role": "user", "content": "..."}], "max_tokens": 2000},
  "wait_timeout": 7200,           // необязательно, сек
  "run_timeout": 1800             // необязательно, сек
}
```

`payload`: для `chat` — тело OpenAI `/chat/completions`; для `image` — поля `ImageRequest`;
для `tts` — поля `TTSRequest`. Результат: `chat` — `{"model","content","finish_reason","usage",
"reasoning_content"?, "tool_calls"?}`, `image` — `ImageResponse`, `tts` — `TTSResponse`.

После перезапуска сервиса ожидавшие async-задачи ставятся в очередь заново; прерванные
и синхронные — помечаются `failed`. История хранится `AIM_JOB_RETENTION_DAYS` (14) дней.

`GET /api/status` дополнительно возвращает `queue_running`, `queue_waiting`, `switch_locked`.
`POST /api/restart` при непустой очереди — `409 queue_busy` (`?force=true` — всё равно).

## 5. Бэкенды

### 5.1 `llama_server` (текст, GGUF, GPU)

- Установка: `hf_hub_download(repo:file)` в `AIM_HF_CACHE/<name>/`.
- Проверка: наличие файла весов.
- Запуск: `llama-server -m <веса> --host 127.0.0.1 --port <AIM_PORT_RANGE>
  -c <context> -np <parallel> --alias <name> [-ngl <ngl>] [--n-cpu-moe N] [-fa ..] <args>`;
  health-check `GET /health` (таймаут 10 мин). Команда запуска пишется в лог
  `logs/llama-server-<name>.log`.
- Остановка: `SIGTERM` → через 15 с `kill`; освобождает VRAM (инвариант I1).
- Запрос: `POST /v1/chat/completions` (OpenAI-совместимый).
- GPU: сборки `win-vulkan-x64` (по умолчанию) или `win-sycl-x64` (нужен oneAPI),
  путь через `LLAMA_SERVER_BIN`. Процесс наследует окружение — переменные
  `ONEAPI_DEVICE_SELECTOR` и пр. можно задать в `.env`.

### 5.2 `openvino_sd` (графика, OpenVINO, GPU)

- Установка: `snapshot_download(model_ref)` в `AIM_HF_CACHE`.
- Проверка: снапшот с `model_index.json` в кэше.
- Запуск: загрузка `optimum.intel.OVDiffusionPipeline.from_pretrained(...)` (класс SD/SDXL/FLUX
  определяется по `model_index.json`; готовые OpenVINO-сборки — без конвертации,
  иначе `export=True`) и перенос на устройство GPU (Intel Arc); смена активной модели
  выгружает пайплайн (`del` + `gc`).
- Запрос: `pipe(prompt, negative_prompt, num_inference_steps, height, width,
  generator=seed)`. Результат — PNG в base64.
- Зависимости: `optimum-intel[openvino]`, `torch` (см. requirements.txt).

### 5.3 `llama_tts` (речь, GGUF, GPU)

- Установка: `hf_hub_download` файлов `model_ref` и `mmproj` в `AIM_HF_CACHE/<name>/`.
- Запуск: проверка весов и `llama-tts.exe` рядом с llama-server; GPU резервируется (I1).
- Запрос: отдельный процесс `llama-tts -m <веса> -mm <mmproj> --tts-lang <язык>
  [--tts-speaker-file voices/<speaker>] --output <wav>`; лог — `logs/llama-tts-<name>.log`.

### 5.4 Добавление бэкенда

Наследовать `app/backends/base.py.Backend`, реализовать методы жизненного цикла
и `chat` (текст), `generate_image` (графика) и/или `synthesize` (речь); зарегистрировать
в `BACKENDS` (`app/manager.py`). Класс-атрибуты `supports_images` / `supports_audio`
определяют тип модели (`text` / `image` / `audio`).

## 6. Состояния модели

```
not_installed --install--> installed --start--> running --stop--> installed
running --start другой модели--> stopped (автоматически, I1)
```

## 7. Веб-интерфейс

`GET /` — одностраничный UI: ввод пароля, таблица моделей с кнопками
«Установить»/«Запустить»/«Стоп», индикатор активной модели, форма чата
(текст) и форма генерации изображений с предпросмотром.

## 8. Скрипты (Windows)

| Скрипт | Назначение |
|---|---|
| `scripts/setup.bat` | venv + зависимости + `.env` + скачивание llama-server (Vulkan) |
| `scripts/run.bat` | Запуск сервиса с авто-перезапуском при падении и зависании (запускает `watchdog.ps1`) |
| `scripts/watchdog.ps1` | Внешний сторож: перезапуск, если `/health` не отвечает (см. 8.1) |
| `scripts/restart.bat` | Перезапуск только процесса AI Manager этого проекта |
| `scripts/install_autostart.bat` | Задача планировщика: запуск при входе в Windows |
| `scripts/remove_autostart.bat` | Удаление задачи автозапуска |
| `scripts/download_llama_server.ps1` | Скачивание последней сборки llama-server |
| `scripts/open_firewall.bat` | Настройка правил брандмауэра + добавление LAN-адресов (требует прав администратора) |

### 8.1 Сторож живости

Процесс может «зависнуть» живым (event loop занят, слушающий сокет закрыт) — тогда
цикл в `run.bat` не срабатывает. Защита в два уровня, оба опираются на `GET /health`
(без пароля, отвечает сам event loop: `{"status":"ok"}`):

1. **Внутри процесса** (`app/watchdog.py`): heartbeat-задача asyncio раз в секунду и
   поток-проверяльщик. Перезапуск, если loop молчит дольше `AIM_HANG_TIMEOUT` или
   `/health` на свой порт не ответил `AIM_PROBE_FAILS` раз подряд. Перед выходом стеки
   всех потоков пишутся в `logs/hang-*.log`, модель останавливается (до 15 с),
   процесс завершается вместе с llama-server (`taskkill /T`), `run.bat` поднимает его через 5 с.
2. **Снаружи** (`scripts/watchdog.ps1`, один экземпляр, стартует из `run.bat`): каждые 30 с
   проверяет `/health`; после 5 неудач (но не раньше 300 с после старта) убивает uvicorn
   этого проекта (ищет по `--app-dir`). Нужен, если завис весь интерпретатор или старт.
   Журнал — `logs/watchdog.log`; сам завершается, если сервис не запущен 2 мин.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AIM_WATCHDOG` | `true` | Включить сторож внутри процесса |
| `AIM_HANG_TIMEOUT` | `60` | Сколько секунд event loop может не отвечать |
| `AIM_PROBE_INTERVAL` | `30` | Интервал проверки `/health`, с |
| `AIM_PROBE_FAILS` | `3` | Неудач `/health` подряд до перезапуска |

## 9. Ограничения

- Один экземпляр сервиса на машину (очередь — в памяти процесса, история — в SQLite).
- Строгое правило I6: если один проект непрерывно шлёт задачи к текущей модели,
  задачи к другим моделям ждут до `AIM_JOB_WAIT_TIMEOUT` и истекают (`expired`).
- **stream=true** не поддерживается в `/api/chat` (возвращается `400`).
- В `/v1/chat/completions` поддерживается стриминг (SSE) через параметр `stream=true`.
- Установка моделей — синхронная.
- Авторизация — один общий пароль (заголовок), без пользователей/ролей.
