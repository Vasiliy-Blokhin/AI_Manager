# AI Manager Service

Сервис для централизованного управления несколькими локальными ИИ-моделями.

## Возможности

- **Единый реестр моделей** — список поддерживаемых моделей в `config/models.registry.json`.
- **Автопроверка и установка** — при запуске сервис проверяет, установлены ли модели,
  и при необходимости скачивает и устанавливает их (`POST /api/models/install`).
- **Строго одна активная модель** — при запуске модели все остальные автоматически
  останавливаются; либо все модели выключены.
- **Стандартизированный интерфейс запросов** — единый формат `/api/chat`
  (OpenAI-совместимый) для всех бэкендов.
- **Два интерфейса доступа** — веб-интерфейс (`/`) и REST API (`/api/*`).
- **Плагинные бэкенды** — Ollama и llama.cpp server; легко добавить новые.

## Архитектура

```
ai-manager/
├── app/
│   ├── main.py            # FastAPI: веб + REST API
│   ├── schemas.py         # Pydantic-схемы (единый формат запросов/ответов)
│   ├── registry.py        # Загрузка и валидация реестра моделей
│   ├── manager.py         # ModelManager: install/start/stop, контроль "одна модель"
│   ├── backends/
│   │   ├── base.py        # Абстрактный бэкенд
│   │   ├── ollama.py      # Бэкенд Ollama (pull / generate)
│   │   └── llama_server.py# Бэкенд llama.cpp server (скачивание GGUF + запуск)
│   └── static/index.html  # Веб-интерфейс
├── config/
│   └── models.registry.json
├── requirements.txt
└── Dockerfile
```

## Установка и запуск

### 1. Зависимости

```bash
pip install -r requirements.txt
```

Также нужен установленный и работающий Ollama (для моделей с бэкендом `ollama`)
и/или бинарник `llama-server` (для бэкенда `llama_server`), путь к нему задаётся
через переменную окружения `LLAMA_SERVER_BIN`.

### 2. Запуск

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Опциональные переменные окружения:

| Переменная | Значение по умолчанию | Описание |
|---|---|---|
| `AIM_REGISTRY` | `config/models.registry.json` | Путь к реестру моделей |
| `AIM_OLLAMA_URL` | `http://127.0.0.1:11434` | URL Ollama |
| `AIM_PORT_RANGE` | `8100-8199` | Диапазон портов для llama-server |
| `LLAMA_SERVER_BIN` | `llama-server` | Путь к бинарнику llama.cpp server |
| `AIM_HF_CACHE` | `~/.cache/ai-manager` | Куда скачивать GGUF-веса |
| `AIM_AUTO_INSTALL` | `false` | Автоустановка отсутствующих моделей при старте |

Веб-интерфейс: `http://localhost:8000/`, документация API: `/docs`.

## Быстрый старт

```bash
# Список моделей и их статус
curl -s localhost:8000/api/models | jq

# Установить модель (скачать, если отсутствует)
curl -s -X POST localhost:8000/api/models/install -H 'Content-Type: application/json' \
     -d '{"name": "llama3.1-8b"}'

# Запустить модель (остальные будут остановлены автоматически)
curl -s -X POST localhost:8000/api/models/llama3.1-8b/start

# Стандартизированный запрос
curl -s -X POST localhost:8000/api/chat -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "Привет! Кто ты?"}],
  "temperature": 0.7
}'

# Выключить всё
curl -s -X POST localhost:8000/api/models/stop
```

## Как добавить новую модель

1. Добавьте запись в `config/models.registry.json` (см. SPEC.md, раздел «Реестр»).
2. Если бэкенд новый — реализуйте класс в `app/backends/` (интерфейс в `base.py`)
   и зарегистрируйте его в `manager.BACKENDS`.

Подробная спецификация API и форматов — в файле SPEC.md.
