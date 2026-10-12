"""Абстрактный бэкенд: интерфейс для всех провайдеров ИИ."""
from __future__ import annotations

import abc
from dataclasses import dataclass

from ..registry import ModelEntry
from ..schemas import (ChatRequest, ChatResponse, ImageRequest, ImageResponse,
                       TTSRequest, TTSResponse)


@dataclass
class StartInfo:
    """Информация о запущенной модели."""
    pid: int
    endpoint: str


class BackendError(Exception):
    """Базовая ошибка бэкенда (502 наружу)."""


def download_with_retries(fn, what: str, attempts: int = 10, pause: float = 30.0):
    """
    Повторяет загрузку при сетевых сбоях (обрыв соединения, DNS, ошибки CDN HuggingFace).
    huggingface_hub пропускает уже скачанные файлы и докачивает незавершённые,
    поэтому каждый повтор продолжает с места обрыва, а не начинает заново.

    После первого сбоя протокол Xet (CDN HuggingFace) отключается: на нестабильной сети
    он падает с «CAS Client Error» и не умеет докачивать, а обычный HTTP продолжает
    файл с места обрыва (Range-запросы).
    """
    import logging
    import time
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # сетевые ошибки HF бывают разных типов
            last = e
            logging.getLogger(__name__).warning(
                "Загрузка %s: попытка %d/%d не удалась: %s", what, attempt, attempts, str(e)[:300])
            try:
                from huggingface_hub import constants
                if not constants.HF_HUB_DISABLE_XET:
                    constants.HF_HUB_DISABLE_XET = True
                    logging.getLogger(__name__).warning("Xet отключён — дальше обычная HTTP-загрузка с докачкой")
            except Exception:
                pass
            if attempt < attempts:
                time.sleep(pause * min(attempt, 4))
    raise BackendError(f"Не удалось скачать {what} за {attempts} попыток: {str(last)[:500]}")


class Backend(abc.ABC):
    """Каждый бэкенд привязан к одной записи реестра (модели)."""

    backend_name: str = "abstract"
    supports_images: bool = False  # False = текстовая модель
    supports_audio: bool = False   # True = синтез речи (TTS)

    def __init__(self, entry: ModelEntry, settings: dict):
        self.entry = entry
        self.settings = settings

    @property
    def model_type(self) -> str:
        if self.supports_audio:
            return "audio"
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

    def synthesize(self, request: TTSRequest) -> TTSResponse:
        raise BackendError(f"Бэкенд {self.backend_name} не поддерживает синтез речи")

    def save_code(self, code: str) -> None:
        raise BackendError(f"Бэкенд {self.backend_name} не поддерживает сохранение кода")

    def get_code(self) -> str:
        raise BackendError(f"Бэкенд {self.backend_name} не поддерживает получение кода")