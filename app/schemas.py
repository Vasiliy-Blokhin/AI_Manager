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
    # Имя модели из реестра: запустится сама, когда до задачи дойдёт очередь.
    # None — текущая активная модель.
    model: Optional[str] = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    stream: bool = False


class ImageRequest(BaseModel):
    """Стандартизированный запрос генерации изображения."""
    prompt: str = Field(min_length=1)
    model: Optional[str] = None  # None — активная графическая модель
    negative_prompt: str = ""
    steps: int = Field(default=25, ge=1, le=150)
    width: int = 512
    height: int = 512
    seed: Optional[int] = None
    # None — значение по умолчанию модели (FLUX.1-schnell: 0.0, SD/SDXL: 7.5)
    guidance_scale: Optional[float] = Field(default=None, ge=0.0, le=30.0)
    # «Картинка → картинка»: исходное изображение (PNG/JPEG в base64) и сила изменения.
    # strength 0.2–0.4 — стиль и детали при сохранении композиции; 0.8+ — почти новая картинка.
    # Исходник приводится к width×height (обрезка по центру).
    init_image_base64: Optional[str] = None
    strength: float = Field(default=0.35, ge=0.05, le=1.0)

    @field_validator("width", "height")
    @classmethod
    def _multiple_of_64(cls, v: int) -> int:
        if v % 64 != 0:
            raise ValueError("width/height должны быть кратны 64")
        if not 256 <= v <= 1536:
            raise ValueError("width/height должны быть в диапазоне 256..1536")
        return v


class TTSRequest(BaseModel):
    """Стандартизированный запрос синтеза речи."""
    text: str = Field(min_length=1, max_length=4000)
    model: Optional[str] = None  # None — активная TTS-модель
    language: str = "ru"
    # Имя файла-образца голоса из папки voices/ (wav/mp3, 5–15 с) — клонирование голоса.
    # None — голос модели по умолчанию.
    speaker: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    # Сид сэмплирования: другой сид — другой «дубль» той же фразы (для перегенерации неудачных)
    seed: Optional[int] = Field(default=None, ge=0)


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


class TTSResponse(BaseModel):
    """Стандартизированный ответ синтеза речи (WAV в base64)."""
    model: str
    backend: str
    format: Literal["wav"] = "wav"
    duration_sec: float = 0.0
    audio_base64: str


class ModelInfo(BaseModel):
    name: str
    backend: str
    display_name: str = ""
    description: str = ""
    type: Literal["text", "image", "audio"] = "text"
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
    queue_running: int = 0
    queue_waiting: int = 0
    switch_locked: bool = False  # по текущей модели есть задачи — переключение запрещено


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody