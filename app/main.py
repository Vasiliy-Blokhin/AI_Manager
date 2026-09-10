"""AI Manager Service — FastAPI: веб-интерфейс + REST API."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .manager import (BACKENDS, ModelManager, ModelNotFoundError,
                      NotInstalledError)
from .backends.base import BackendError
from .registry import Registry, RegistryError
from .schemas import (ChatRequest, ChatResponse, ErrorResponse, InstallRequest,
                      InstallResponse, ModelInfo, ModelListResponse,
                      StartResponse, StatusResponse, StopResponse)

BASE_DIR = Path(__file__).resolve().parent


def _settings() -> dict:
    lo, _, hi = os.getenv("AIM_PORT_RANGE", "8100-8199").partition("-")
    return {
        "ollama_url": os.getenv("AIM_OLLAMA_URL", "http://127.0.0.1:11434"),
        "llama_server_bin": os.getenv("LLAMA_SERVER_BIN", "llama-server"),
        "hf_cache": os.getenv("AIM_HF_CACHE", "~/.cache/ai-manager"),
        "port_range": (int(lo), int(hi or lo)),
    }


def _err(code: str, msg: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


registry = Registry(os.getenv("AIM_REGISTRY",
                    str(BASE_DIR.parent / "config" / "models.registry.json")))
manager = ModelManager(registry, _settings())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Проверка наличия установленных ИИ по списку; при AIM_AUTO_INSTALL=true
    # отсутствующие модели скачиваются и устанавливаются автоматически.
    auto = os.getenv("AIM_AUTO_INSTALL", "false").lower() in ("1", "true", "yes")
    for entry in registry.all():
        try:
            if not manager._backend_for(entry).is_installed():
                if auto:
                    manager.install(entry.name)
        except BackendError:
            pass  # бэкенд (например Ollama) может быть временно недоступен
    yield
    manager.shutdown()


app = FastAPI(title="AI Manager Service", version="1.0.0", lifespan=lifespan)


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
    return ErrorResponse(error=detail).model_dump(), exc.status_code


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
