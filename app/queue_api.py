"""
HTTP-слой очереди задач: общие хелперы для эндпоинтов и роутер /api/jobs, /api/queue.

Проект-источник запроса определяется так (первое найденное):
заголовок X-Project → поле "project" тела → поле "user" (OpenAI) → IP клиента.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .backends.base import BackendError
from .executors import chat_summary
from .jobs import JobCancelled, JobQueue, ModelBusyError, TERMINAL
from .manager import ModelNotFoundError, NotInstalledError, WrongModelTypeError
from .schemas import ImageRequest, TTSRequest

# HTTP-коды для финальных статусов отменённых задач
CANCEL_HTTP = {"timeout": 504, "expired": 503, "cancelled": 499}


def err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def project_of(request: Request, payload: Optional[dict] = None) -> str:
    header = request.headers.get("X-Project", "").strip()
    if header:
        return header[:100]
    if isinstance(payload, dict):
        for key in ("project", "user"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:100]
    return f"ip:{request.client.host}" if request.client else "unknown"


def _float_header(request: Request, name: str) -> Optional[float]:
    raw = request.headers.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise err("bad_request", f"Заголовок {name} должен быть числом секунд", 400)


def timeouts_of(request: Request) -> tuple[Optional[float], Optional[float]]:
    """(wait_timeout, run_timeout) из заголовков X-Job-Wait-Timeout / X-Job-Timeout."""
    return _float_header(request, "X-Job-Wait-Timeout"), _float_header(request, "X-Job-Timeout")


@contextmanager
def http_errors() -> Iterator[None]:
    """Перевод исключений очереди/менеджера в единый формат ошибок API."""
    try:
        yield
    except HTTPException:
        raise
    except JobCancelled as e:
        raise err(e.status, str(e), CANCEL_HTTP.get(e.status, 500))
    except ModelBusyError as e:
        raise err("model_busy", str(e), 409)
    except ModelNotFoundError as e:
        raise err("model_not_found", f"Модель '{e.args[0]}' не найдена в реестре", 404)
    except NotInstalledError as e:
        raise err("not_installed", str(e), 409)
    except WrongModelTypeError as e:
        raise err("wrong_model_type", str(e), 409)
    except BackendError as e:
        raise err("backend_error", str(e), 502)
    except RuntimeError as e:
        if str(e) == "NO_ACTIVE_MODEL":
            raise err("no_active_model",
                      "Нет активной модели подходящего типа. Укажите установленную модель в поле "
                      "'model' или запустите её: POST /api/models/{name}/start", 409)
        raise


# ---------- /api/jobs ----------

class JobSubmit(BaseModel):
    """Асинхронная задача: сразу возвращает id, результат — GET /api/jobs/{id}."""
    kind: Literal["chat", "image", "tts"]
    model: Optional[str] = None
    project: Optional[str] = None
    # chat — тело OpenAI /chat/completions (messages, temperature, max_tokens, ...);
    # image — поля ImageRequest; tts — поля TTSRequest
    payload: dict
    wait_timeout: Optional[float] = Field(default=None, gt=0)
    run_timeout: Optional[float] = Field(default=None, gt=0)


def _summary(kind: str, payload: dict) -> str:
    if kind == "chat":
        return chat_summary(payload)
    return str(payload.get("prompt") or payload.get("text") or "")[:300]


def build_queue_router(queue: JobQueue) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["queue"])

    @router.get("/queue")
    async def queue_state():
        """Что сейчас выполняется и кто ждёт (по моделям). switch_locked — переключение модели запрещено."""
        return queue.snapshot()

    @router.post("/jobs", status_code=202)
    async def submit(req: JobSubmit, request: Request):
        payload = dict(req.payload)
        if req.kind == "chat" and not payload.get("messages"):
            raise err("bad_request", "Для kind=chat нужно payload.messages", 400)
        try:
            if req.kind == "image":
                ImageRequest(**payload)
            elif req.kind == "tts":
                TTSRequest(**payload)
        except ValidationError as e:
            raise err("bad_request", str(e.errors(include_url=False))[:500], 400)
        project = req.project or project_of(request, payload)
        with http_errors():
            model = queue.resolve_model(req.kind, req.model or payload.get("model"))
            t = queue.submit(kind=req.kind, model=model, project=project, payload=payload,
                             summary=_summary(req.kind, payload),
                             wait_timeout=req.wait_timeout, run_timeout=req.run_timeout)
        return {"id": t.id, "status": "queued", "model": model, "project": project,
                "position": queue.position(t.id)}

    @router.get("/jobs")
    async def list_jobs(status: Optional[str] = None, project: Optional[str] = None,
                        model: Optional[str] = None, limit: int = 100):
        """История задач (без payload/result). status — один или несколько через запятую."""
        return {"jobs": queue.store.list(status=status, project=project, model=model, limit=limit)}

    @router.get("/jobs/stats")
    async def job_stats(hours: float = 24):
        """Сводка по проектам/моделям/статусам: число, среднее ожидание и выполнение."""
        return {"since_hours": hours, "rows": queue.store.stats(time.time() - hours * 3600)}

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str, include_result: bool = True):
        job = queue.store.get(job_id, with_result=include_result)
        if not job:
            raise err("job_not_found", f"Задача '{job_id}' не найдена", 404)
        job.pop("payload", None)
        if job["status"] == "queued":
            job["position"] = queue.position(job_id)
        return job

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        if queue.cancel(job_id):
            return {"id": job_id, "cancelling": True}
        job = queue.store.get(job_id, with_result=False)
        if not job:
            raise err("job_not_found", f"Задача '{job_id}' не найдена", 404)
        if job["status"] in TERMINAL:
            return {"id": job_id, "cancelling": False, "status": job["status"]}
        raise err("job_not_found", f"Задача '{job_id}' не активна в очереди", 404)

    return router
