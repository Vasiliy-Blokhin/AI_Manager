"""AI Manager Service v2 — FastAPI: веб + REST API + авторизация по паролю (.env)."""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from .backends.base import BackendError
from .manager import (ModelManager, ModelNotFoundError, NotInstalledError,
                      WrongModelTypeError)
from .registry import Registry, RegistryError
from .schemas import (ChatRequest, ChatResponse, ErrorResponse, ImageRequest,
                      ImageResponse, InstallRequest, InstallResponse, ModelInfo,
                      ModelListResponse, StartResponse, StatusResponse,
                      StopResponse)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")  # пароль и настройки из .env

PASSWORD = os.getenv("AIM_PASSWORD", "")
if not PASSWORD:
    raise RuntimeError("Задайте AIM_PASSWORD в файле .env (см. .env.example)")


def _settings() -> dict:
    lo, _, hi = os.getenv("AIM_PORT_RANGE", "8100-8199").partition("-")
    hf_cache = os.path.expandvars(os.getenv("AIM_HF_CACHE", "~/.cache/ai-manager"))
    os.environ.setdefault("HF_HOME", str(Path(hf_cache).expanduser()))
    return {
        "llama_server_bin": os.path.expandvars(os.getenv("LLAMA_SERVER_BIN", "llama-server")),
        "hf_cache": hf_cache,
        "port_range": (int(lo), int(hi or lo)),
        "gpu_layers": int(os.getenv("AIM_GPU_LAYERS", "99")),
    }


def _err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


registry = Registry(os.path.expandvars(os.getenv(
    "AIM_REGISTRY", str(BASE_DIR.parent / "config" / "models.registry.json"))))
manager = ModelManager(registry, _settings())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Проверка наличия установленных ИИ по списку; при AIM_AUTO_INSTALL=true
    # отсутствующие модели скачиваются и устанавливаются автоматически.
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


app = FastAPI(title="AI Manager Service", version="2.0.0", lifespan=lifespan)


# ---------- Авторизация: проверка пароля до обработки эндпоинтов (инвариант I4) ----------
@app.middleware("http")
async def password_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        provided = request.headers.get("X-API-Password", "")
        if not hmac.compare_digest(provided.encode(), PASSWORD.encode()):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized",
                                   "message": "Неверный или отсутствующий пароль (заголовок X-API-Password)"}},
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
    if req.stream:
        raise _err("unsupported", "stream=true не поддерживается в этой версии", 400)
    try:
        return manager.chat(req)
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


# ---------- Веб-интерфейс ----------

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# Единый формат ошибок {"error": {...}}
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
