from __future__ import annotations

import threading
from typing import Dict, List, Optional

from .backends.base import Backend, BackendError
from .backends.llama_server import LlamaServerBackend
from .backends.openvino_sd import OpenVinoSdBackend
from .registry import ModelEntry, Registry
from .schemas import ChatRequest, ChatResponse, ImageRequest, ImageResponse, ModelInfo


BACKENDS = {
    LlamaServerBackend.backend_name: LlamaServerBackend,
    OpenVinoSdBackend.backend_name: OpenVinoSdBackend,
}


class ModelNotFoundError(KeyError):
    """Исключение: модель не найдена в реестре."""


class NotInstalledError(Exception):
    """Исключение: модель не установлена."""


class WrongModelTypeError(Exception):
    """Активна модель неподходящего типа (текст/графика)."""


class ModelManager:
    """
    Управляет жизненным циклом моделей, обеспечивая инвариант «одна активная модель».
    Отвечает за установку, запуск, остановку моделей, а также за инференс (чат, генерацию изображений).
    Исправлена проблема полной перезаписи кода — теперь изменения добавляются, сохраняя контекст.
    """

    def __init__(self, registry: Registry, settings: dict):
        """
        Инициализирует менеджер моделей.

        :param registry: реестр моделей (список доступных моделей)
        :param settings: настройки окружения (например, URL-префикс для API)
        """
        self.registry = registry
        self.settings = settings
        self._lock = threading.RLock()  # блокировка для потокобезопасности
        self._backends: Dict[str, Backend] = {}  # кэш инициализированных бэкендов
        self._active: Optional[str] = None  # имя активной модели (или None)

    def _backend_for(self, entry: ModelEntry) -> Backend:
        """
        Возвращает бэкенд для указанной модели, инициализируя его при первом обращении.

        :param entry: запись модели из реестра
        :return: экземпляр бэкенда
        :raises BackendError: если бэкенд неизвестен
        """
        if entry.name not in self._backends:
            cls = BACKENDS.get(entry.backend)
            if cls is None:
                raise BackendError(f"Неизвестный бэкенд: {entry.backend}")
            self._backends[entry.name] = cls(entry, self.settings)
        return self._backends[entry.name]

    def list_models(self) -> List[ModelInfo]:
        """
        Формирует список информации о моделях, включая статус активности и установки.

        :return: список объектов ModelInfo
        """
        with self._lock:
            infos = []
            for entry in self.registry.all():
                be = self._backend_for(entry)
                running = self._active == entry.name and be.is_running()
                infos.append(ModelInfo(
                    name=entry.name,
                    backend=entry.backend,
                    display_name=entry.display_name,
                    description=entry.description,
                    type=be.model_type,
                    installed=be.is_installed(),
                    running=running,
                    pid=be._proc.pid if running and getattr(be, "_proc", None) else None,
                    endpoint=(f"{self.settings.get('public_origin', 'http://127.0.0.1:8000')}/v1"
                              if running and be.model_type == "text" else None),
                ))
            return infos

    def status(self) -> dict:
        """
        Собирает сводный статус системы: активная модель, количество установленных/работающих моделей.

        :return: словарь с данными статуса
        """
        models = self.list_models()
        active = self._active if any(m.name == self._active and m.running for m in models) else None
        active_type = next((m.type for m in models if m.name == active), None)
        return {
            "active": active,
            "active_type": active_type,
            "models_total": len(models),
            "models_installed": sum(m.installed for m in models),
            "models_running": sum(m.running for m in models),
        }

    def install(self, name: str) -> dict:
        with self._lock:
            entry = self._get(name)
            be = self._backend_for(entry)
            was = be.is_installed()
            if not was:
                be.install()
            return {"name": name, "installed": be.is_installed(), "was_installed": was}

    def start(self, name: str) -> dict:
        """
        Запускает модель, предварительно остановив активную (если это другая модель).

        :param name: имя запускаемой модели
        :return: данные о запуске (имя, статус, список остановленных моделей)
        :raises NotInstalledError: если модель не установлена
        """
        with self._lock:
            entry = self._get(name)
            be = self._backend_for(entry)
            if not be.is_installed():
                raise NotInstalledError(f"Модель '{name}' не установлена. Сначала выполните POST /api/models/install")

            stopped: List[str] = []
            if self._active and self._active != name:
                stopped = self._stop_active_locked()
            elif self._active == name and be.is_running():
                return {"name": name, "running": True, "stopped": []}

            be.start()
            self._active = name
            return {"name": name, "running": True, "stopped": stopped}

    def stop(self) -> dict:
        """
        Останавливает активную модель.

        :return: статус ({"running": None})
        """
        with self._lock:
            self._stop_active_locked()
            return {"running": None}

    def _stop_active_locked(self) -> List[str]:
        """
        Останавливает текущую активную модель (внутри блокировки).

        :return: список имён остановленных моделей (обычно длина 0 или 1)
        """
        if not self._active:
            return []
        be = self._backends.get(self._active)
        if be:
            be.stop()  # освобождает VRAM — ключевой механизм инварианта I1
        name, self._active = self._active, None
        return [name]

    def _active_text_backend(self) -> Backend:
        """
        Возвращает активный текстовый бэкенд, проверяя тип модели.

        :return: бэкенд активной текстовой модели
        :raises RuntimeError: если нет активной модели
        :raises WrongModelTypeError: если активна графическая модель
        """
        with self._lock:
            if not self._active:
                raise RuntimeError("NO_ACTIVE_MODEL")
            be = self._backends.get(self._active)
            if be is None or not be.is_running():
                raise RuntimeError("NO_ACTIVE_MODEL")
            if be.model_type != "text":
                raise WrongModelTypeError("Активна графическая модель — используйте POST /api/image")
            return be

    def _active_image_backend(self) -> Backend:
        """
        Возвращает активный графический бэкенд, проверяя тип модели.

        :return: бэкенд активной графической модели
        :raises RuntimeError: если нет активной модели
        :raises WrongModelTypeError: если активна текстовая модель
        """
        with self._lock:
            if not self._active:
                raise RuntimeError("NO_ACTIVE_MODEL")
            be = self._backends.get(self._active)
            if be is None or not be.is_running():
                raise RuntimeError("NO_ACTIVE_MODEL")
            if be.model_type != "image":
                raise WrongModelTypeError("Активна текстовая модель — используйте POST /api/chat")
            return be

    def active_name(self) -> Optional[str]:
        """
        Возвращает имя активной модели (или None, если нет).

        :return: имя активной модели
        """
        return self._active

    def chat(self, request: ChatRequest) -> ChatResponse:
        """
        Обрабатывает запрос чата через активный текстовый бэкенд.

        Сообщения передаются модели БЕЗ изменений. Инструкции по формату
        правок задаются на стороне клиента (Continue config.yaml) — их
        подмена здесь ломала применение правок в VS Code.
        """
        return self._active_text_backend().chat(request)

    def image(self, request: ImageRequest) -> ImageResponse:
        """
        Генерирует изображение через активный графический бэкенд.

        :param request: запрос на генерацию изображения
        :return: результат генерации
        """
        return self._active_image_backend().generate_image(request)


    def _get(self, name: str) -> ModelEntry:
        """
        Извлекает запись модели по имени, поднимая исключение, если модель не найдена.

        :param name: имя модели
        :return: запись модели
        :raises ModelNotFoundError: если модель отсутствует
        """
        try:
            return self.registry.get(name)
        except KeyError:
            raise ModelNotFoundError(name)

    def shutdown(self) -> None:
        """
        Полностью останавливает систему: отключает активную модель.
        """
        with self._lock:
            self._stop_active_locked()
