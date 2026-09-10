"""Абстрактный бэкенд: интерфейс для всех провайдеров ИИ."""
from __future__ import annotations

import abc
from dataclasses import dataclass

from ..registry import ModelEntry
from ..schemas import ChatRequest, ChatResponse, ImageRequest, ImageResponse


@dataclass
class StartInfo:
    """Информация о запущенной модели."""
    pid: int
    endpoint: str


class BackendError(Exception):
    """Базовая ошибка бэкенда (502 наружу)."""


class Backend(abc.ABC):
    """Каждый бэкенд привязан к одной записи реестра (модели)."""

    backend_name: str = "abstract"
    supports_images: bool = False  # False = текстовая модель

    def __init__(self, entry: ModelEntry, settings: dict):
        self.entry = entry
        self.settings = settings

    @property
    def model_type(self) -> str:
        return "image" if self.supports_images else "text"

    # --- жизненный цикл ---
    @abc.abstractmethod
    def is_installed(self) -> bool:
        """Проверить, установлена ли модель локально."""

    @abc.abstractmethod
    def install(self) -> None:
        """Скачать и установить модель. Бросает BackendError при неудаче."""

    @abc.abstractmethod
    def start(self) -> StartInfo:
        """Запустить модель и вернуть pid + endpoint."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Остановить модель (идемпотентно), освободить VRAM."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """Проверить, что модель реально работает."""

    # --- инференс ---
    def chat(self, request: ChatRequest) -> ChatResponse:
        raise BackendError(f"Бэкенд {self.backend_name} не поддерживает текстовые запросы")

    def generate_image(self, request: ImageRequest) -> ImageResponse:
        raise BackendError(f"Бэкенд {self.backend_name} не поддерживает генерацию изображений")
