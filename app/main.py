"""AI Manager Service v2.3 — FastAPI: REST API + OpenAI-прокси (/v1) + очередь задач (SQLite) + авторизация по паролю (.env)."""
from __future__ import annotations

import os
import sys
import hmac
import socket
import signal
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Антивирус (Kaspersky) проверяет HTTPS своим корневым сертификатом из хранилища Windows.
# Python по умолчанию его не знает (certifi) — без этого загрузка моделей с HuggingFace падает
# с CERTIFICATE_VERIFY_FAILED. truststore заставляет Python доверять системному хранилищу, как браузер.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from dotenv import load_dotenv
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .backends.base import BackendError
from .executors import backend_of, exec_chat, register_all
from .jobs import JobQueue, JobStore, Ticket
from .manager import ModelManager, ModelNotFoundError, NotInstalledError
from .openai_proxy import build_openai_router
from .queue_api import build_queue_router, http_errors, project_of, timeouts_of
from .registry import Registry, RegistryError
from .watchdog import Watchdog
from .orphans import OrphanSweeper
from .schemas import (ChatRequest, ChatResponse, ErrorResponse, ImageRequest,
                      ImageResponse, InstallRequest, InstallResponse, ModelInfo,
                      ModelListResponse, StartResponse, StatusResponse,
                      StopResponse, TTSRequest, TTSResponse)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")

PASSWORD = os.getenv("AIM_PASSWORD", "")
if not PASSWORD:
    raise RuntimeError("Задайте AIM_PASSWORD в файле .env (см. .env.example)")

import logging
logger = logging.getLogger(__name__)


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _public_origin() -> str:
    host = os.getenv("AIM_PUBLIC_HOST", "").strip()
    if not host:
        host = os.getenv("AIM_HOST", "127.0.0.1").strip() or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = _lan_ip()
    return f"http://{host}:{os.getenv('AIM_PORT', '8000')}"


def _settings() -> dict:
    lo, _, hi = os.getenv("AIM_PORT_RANGE", "8100-8199").partition("-")
    hf_cache = os.path.expandvars(os.getenv("AIM_HF_CACHE", "~/.cache/ai-manager"))
    os.environ.setdefault("HF_HOME", str(Path(hf_cache).expanduser()))
    return {
        "llama_server_bin": os.path.expandvars(os.getenv("LLAMA_SERVER_BIN", "llama-server")),
        "hf_cache": hf_cache,
        "port_range": (int(lo), int(hi or lo)),
        "gpu_layers": int(os.getenv("AIM_GPU_LAYERS", "99")),
        # Авто-запуск модели из поля "model" запроса и перезапуск упавшего llama-server
        "autostart": os.getenv("AIM_AUTOSTART", "true").lower() in ("1", "true", "yes"),
        "public_origin": _public_origin(),
        # Очередь задач: сколько ждать в очереди / сколько выполнять (сек), хранение истории (дни)
        "job_wait_timeout": float(os.getenv("AIM_JOB_WAIT_TIMEOUT", "3600")),
        "job_run_timeout": float(os.getenv("AIM_JOB_RUN_TIMEOUT", "3600")),
        "job_retention_days": float(os.getenv("AIM_JOB_RETENTION_DAYS", "14")),
        # Зависания: срок запуска модели; сколько ждать завершения потока после отмены/таймаута;
        # сколько один шаг генерации картинки может длиться (0 — не проверять). Затем — перезапуск.
        "model_start_timeout": float(os.getenv("AIM_MODEL_START_TIMEOUT", "1200")),
        "stuck_grace": float(os.getenv("AIM_STUCK_GRACE", "300")),
        "image_stall_timeout": float(os.getenv("AIM_IMAGE_STALL_TIMEOUT", "600")),
    }


def _err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


registry = Registry(os.path.expandvars(os.getenv(
    "AIM_REGISTRY", str(PROJECT_ROOT / "config" / "models.registry.json"))))
SETTINGS = _settings()
manager = ModelManager(registry, SETTINGS)
store = JobStore(os.path.expandvars(os.getenv("AIM_DB", str(PROJECT_ROOT / "data" / "ai_manager.db"))))
queue = JobQueue(manager, store, SETTINGS)
register_all(queue)
watchdog = Watchdog(PROJECT_ROOT / "logs", int(os.getenv("AIM_PORT", "8000")), manager.shutdown)
queue.on_hang = watchdog.restart
# llama-server, оставшиеся от упавшего экземпляра сервиса, держат память и порты — убираем
orphans = OrphanSweeper(float(os.getenv("AIM_ORPHAN_INTERVAL", "300")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    auto = os.getenv("AIM_AUTO_INSTALL", "false").lower() in ("1", "true", "yes")
    for entry in registry.all():
        try:
            be = manager._backend_for(entry)
            if not be.is_installed() and auto:
                manager.install(entry.name)
        except BackendError:
            pass
    orphans.start()
    await queue.start()
    await watchdog.start()
    yield
    orphans.stop()
    await watchdog.stop()
    await queue.stop()
    manager.shutdown()
    store.close()


app = FastAPI(title="AI Manager Service", version="2.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _provided_password(request: Request) -> str:
    header = request.headers.get("X-API-Password", "")
    if header:
        return header
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


@app.middleware("http")
async def password_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") or request.url.path.startswith("/v1/"):
        provided = _provided_password(request)
        if not hmac.compare_digest(provided.encode(), PASSWORD.encode()):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized",
                                   "message": "Неверный или отсутствующий пароль "
                                              "(X-API-Password или Authorization: Bearer)"}}
            )
    return await call_next(request)


# ---------- API ----------

@app.get("/health", include_in_schema=False)
async def health():
    """Живость сервиса без пароля: отвечает сам event loop (для сторожей)."""
    return {"status": "ok"}


@app.get("/api/models", response_model=ModelListResponse)
def list_models():
    st = manager.status()
    return ModelListResponse(active=st["active"], models=manager.list_models())


@app.get("/api/models/{name}", response_model=ModelInfo)
def get_model(name: str):
    for m in manager.list_models():
        if m.name == name:
            return m
    raise _err("model_not_found", f"Модель '{name}' не найдена в реестре", 404)


@app.post("/api/models/install", response_model=InstallResponse)
def install(req: InstallRequest):
    try:
        return InstallResponse(**manager.install(req.name))
    except ModelNotFoundError:
        raise _err("model_not_found", f"Модель '{req.name}' не найдена в реестре", 404)
    except BackendError as e:
        raise _err("install_failed", str(e), 502)


@app.post("/api/models/{name}/start", response_model=StartResponse)
async def start(name: str):
    """Ручной запуск. 409 model_busy, если по активной модели есть задачи."""
    try:
        manager._get(name)
        if name == queue.current_model() and queue.busy_with(name):
            # модель уже работает и занята задачами — переключения нет, это не ошибка
            return StartResponse(name=name, running=True, stopped=[])
        with http_errors():
            return StartResponse(**await queue.manual(manager.start, name))
    except ModelNotFoundError:
        raise _err("model_not_found", f"Модель '{name}' не найдена в реестре", 404)
    except NotInstalledError as e:
        raise _err("not_installed", str(e), 409)
    except BackendError as e:
        raise _err("start_failed", str(e), 502)


@app.post("/api/models/stop", response_model=StopResponse)
async def stop():
    """Ручная остановка. 409 model_busy, если по активной модели есть задачи."""
    with http_errors():
        return StopResponse(**await queue.manual(manager.stop))


@app.get("/api/status", response_model=StatusResponse)
async def status():
    st = await run_in_threadpool(manager.status)
    snap = queue.snapshot()
    return StatusResponse(**st, queue_running=len(snap["running"]),
                          queue_waiting=len(snap["waiting"]), switch_locked=snap["switch_locked"])


async def _queued(request: Request, kind: str, model: Optional[str], summary: str) -> Ticket:
    """Ставит синхронный запрос в очередь и ждёт её (после возврата нужная модель запущена)."""
    wait_timeout, run_timeout = timeouts_of(request)
    t = queue.new_ticket(kind=kind, model=queue.resolve_model(kind, model),
                         project=project_of(request), summary=summary,
                         wait_timeout=wait_timeout, run_timeout=run_timeout)
    await queue.acquire(t, request.is_disconnected)
    return t


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request, response: Response):
    if req.stream:
        raise _err("unsupported", "stream=true не поддерживается в /api/chat — используйте /v1/chat/completions", 400)
    payload = {"messages": [m.model_dump() for m in req.messages], "temperature": req.temperature}
    if req.max_tokens:
        payload["max_tokens"] = req.max_tokens
    with http_errors():
        t = await _queued(request, "chat", req.model, req.messages[-1].content if req.messages else "")
        response.headers["X-Job-Id"] = t.id
        result = await queue.execute(t, exec_chat(queue, t, payload), usage=lambda r: r["usage"])
    return ChatResponse(model=t.model, backend=backend_of(queue, t).backend_name,
                        content=result["content"], usage=result["usage"] or {})


@app.post("/api/image", response_model=ImageResponse)
async def image(req: ImageRequest, request: Request, response: Response):
    with http_errors():
        t = await _queued(request, "image", req.model, req.prompt)
        response.headers["X-Job-Id"] = t.id
        return await queue.execute(t, queue.run_blocking(t, backend_of(queue, t).generate_image, req,
                                                         t.cancel_event))


@app.post("/api/tts", response_model=TTSResponse)
async def tts(req: TTSRequest, request: Request, response: Response):
    with http_errors():
        t = await _queued(request, "tts", req.model, req.text)
        response.headers["X-Job-Id"] = t.id
        return await queue.execute(t, queue.run_blocking(t, backend_of(queue, t).synthesize, req))


# ---------- Перезапуск: git pull + рестарт ТОЛЬКО этого процесса ----------

@app.post("/api/restart")
def restart_service(force: bool = False):
    """
    git pull + перезапуск сервиса. Перезапускается только процесс AI Manager
    (по PID текущего процесса) — другие Python-процессы не затрагиваются.
    Пока в очереди есть задачи — 409 queue_busy (force=true — перезапустить всё равно).
    """
    snap = queue.snapshot()
    busy = len(snap["running"]) + len(snap["waiting"])
    if busy and not force:
        raise _err("queue_busy", f"В очереди задач: {busy}. Дождитесь их или POST /api/restart?force=true", 409)
    result = {"git": None, "restarting": True}

    try:
        p = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=120,
        )
        result["git"] = {
            "ok": p.returncode == 0,
            "output": (p.stdout + p.stderr).strip()[-500:],
        }
    except Exception as e:
        result["git"] = {"ok": False, "output": str(e)}

    def _delayed_restart():
        time.sleep(1.5)
        logger.info("Перезапуск AI Manager (PID %s)...", os.getpid())
        if hasattr(signal, "SIGTERM"):
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os._exit(1)

    threading.Thread(target=_delayed_restart, daemon=True).start()
    return result


# ---------- OpenAI-совместимый прокси (/v1) ----------

app.include_router(build_openai_router(queue))
app.include_router(build_queue_router(queue))

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(ValidationError)
async def validation_exc_handler(request: Request, exc: ValidationError):
    return JSONResponse(status_code=400,
                        content={"error": {"code": "bad_request",
                                           "message": exc.errors(include_url=False).__str__()[:500]}})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app",
                host=os.getenv("AIM_HOST", "127.0.0.1"),
                port=int(os.getenv("AIM_PORT", "8000")))