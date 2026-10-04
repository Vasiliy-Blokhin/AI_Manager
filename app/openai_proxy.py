"""
OpenAI-совместимый прокси (/v1/*) для IDE-расширений (Continue, Cline, Roo Code).

Запросы пересылаются на активную текстовую модель, поддерживается SSE-стриминг.
Авторизация — пароль AIM_PASSWORD через заголовок Authorization: Bearer или
X-API-Password (проверяется в middleware app/main.py).

Особенности:
- Отсечки по времени исполнения НЕТ (timeout=None): длинные запросы к модели
  не прерываются.
- Переиспользуется один httpx.Client на процесс.
- Включён кэш промпта (cache_prompt=true): повторный префикс контекста
  не пересчитывается — экономия токенов и времени.
- Пустые сообщения отбрасываются до отправки модели.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .manager import ModelManager, WrongModelTypeError

# Единый клиент без таймаутов на чтение/запись (отсечка по времени снята).
# connect=30 — только чтобы быстро упасть, если бэкенд недоступен.
_CLIENT: httpx.Client | None = None


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(timeout=httpx.Timeout(None, connect=30.0))
    return _CLIENT


def _err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def _sanitize_messages(messages: list) -> list:
    """Убираем сообщения с пустым контентом — лишние токены префилла."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            continue
        out.append(m)
    return out


def build_openai_router(manager: ModelManager) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["openai"])

    def _active_base_url() -> str:
        try:
            be = manager._active_text_backend()
        except WrongModelTypeError:
            raise _err("wrong_model_type", "Active model is image type. Start a text model.", 409)
        except RuntimeError:
            raise _err(
                "no_active_model",
                "No active text model found. Start one via POST /api/models/{name}/start.",
                409,
            )

        if not be.is_running():
            raise _err("no_active_model", "The selected model process is not running.", 409)

        url = getattr(be, "openai_base_url", None)
        if not url:
            raise _err(
                "unsupported",
                f"Backend {be.backend_name} does not support OpenAI interface.",
                502,
            )
        return url

    @router.get("/models")
    async def list_models() -> JSONResponse:
        try:
            base = _active_base_url()
            name = manager.active_name() or "unknown"
            try:
                r = _client().get(f"{base}/models")
                if r.status_code == 200:
                    return JSONResponse(r.json())
            except Exception:
                pass
            return JSONResponse({
                "object": "list",
                "data": [{
                    "id": name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "ai-manager",
                }],
            })
        except HTTPException:
            return JSONResponse({"object": "list", "data": []})

    @router.post("/chat/completions")
    async def chat_completions(request: Request):
        """Проксирование чата."""
        try:
            payload = await request.json()
        except Exception:
            raise _err("bad_request", "Invalid JSON body.", 400)
        if not isinstance(payload, dict) or not payload.get("messages"):
            raise _err("bad_request", "Field 'messages' is required.", 400)

        base = _active_base_url()
        url = f"{base}/chat/completions"

        payload["messages"] = _sanitize_messages(payload["messages"])
        payload.setdefault("cache_prompt", True)

        if payload.get("stream"):
            return StreamingResponse(_stream(url, payload), media_type="text/event-stream")

        return await run_in_threadpool(_post_json, url, payload)

    return router


def _post_json(url: str, payload: dict) -> JSONResponse:
    """Синхронный POST-запрос к бэкенду (без таймаута исполнения)."""
    try:
        r = _client().post(url, json=payload)
        return JSONResponse(r.json(), status_code=r.status_code)
    except ValueError:  # не-JSON от бэкенда
        resp_text = r.text[:500] if "r" in locals() else "Non-JSON response from backend."
        return JSONResponse(
            {"error": {"code": "backend_error", "message": resp_text}},
            status_code=502,
        )
    except httpx.HTTPError as e:
        raise _err("backend_error", f"Request to model failed: {e}", 502)


def _stream(url: str, payload: dict):
    """SSE-стрим из бэкенда (синхронный генератор — starlette исполнит в тредпуле)."""
    body = dict(payload)
    body["stream"] = True
    try:
        with _client().stream("POST", url, json=body) as r:
            if r.status_code != 200:
                yield f"data: {r.text[:500]}\n\n"
                yield "data: [DONE]\n\n"
                return
            for line in r.iter_lines():
                if line:
                    yield line + "\n\n"
    except Exception as e:
        yield f"data: {e}\n\n"