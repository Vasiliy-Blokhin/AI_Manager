"""
OpenAI-совместимый прокси (/v1/*) для IDE-расширений и local_code_worker.

Запросы идут через очередь задач (app/jobs.py) и пересылаются на нужную текстовую
модель, поддерживается SSE-стриминг.
Авторизация — пароль AIM_PASSWORD через заголовок Authorization: Bearer или
X-API-Password (проверяется в middleware app/main.py).

Особенности:
- Каждый запрос — задача в БД. Пока по текущей модели есть задачи, модель не
  переключается; запросы к другой модели ждут своей очереди.
- Время ожидания и выполнения ограничено (AIM_JOB_WAIT_TIMEOUT / AIM_JOB_RUN_TIMEOUT,
  заголовки X-Job-Wait-Timeout / X-Job-Timeout). Проект — заголовок X-Project.
- Один асинхронный httpx.AsyncClient на процесс (keep-alive к llama-server).
- Стрим пробрасывается байт-в-байт; при обрыве соединения клиентом закрывается
  и соединение с llama-server — генерация останавливается сразу, GPU освобождается.
- Поле "model" запроса: если это установленная текстовая модель из реестра,
  она запускается автоматически (AIM_AUTOSTART), когда до неё дойдёт очередь.
- Параметры по умолчанию из реестра (request_defaults: reasoning_effort и т.п.)
  подставляются, если клиент их не задал. Включён кэш промпта (cache_prompt).
- Пустые сообщения отбрасываются (кроме assistant с tool_calls и ответов tool).
- Ответ содержит заголовок X-Job-Id.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .executors import JobStreamingResponse, chat_completion, chat_summary, usage_of
from .jobs import JobQueue
from .queue_api import err, http_errors, project_of, timeouts_of


def build_openai_router(queue: JobQueue) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["openai"])
    manager = queue.manager

    @router.get("/models")
    async def list_models() -> JSONResponse:
        """Все установленные текстовые модели (любую можно указать в "model" — запустится сама)."""
        infos = await run_in_threadpool(manager.list_models)
        data = [{"id": m.name, "object": "model", "created": 0, "owned_by": "ai-manager",
                 "active": m.running}
                for m in infos if m.type == "text" and m.installed]
        return JSONResponse({"object": "list", "data": data})

    @router.post("/chat/completions")
    async def chat_completions(request: Request):
        try:
            payload = await request.json()
        except Exception:
            raise err("bad_request", "Invalid JSON body.", 400)
        if not isinstance(payload, dict) or not payload.get("messages"):
            raise err("bad_request", "Field 'messages' is required.", 400)

        wait_timeout, run_timeout = timeouts_of(request)
        stream = bool(payload.get("stream"))
        with http_errors():
            model = queue.resolve_model("chat", payload.get("model"))
            t = queue.new_ticket(kind="chat", model=model, project=project_of(request, payload),
                                 summary=chat_summary(payload), stream=stream,
                                 wait_timeout=wait_timeout, run_timeout=run_timeout)
            await queue.acquire(t, request.is_disconnected)
        headers = {"X-Job-Id": t.id}

        if stream:
            # задачу закрывает генератор/ответ — по окончании стрима или обрыве
            return JobStreamingResponse(queue, t, payload, headers)

        def _check(res: tuple) -> str | None:
            code, data = res
            return None if code == 200 else f"llama-server HTTP {code}: {str(data)[:500]}"

        with http_errors():
            code, data = await queue.execute(t, chat_completion(queue, t, payload),
                                             check=_check, usage=lambda r: usage_of(r[1]))
        return JSONResponse(data, status_code=code, headers=headers)

    return router
