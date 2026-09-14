"""Единые схемы запросов/ответов (стандартизация для всех ИИ)."""
from __future__ import annotations

from typing import List, Optional, Literal, Any

from pydantic import BaseModel, Field, field_validator, ConfigDict


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Стандартизированный текстовый запрос к любой текстовой модели."""
    messages: List[Message]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    stream: bool = False


class ImageRequest(BaseModel):
    """Стандартизированный запрос генерации изображения."""
    prompt: str = Field(min_length=1)
    negative_prompt: str = ""
    steps: int = Field(default=25, ge=1, le=150)
    width: int = 512
    height: int = 512
    seed: Optional[int] = None

    @field_validator("width", "height")
    @classmethod
    def _multiple_of_64(cls, v: int) -> int:
        if v % 64 != 0:
            raise ValueError("width/height должны быть кратны 64")
        if not 256 <= v <= 1024:
            raise ValueError("width/height должны быть в диапазоне 256..1024")
        return v


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """Стандартизированный ответ текстовой модели."""
    model: str
    backend: str
    content: str
    usage: Usage = Field(default_factory=Usage)
    # response: str  # Assuming this should be a field
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ImageResponse(BaseModel):
    """Стандартизированный ответ графической модели (PNG в base64)."""
    model: str
    backend: str
    width: int
    height: int
    steps: int
    seed: Optional[int] = None
    image_base64: str


class ModelInfo(BaseModel):
    name: str
    backend: str
    display_name: str = ""
    description: str = ""
    type: Literal["text", "image"] = "text"
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
    active_type: Optional[str] = None
    models_total: int = 0
    models_installed: int = 0
    models_running: int = 0


# update continue

# Определение модели запроса для continue
class ContinueRequest(BaseModel):
    prompt: str
    max_tokens: int = 16
    temperature: float = 1.0
    top_p: float = 1.0
    stop: Optional[list[str]] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

# Определение модели ответа для continue
class ContinueResponse(BaseModel):
    id: str
    object: str
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, int]

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody