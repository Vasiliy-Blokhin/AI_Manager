"""AI Manager Service v2.1 — FastAPI: веб + REST API + OpenAI-прокси (/v1) + авторизация по паролю (.env)."""
from __future__ import annotations

import subprocess
import hmac
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import time
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from .backends.base import BackendError
from .manager import (ModelManager, ModelNotFoundError, NotInstalledError,
                      WrongModelTypeError)
from .openai_proxy import build_openai_router
from .registry import Registry, RegistryError
from .schemas import (ChatRequest, ChatResponse, ErrorResponse, ImageRequest,
                      ImageResponse, InstallRequest, InstallResponse, ModelInfo,
                      ModelListResponse, StartResponse, StatusResponse,
                      StopResponse, ContinueResponse, ContinueRequest)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")  # пароль и настройки из .env

PASSWORD = os.getenv("AIM_PASSWORD", "")
if not PASSWORD:
    raise RuntimeError("Задайте AIM_PASSWORD в файле .env (см. .env.example)")

import logging

logger = logging.getLogger(__name__)

def _lan_ip() -> str:
    """IP-адрес в локальной сети (UDP-сокет ничего не отправляет — работает офлайн)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def _public_origin() -> str:
    """Базовый URL сервиса, который видят другие устройства (для endpoint/инструкций)."""
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
        "public_origin": _public_origin(),
    }

def _err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})

registry = Registry(os.path.expandvars(os.getenv(
    "AIM_REGISTRY", str(BASE_DIR.parent / "config" / "models.registry.json"))))
manager = ModelManager(registry, _settings())

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
    yield
    manager.shutdown()

app = FastAPI(title="AI Manager Service", version="2.1.0", lifespan=lifespan)

# CORS: разрешаем обращения из браузерных инструментов к API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Авторизация (инвариант I4): X-API-Password ИЛИ Bearer <пароль> ----------

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
def start(name: str):
    try:
        return StartResponse(**manager.start(name))
    except ModelNotFoundError:
        raise _err("model_not_found", f"Модель '{name}' не найдена в реестре", 404)
    except NotInstalledError as e:
        raise _err("not_installed", str(e), 409)
    except BackendError as e:
        raise _err("start_failed", str(e), 502)

@app.post("/api/models/stop", response_model=StopResponse)
def stop():
    return StopResponse(**manager.stop())

@app.get("/api/status", response_model=StatusResponse)
def status():
    return StatusResponse(**manager.status())

@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    logger.info(f"Chat request: {req.model_dump_json()}")
    if req.stream:
        raise _err("unsupported", "stream=true не поддерживается в /api/chat — используйте /v1/chat/completions", 400)
    try:
        logger.info(f"Chat request: {req.model_dump_json()}")
        response = manager.chat(req)
        return ChatResponse(model=response.model, backend=response.backend, content=response.content, usage=response.usage)
    except RuntimeError as e:
        if str(e) == "NO_ACTIVE_MODEL":
            raise _err("no_active_model",
                       "Нет активной модели. Запустите одну: POST /api/models/{name}/start", 409)
        raise
    except WrongModelTypeError as e:
        raise _err("wrong_model_type", str(e), 409)
    except BackendError as e:
        raise _err("backend_error", str(e), 502)

@app.post("/api/image", response_model=ImageResponse)
def image(req: ImageRequest):
    try:
        return manager.image(req)
    except RuntimeError as e:
        if str(e) == "NO_ACTIVE_MODEL":
            raise _err("no_active_model",
                       "Нет активной модели. Запустите одну: POST /api/models/{name}/start", 409)
        raise
    except WrongModelTypeError as e:
        raise _err("wrong_model_type", str(e), 409)
    except BackendError as e:
        raise _err("backend_error", str(e), 502)

# Обработчик POST-запросов на путь /v1/completions
@app.post("/v1/completions", response_model=ContinueResponse)
def completions(req: ContinueRequest):
    try:
        # Преобразуем запрос в формат, который поддерживает ваш менеджер моделей
        chat_request = ChatRequest(
            messages=[{"role": "user", "content": req.prompt}],
            stream=False,
            temperature=req.temperature,
            top_p=req.top_p,
            max_tokens=req.max_tokens,
            stop=req.stop
        )
        # Получаем ответ от менеджера моделей
        chat_response = manager.chat(chat_request)
        # Преобразуем ChatResponse в ContinueResponse
        continue_response = ContinueResponse(
            id="cmpl-1234567890",
            object="text_completion",
            created=int(time.time()),
            model=chat_response.model,
            choices=[{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": chat_response.content
                },
                "finish_reason": "stop"
            }],
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30
            }
        )
        return continue_response
    except RuntimeError as e:
        if str(e) == "NO_ACTIVE_MODEL":
            raise _err("no_active_model",
                       "Нет активной модели. Запустите одну: POST /api/models/{name}/start", 409)
        raise
    except WrongModelTypeError as e:
        raise _err("wrong_model_type", str(e), 409)
    except BackendError as e:
        raise _err("backend_error", str(e), 502)


@app.post("/restart")
async def restart_service():
    """Endpoint для перезапуска сервиса."""
    subprocess.run(["git", "pull"])
    subprocess.run(["scripts/restart.bat"])
    return {"message": "Service is restarting"}

# ---------- OpenAI-совместимый прокси (/v1) для Continue и др. ----------
app.include_router(build_openai_router(manager))


# ---------- Веб-интерфейс ----------

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")

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
