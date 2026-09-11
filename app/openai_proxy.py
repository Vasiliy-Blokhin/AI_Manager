"""OpenAI-совместимый прокси (/v1/*) для IDE-расширений (Continue, Cline, Roo Code).

Запросы пересылаются на активную текстовую модель, поддерживается SSE-стриминг.
Авторизация — пароль AIM_PASSWORD через заголовок Authorization: Bearer или
X-API-Password (проверяется в middleware app/main.py).
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .manager import ModelManager, WrongModelTypeError


def _err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def build_openai_router(manager: ModelManager) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["openai"])

    def _active_base_url() -> str:
        """OpenAI-base активной текстовой модели или HTTP-ошибка 409."""
        try:
            be = manager.active_text_backend()
        except WrongModelTypeError:
            raise _err("wrong_model_type", "Активна графическая модель", 409)
        except RuntimeError:
            raise _err("no_active_model",
                       "Нет активной текстовой модели. Запустите: POST /api/models/{name}/start", 409)
        if not be.is_running():
            raise _err("no_active_model", "Модель не запущена", 409)
        url = getattr(be, "openai_base_url", None)
        if not url:
            raise _err("unsupported",
                       f"Бэкенд {be.backend_name} не поддерживает OpenAI-интерфейс", 502)
        return url

    @router.get("/models")
    async def list_models() -> JSONResponse:
        try:
            _active_base_url()
            name = manager.active_name() or "unknown"
        except HTTPException:
            return JSONResponse({"object": "list", "data": []})
        return JSONResponse({"object": "list", "data": [{
            "id": name, "object": "model", "created": 0, "owned_by": "ai-manager"}]})

    @router.post("/chat/completions")
    async def chat_completions(request: Request):
        try:
            payload = await request.json()
        except Exception:
            raise _err("bad_request", "Невалидный JSON", 400)
        if not isinstance(payload, dict) or not payload.get("messages"):
            raise _err("bad_request", "Поле messages обязательно", 400)

        base = _active_base_url()
        url = f"{base}/chat/completions"

        if payload.get("stream"):
            return StreamingResponse(_stream(url, payload),
                                     media_type="text/event-stream")
        return await run_in_threadpool(_post_json, url, payload)

    return router


def _post_json(url: str, payload: dict) -> JSONResponse:
    try:
        r = httpx.post(url, json=payload, timeout=1800)
        # пробрасываем тело как есть (включая ошибки бэкенда) — совместимость с OpenAI
        return JSONResponse(r.json(), status_code=r.status_code)
    except ValueError:  # не-JSON от бэкенда
        return JSONResponse({"error": {"code": "backend_error", "message": r.text[:500]}},
                            status_code=502)
    except Exception as e:
        raise _err("backend_error", f"Ошибка запроса к модели: {e}", 502)


def _stream(url: str, payload: dict):
    """SSE-стрим из бэкенда (синхронный генератор — starlette исполнит в тредпуле)."""
    body = dict(payload)
    body["stream"] = True
    try:
        with httpx.stream("POST", url, json=body, timeout=1800) as r:
            if r.status_code != 200:
                yield f"data: {r.text[:500]}\n\n"
                yield "data: [DONE]\n\n"
                return
            for line in r.iter_lines():
                if line:
                    yield line + "\n\n"
    except Exception as e:
        yield f"data: {e}\n\n"