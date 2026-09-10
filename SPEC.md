# Спецификация AI Manager Service

## 1. Назначение

Сервис предоставляет единую точку входа для работы с несколькими локальными
ИИ-моделями: проверка наличия, установка, запуск/остановка и стандартизированные
запросы к активной модели.

Инварианты системы:

1. **I1.** Активна не более одной модели. Запуск модели автоматически останавливает
   все остальные. Допустимо состояние «все выключены».
2. **I2.** Все модели из реестра доступны через единый формат запроса `/api/chat`,
   независимо от бэкенда.
3. **I3.** Запрос к `/api/chat` при отсутствии активной модели возвращает ошибку
   `409 Conflict`.

## 2. Реестр моделей

Файл `config/models.registry.json`:

```json
{
  "models": [
    {
      "name": "llama3.1-8b",
      "backend": "ollama",
      "model_ref": "llama3.1:8b",
      "display_name": "Llama 3.1 8B",
      "description": "Универсальная модель Meta"
    },
    {
      "name": "qwen2.5-7b",
      "backend": "ollama",
      "model_ref": "qwen2.5:7b",
      "display_name": "Qwen 2.5 7B",
      "description": "Сильна в коде и математике"
    },
    {
      "name": "phi3-mini-gguf",
      "backend": "llama_server",
      "model_ref": "microsoft/Phi-3-mini-4k-instruct-gguf:Phi-3-mini-4k-instruct-q4.gguf",
      "display_name": "Phi-3 Mini (GGUF)",
      "description": "Лёгкая модель через llama.cpp"
    }
  ]
}
```

Поля:

| Поле | Обяз. | Описание |
|---|---|---|
| `name` | да | Уникальный идентификатор модели в сервисе |
| `backend` | да | `ollama` или `llama_server` |
| `model_ref` | да | Для Ollama — тег модели; для llama_server — `repo:file` на HuggingFace |
| `display_name`, `description` | нет | Метаданные для UI |

## 3. REST API

Все эндпоинты, кроме `/`, — префикс `/api`. Ошибки возвращаются в формате:

```json
{"error": {"code": "model_not_found", "message": "..."}}
```

### 3.1 Модели

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/models` | Список моделей со статусами |
| GET | `/api/models/{name}` | Одна модель со статусом |
| POST | `/api/models/install` | Проверить наличие; при отсутствии скачать и установить |
| POST | `/api/models/{name}/start` | Запустить модель (остановив остальные) |
| POST | `/api/models/stop` | Остановить активную модель (все выключены) |
| GET | `/api/status` | Краткий статус сервиса и активной модели |

#### GET /api/models — ответ 200

```json
{
  "active": "llama3.1-8b",
  "models": [
    {
      "name": "llama3.1-8b",
      "backend": "ollama",
      "display_name": "Llama 3.1 8B",
      "installed": true,
      "running": true,
      "pid": 12345,
      "endpoint": "http://127.0.0.1:11434"
    }
  ]
}
```

`active` равен `null`, если все модели выключены.

#### POST /api/models/install — запрос

```json
{"name": "llama3.1-8b"}
```

Ответ 200: `{"name": "...", "installed": true, "was_installed": false}`
(операция синхронная; для больших моделей может занять минуты).

#### POST /api/models/{name}/start — ответ 200

```json
{"name": "llama3.1-8b", "running": true, "stopped": ["qwen2.5-7b"]}
```

Ошибки: `404` модель не в реестре, `409` не установлена (сначала `/install`),
`502` бэкенд не смог запуститься.

#### POST /api/models/stop — ответ 200

```json
{"running": null}
```

### 3.2 Стандартизированный запрос: POST /api/chat

Запрос (OpenAI-совместимый подмножество):

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

`messages` обязателен; `temperature`, `max_tokens` — опционально; `stream` в MVP
только `false`.

Ответ 200:

```json
{
  "model": "llama3.1-8b",
  "backend": "ollama",
  "content": "Привет! Чем могу помочь?",
  "usage": {"prompt_tokens": 25, "completion_tokens": 8, "total_tokens": 33}
}
```

Ошибки: `409` нет активной модели, `502` ошибка вывода модели.

## 4. Бэкенды

### 4.1 `ollama`

- Проверка установки: `GET {AIM_OLLAMA_URL}/api/tags`, поиск `model_ref`.
- Установка: `ollama pull <model_ref>`.
- Запуск: Ollama держит модели в памяти при первом запросе; сервис делает
  прогревочный запрос после старта.
- Запрос: `POST {AIM_OLLAMA_URL}/api/chat` с преобразованием схемы.

### 4.2 `llama_server`

- Установка: скачивание файла `<file>` из репозитория HuggingFace `<repo>`
  (через `huggingface_hub`) в `AIM_HF_CACHE/<name>/`.
- Проверка: наличие файла весов на диске.
- Запуск: `llama-server -m <веса> --port <порт из AIM_PORT_RANGE> --host 127.0.0.1`
  в отдельном процессе; health-check `GET /health`.
- Остановка: `SIGTERM` процессу.
- Запрос: `POST http://127.0.0.1:<порт>/v1/chat/completions` (OpenAI-совместимый).

### 4.3 Добавление бэкенда

Наследовать `app/backends/base.py.Backend`:

```python
class MyBackend(Backend):
    backend_name = "my_backend"
    def is_installed(self) -> bool: ...
    def install(self) -> None: ...
    def start(self) -> StartInfo: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...
    def chat(self, request: ChatRequest) -> ChatResponse: ...
```

Затем добавить класс в `BACKENDS` в `app/manager.py`.

## 5. Состояния модели

```
not_installed --install--> installed --start--> running --stop--> installed
running --start другой модели--> stopped (автоматически)
```

## 6. Веб-интерфейс

`GET /` отдаёт одностраничный UI (app/static/index.html): таблица моделей
с кнопками «Установить» / «Запустить» / «Стоп», индикатор активной модели,
форма чата с активной моделью.

## 7. Ограничения MVP

- Один экземпляр сервиса на машину (состояние в памяти).
- Без авторизации; для продакшена поставить за reverse proxy с авторизацией.
- `stream=true` не поддерживается (вернётся `400`).
- Установка — синхронная (для больших моделей используйте запрос с длинным таймаутом).
