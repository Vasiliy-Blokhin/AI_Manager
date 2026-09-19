"""
OpenAI-совместимый прокси (/v1/*) для IDE-расширений (Continue, Cline, Roo Code).

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
        """
        Возвращает OpenAI-base URL активной текстовой модели.
        Исправлено обращение к приватному методу manager._active_text_backend().
        Если модель не запущена или имеет неверный тип, выбрасывается 409 Conflict.
        """
        try:
            # В dev-версии менеджер скрывает методы бэкендов за "_" 
            be = manager._active_text_backend()
        except WrongModelTypeError:
            raise _err("wrong_model_type", "Active model is image type. Start a text model.", 409)
        except RuntimeError:
            raise _err(
                "no_active_model",
                "No active text model found. Start one via POST /api/models/{name}/start.",
                409
            )
        
        if not be.is_running():
            raise _err("no_active_model", "The selected model process is not running.", 409)
            
        url = getattr(be, "openai_base_url", None)
        if not url:
            raise _err(
                "unsupported",
                f"Backend {be.backend_name} does not support OpenAI interface.",
                502
            )
        return url

    @router.get("/models")
    async def list_models() -> JSONResponse:
        """
        Список моделей для OpenAI SDK. 
        Если активная модель не настроена, возвращает пустой список data[], чтобы расширения VS Code не падали.
        """
        try:
            base = _active_base_url()
            name = manager.active_name() or "unknown"
            # Проверяем, поддерживает ли бэкенд endpoint /v1/models напрямую
            test_url = f"{base}/models"
            try:
                r = httpx.get(test_url, timeout=5.0)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
                
            # Fallback: возвращаем только активную модель в формате OpenAI
            return JSONResponse({
                "object": "list",
                "data": [{
                    "id": name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "ai-manager"
                }]
            })
        except HTTPException:
            # Если нет активной модели, вернем валидный пустой ответ для Continue/Cline
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
        if payload.get("stream"):
            return StreamingResponse(_stream(url, payload), media_type="text/event-stream")
        
        return await run_in_threadpool(_post_json, url, payload)

    @router.get("/code")
    async def get_code() -> JSONResponse:
        """Получение накопленного кода из активного бэкенда."""
        code = manager.get_code()
        return JSONResponse({"code": code})

    @router.post("/code")
    async def save_code(request: Request):
        """Сохранение фрагмента кода (добавление к существующему)."""
        try:
            payload = await request.json()
        except Exception:
            raise _err("bad_request", "Invalid JSON body.", 400)
        if not isinstance(payload, dict) or not payload.get("code"):
            raise _err("bad_request", "Field 'code' is required.", 400)

        code = payload.get("code", "")
        manager.save_code(code)
        return JSONResponse({"status": "success"})

    return router


def _post_json(url: str, payload: dict) -> JSONResponse:
    """Синхронный POST-запрос к llama-server/compatible backend."""
    try:
        r = httpx.post(url, json=payload, timeout=1800)
        # Пробрасываем тело как есть (включая ошибки бэкенда) — совместимость с OpenAI
        return JSONResponse(r.json(), status_code=r.status_code)
    except ValueError:  # не-JSON от бэкенда
        # Используем переменную ответа 'r', если она определена, иначе общий текст
        resp_text = locals().get('r').text[:500] if 'r' in locals() else "Non-JSON response from backend."
        return JSONResponse(
            {"error": {"code": "backend_error", "message": resp_text}},
            status_code=502
        )
    except Exception as e:
        raise _err("backend_error", f"Request to model failed: {e}", 502)


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