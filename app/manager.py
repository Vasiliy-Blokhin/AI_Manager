"""ModelManager: жизненный цикл моделей, контроль инварианта «одна активная модель»."""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from .backends.base import Backend, BackendError
from .backends.llama_server import LlamaServerBackend
from .backends.ollama import OllamaBackend
from .registry import ModelEntry, Registry
from .schemas import ChatRequest, ChatResponse, ModelInfo

BACKENDS = {
    OllamaBackend.backend_name: OllamaBackend,
    LlamaServerBackend.backend_name: LlamaServerBackend,
}


class ModelNotFoundError(KeyError):
    pass


class NotInstalledError(Exception):
    pass


class ModelManager:
    def __init__(self, registry: Registry, settings: dict):
        self.registry = registry
        self.settings = settings
        self._lock = threading.RLock()
        self._backends: Dict[str, Backend] = {}
        self._active: Optional[str] = None

    # --- бэкенды ---
    def _backend_for(self, entry: ModelEntry) -> Backend:
        if entry.name not in self._backends:
            cls = BACKENDS.get(entry.backend)
            if cls is None:
                raise BackendError(f"Неизвестный бэкенд: {entry.backend}")
            self._backends[entry.name] = cls(entry, self.settings)
        return self._backends[entry.name]

    # --- статусы ---
    def list_models(self) -> List[ModelInfo]:
        with self._lock:
            infos = []
            for entry in self.registry.all():
                be = self._backend_for(entry)
                running = self._active == entry.name and be.is_running()
                infos.append(ModelInfo(
                    name=entry.name, backend=entry.backend,
                    display_name=entry.display_name, description=entry.description,
                    installed=be.is_installed(), running=running,
                    pid=(be.start_info_pid() if hasattr(be, "start_info_pid") else None),
                ))
            return infos

    def status(self) -> dict:
        models = self.list_models()
        return {
            "active": self._active if any(m.name == self._active and m.running for m in models) else None,
            "models_total": len(models),
            "models_installed": sum(m.installed for m in models),
            "models_running": sum(m.running for m in models),
        }

    # --- операции ---
    def install(self, name: str) -> dict:
        with self._lock:
            entry = self._get(name)
            be = self._backend_for(entry)
            was = be.is_installed()
            if not was:
                be.install()
            return {"name": name, "installed": be.is_installed(), "was_installed": was}

    def start(self, name: str) -> dict:
        with self._lock:
            entry = self._get(name)
            be = self._backend_for(entry)
            if not be.is_installed():
                raise NotInstalledError(f"Модель '{name}' не установлена. Сначала выполните POST /api/models/install")

            stopped: List[str] = []
            if self._active and self._active != name:
                stopped = self._stop_active_locked()
            elif self._active == name:
                if be.is_running():
                    return {"name": name, "running": True, "stopped": []}

            be.start()
            self._active = name
            return {"name": name, "running": True, "stopped": stopped}

    def stop(self) -> dict:
        with self._lock:
            self._stop_active_locked()
            return {"running": None}

    def _stop_active_locked(self) -> List[str]:
        if not self._active:
            return []
        be = self._backends.get(self._active)
        if be:
            be.stop()
        name, self._active = self._active, None
        return [name]

    def chat(self, request: ChatRequest) -> ChatResponse:
        with self._lock:
            if not self._active:
                raise RuntimeError("NO_ACTIVE_MODEL")
            be = self._backends.get(self._active)
            if be is None or not be.is_running():
                raise RuntimeError("NO_ACTIVE_MODEL")
            return be.chat(request)

    def _get(self, name: str) -> ModelEntry:
        try:
            return self.registry.get(name)
        except KeyError:
            raise ModelNotFoundError(name)

    def shutdown(self) -> None:
        with self._lock:
            self._stop_active_locked()
