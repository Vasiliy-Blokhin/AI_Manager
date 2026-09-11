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
| `backend` | да | `llama_server` (текст, GGUF) или `openvino_sd` (картинки) |
| `model_ref` | да | llama_server: `repo:file` на HuggingFace; openvino_sd: `repo_id` |
| `ngl` | нет | Число слоёв на GPU (`-ngl`, по умолчанию `AIM_GPU_LAYERS`=99) |
| `context` | нет | Размер контекста (`-c`, по умолчанию 16384) |

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

Ошибки: `404` не в реестре, `409` не установлена, `502` бэкенд не смог запуститься.

#### POST /api/models/stop — ответ 200: `{"running": null}`

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

`prompt` обязателен; `width`/`height` кратны 64 (иначе `400`);
`seed` опционален. Ответ 200:

```json
{
  "model": "dreamshaper-8",
  "backend": "openvino_sd",
  "width": 512, "height": 512, "steps": 25, "seed": 42,
  "image_base64": "<PNG в base64>"
}
```

Ошибки: `400` параметры, `409` нет активной / активна текстовая, `502` ошибка генерации.

### 4.4 OpenAI-совместимый прокси

Сервис предоставляет OpenAI-совместимые эндпоинты:

- **GET /v1/models** — возвращает список доступных текстовых моделей
- **POST /v1/chat/completions** — запрос к активной текстовой модели
  (поддерживает параметр `stream=true` с SSE-стримингом)

При отсутствии активной текстовой модели возвращается код `409`.

## 5. Бэкенды

### 5.1 `llama_server` (текст, GGUF, GPU)

- Установка: `hf_hub_download(repo:file)` в `AIM_HF_CACHE/<name>/`.
- Проверка: наличие файла весов.
- Запуск: `llama-server -m <веса> --host 127.0.0.1 --port <AIM_PORT_RANGE>
  -ngl <ngl> -c <context>`; health-check `GET /health` (таймаут 10 мин).
- Остановка: `SIGTERM` → через 15 с `kill`; освобождает VRAM (инвариант I1).
- Запрос: `POST /v1/chat/completions` (OpenAI-совместимый).
- GPU: сборки `win-vulkan-x64` (по умолчанию) или `win-sycl-x64` (нужен oneAPI),
  путь через `LLAMA_SERVER_BIN`. Процесс наследует окружение — переменные
  `ONEAPI_DEVICE_SELECTOR` и пр. можно задать в `.env`.

### 5.2 `openvino_sd` (графика, OpenVINO, GPU)

- Установка: `snapshot_download(model_ref)` в `AIM_HF_CACHE`.
- Проверка: снапшот с `model_index.json` в кэше.
- Запуск: загрузка `optimum.intel.OVStableDiffusionPipeline.from_pretrained(...,
  export=True)` и перенос на устройство GPU (Intel Arc); смена активной модели
  выгружает пайплайн (`del` + `gc`).
- Запрос: `pipe(prompt, negative_prompt, num_inference_steps, height, width,
  generator=seed)`. Результат — PNG в base64.
- Зависимости: `optimum-intel[openvino]`, `torch` (см. requirements.txt).

### 5.3 Добавление бэкенда

Наследовать `app/backends/base.py.Backend`, реализовать методы жизненного цикла
и `chat` (текст) и/или `generate_image` (графика); зарегистрировать в `BACKENDS`
(`app/manager.py`). Класс-атрибут `supports_images` определяет тип модели.

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
| `scripts/run.bat` | Запуск сервиса с авто-перезапуском при падении |
| `scripts/install_autostart.bat` | Задача планировщика: запуск при входе в Windows |
| `scripts/remove_autostart.bat` | Удаление задачи автозапуска |
| `scripts/download_llama_server.ps1` | Скачивание последней сборки llama-server |
| `scripts/open_firewall.bat` | Настройка правил брандмауэра + добавление LAN-адресов (требует прав администратора) |

## 9. Ограничения

- Один экземпляр сервиса на машину (состояние в памяти).
- **stream=true** не поддерживается в `/api/chat` (возвращается `400`).
- В `/v1/chat/completions` поддерживается стриминг (SSE) через параметр `stream=true`.
- Установка моделей — синхронная.
- Авторизация — один общий пароль (заголовок), без пользователей/ролей.
