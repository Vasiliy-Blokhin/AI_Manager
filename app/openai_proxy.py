"""
OpenAI-совместимый прокси (/v1/*) для IDE-расширений и local_code_worker.

Запросы пересылаются на активную текстовую модель, поддерживается SSE-стриминг.
Авторизация — пароль AIM_PASSWORD через заголовок Authorization: Bearer или
X-API-Password (проверяется в middleware app/main.py).

Особенности:
- Отсечки по времени исполнения НЕТ: длинные запросы к модели не прерываются.
- Один асинхронный httpx.AsyncClient на процесс (keep-alive к llama-server).
- Стрим пробрасывается байт-в-байт; при обрыве соединения клиентом закрывается
  и соединение с llama-server — генерация останавливается сразу, GPU освобождается.
- Поле "model" запроса: если это установленная текстовая модель из реестра,
  она запускается автоматически (AIM_AUTOSTART). Упавший llama-server
  перезапускается при следующем запросе.
- Параметры по умолчанию из реестра (request_defaults: reasoning_effort и т.п.)
  подставляются, если клиент их не задал. Включён кэш промпта (cache_prompt).
- Пустые сообщения отбрасываются (кроме assistant с tool_calls и ответов tool).
"""
from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .backends.base import BackendError
from .manager import ModelManager, NotInstalledError, WrongModelTypeError

_CLIENT: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        # Без таймаута чтения: генерация может идти десятки минут.
        _CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=30.0))
    return _CLIENT


def _err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def _sanitize_messages(messages: list) -> list:
    """Убираем сообщения с пустым контентом — лишние токены префилла."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("tool_calls") or m.get("role") == "tool":
            out.append(m)
            continue
        content = m.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            continue
        out.append(m)
    return out


def _sse_error(code: str, message: str) -> bytes:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
    return f"data: {body}\n\ndata: [DONE]\n\n".encode()


def build_openai_router(manager: ModelManager) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["openai"])

    async def _backend(requested: str | None):
        autostart = manager.settings.get("autostart", True)
        try:
            be = await run_in_threadpool(manager.text_backend_for, requested, autostart)
        except WrongModelTypeError:
            raise _err("wrong_model_type", "Active model is image type. Start a text model.", 409)
        except NotInstalledError as e:
            raise _err("not_installed", str(e), 409)
        except BackendError as e:
            raise _err("start_failed", str(e), 502)
        except RuntimeError:
            raise _err("no_active_model",
                       "No active text model. Start one via POST /api/models/{name}/start "
                       "or pass an installed model name in the 'model' field.", 409)
        if not getattr(be, "openai_base_url", None):
            raise _err("unsupported", f"Backend {be.backend_name} does not support OpenAI interface.", 502)
        return be

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
            raise _err("bad_request", "Invalid JSON body.", 400)
        if not isinstance(payload, dict) or not payload.get("messages"):
            raise _err("bad_request", "Field 'messages' is required.", 400)

        be = await _backend(payload.get("model"))
        url = f"{be.openai_base_url}/chat/completions"

        for k, v in getattr(be, "request_defaults", {}).items():
            payload.setdefault(k, v)
        payload["messages"] = _sanitize_messages(payload["messages"])
        payload.setdefault("cache_prompt", True)

        if payload.get("stream"):
            return StreamingResponse(_stream(url, payload), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        try:
            r = await _client().post(url, json=payload)
        except httpx.HTTPError as e:
            raise _err("backend_error", f"Request to model failed: {e}", 502)
        try:
            return JSONResponse(r.json(), status_code=r.status_code)
        except ValueError:
            return JSONResponse({"error": {"code": "backend_error", "message": r.text[:500]}},
                                status_code=502)

    return router


async def _stream(url: str, payload: dict):
    """SSE-стрим из llama-server, байт-в-байт. Отмена генератора закрывает upstream."""
    try:
        async with _client().stream("POST", url, json=payload) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode("utf-8", "replace")[:1000]
                yield _sse_error("backend_error", f"HTTP {r.status_code}: {text}")
                return
            async for chunk in r.aiter_raw():
                yield chunk
    except httpx.HTTPError as e:
        yield _sse_error("backend_error", f"Stream from model failed: {e}")
