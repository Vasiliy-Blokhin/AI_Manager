# app/main.py
import os
import time
from fastapi import FastAPI, Request, HTTPException, status, Depends, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import subprocess

from .manager import ModelManager
from .openai_proxy import openai_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управление жизненным циклом приложения:
    - Инициализирует менеджер моделей при старте (загружает реестр).
    - Гарантирует выгрузку всех моделей из VRAM Intel Arc при остановке сервиса,
      предотвращая утечки видеопамяти и зависания драйвера.
    """
    manager = ModelManager()
    app.state.manager = manager
    print("[SYSTEM] AI Manager initialized.")
    yield
    await manager.stop_all_models()
    print("[SYSTEM] All models unloaded from VRAM.")

app = FastAPI(
    title="AI Manager",
    description="Централизованное управление локальными ИИ-моделями для Intel Arc B580.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None
)

# Настройка CORS необходима для того, чтобы веб-интерфейс или агент VS Code могли 
# отправлять запросы без блокировки браузером/IDE политикой Same-Origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1", "https://vscode.dev"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Password"]
)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Кастомный обработчик ошибок.
    Устанавливает charset=utf-8, чтобы переносы строк (\n) в сообщениях об ошибках
    не экранировались расширением Continue/Cline. Это критически важно для предотвращения
    бага, когда старый код удаляется и заменяется пустой строкой.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=exc.headers,
        media_type="application/json; charset=utf-8"
    )

def verify_password(x_api_password: str = Header(...)):
    """
    Зависимость FastAPI для проверки пароля доступа к API.
    Сравнивает заголовок X-API-Password со значением из переменной окружения AIM_PASSWORD.
    """
    env_pass = os.getenv("AIM_PASSWORD")
    if not env_pass or x_api_password != env_pass:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Password"
        )

# Подключение OpenAI-совместимого роутера с глобальной проверкой авторизации
app.include_router(openai_router, prefix="/v1", dependencies=[Depends(verify_password)])

@app.get("/health")
async def health_check():
    """Endpoint для проверки доступности сервиса (используется Docker/K8s Health Check)."""
    return {"status": "ok"}


@app.post("/restart", dependencies=[Depends(verify_password)])
async def restart_service():
    """Endpoint для перезапуска сервиса."""
    subprocess.run(["git", "pull"])
    subprocess.run(["scripts/restart.bat"])
    return {"message": "Service is restarting"}