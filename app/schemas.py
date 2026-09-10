"""Единые схемы запросов/ответов (стандартизация для всех ИИ)."""
from __future__ import annotations

from typing import List, Optional, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Стандартизированный запрос к любой модели (OpenAI-совместимое подмножество)."""
    messages: List[Message]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    stream: bool = False


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """Стандартизированный ответ от любой модели."""
    model: str
    backend: str
    content: str
    usage: Usage = Field(default_factory=Usage)


class ModelInfo(BaseModel):
    name: str
    backend: str
    display_name: str = ""
    description: str = ""
    installed: bool = False
    running: bool = False
    pid: Optional[int] = None
    endpoint: Optional[str] = None


class ModelListResponse(BaseModel):
    active: Optional[str] = None
    models: List[ModelInfo]


class InstallRequest(BaseModel):
    name: str


class InstallResponse(BaseModel):
    name: str
    installed: bool
    was_installed: bool


class StartResponse(BaseModel):
    name: str
    running: bool
    stopped: List[str] = Field(default_factory=list)


class StopResponse(BaseModel):
    running: Optional[str] = None


class StatusResponse(BaseModel):
    service: str = "ai-manager"
    active: Optional[str] = None
    models_total: int = 0
    models_installed: int = 0
    models_running: int = 0


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
