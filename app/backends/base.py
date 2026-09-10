"""Абстрактный бэкенд: интерфейс для всех провайдеров ИИ."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from ..registry import ModelEntry
from ..schemas import ChatRequest, ChatResponse


@dataclass
class StartInfo:
    """Информация о запущенном процессе модели."""
    pid: int
    endpoint: str


class BackendError(Exception):
    """Базовая ошибка бэкенда (502 наружу)."""


class Backend(abc.ABC):
    """Каждый бэкенд привязан к одной записи реестра (модели)."""

    backend_name: str = "abstract"

    def __init__(self, entry: ModelEntry, settings: dict):
        self.entry = entry
        self.settings = settings

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
        """Остановить модель (идемпотентно)."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """Проверить, что модель реально отвечает."""

    # --- инференс ---
    @abc.abstractmethod
    def chat(self, request: ChatRequest) -> ChatResponse:
        """Стандартизированный запрос; вернуть ChatResponse."""
