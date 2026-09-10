"""Бэкенд Ollama: модели ставятся через `ollama pull`, ответ — через HTTP API."""
from __future__ import annotations

import subprocess
from typing import Optional

import httpx

from ..registry import ModelEntry
from ..schemas import ChatRequest, ChatResponse, Usage
from .base import Backend, BackendError, StartInfo


class OllamaBackend(Backend):
    backend_name = "ollama"

    def __init__(self, entry: ModelEntry, settings: dict):
        super().__init__(entry, settings)
        self.base_url = settings.get("ollama_url", "http://127.0.0.1:11434")

    # --- установка ---
    def _tags(self) -> set:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=10)
            r.raise_for_status()
            return {m.get("name", "") for m in r.json().get("models", [])}
        except Exception as e:
            raise BackendError(f"Ollama недоступен по {self.base_url}: {e}")

    def is_installed(self) -> bool:
        ref = self.entry.model_ref
        return any(t == ref or t.startswith(ref + ":") for t in self._tags())

    def install(self) -> None:
        proc = subprocess.run(
            ["ollama", "pull", self.entry.model_ref],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise BackendError(f"ollama pull завершился с ошибкой: {proc.stderr.strip()}")

    # --- жизненный цикл ---
    def start(self) -> StartInfo:
        if not self.is_installed():
            raise BackendError(f"Модель {self.entry.model_ref} не установлена. Сначала выполните install.")
        # Ollama не держит отдельный процесс на модель: прогреваем её
        # коротким запросом, чтобы она загрузилась в память.
        try:
            r = httpx.post(
                f"{self.base_url}/api/chat",
                json={"model": self.entry.model_ref, "messages": [{"role": "user", "content": "ping"}],
                      "stream": False, "options": {"num_predict": 1}},
                timeout=600,
            )
            r.raise_for_status()
        except Exception as e:
            raise BackendError(f"Не удалось загрузить модель в Ollama: {e}")
        return StartInfo(pid=0, endpoint=self.base_url)  # pid 0 = управляется Ollama

    def stop(self) -> None:
        try:
            httpx.post(f"{self.base_url}/api/generate",
                       json={"model": self.entry.model_ref, "keep_alive": 0}, timeout=30)
        except Exception:
            pass  # выгрузка — best effort

    def is_running(self) -> bool:
        try:
            r = httpx.post(
                f"{self.base_url}/api/chat",
                json={"model": self.entry.model_ref, "messages": [{"role": "user", "content": "ping"}],
                      "stream": False, "options": {"num_predict": 1}},
                timeout=60,
            )
            return r.status_code == 200
        except Exception:
            return False

    # --- инференс ---
    def chat(self, request: ChatRequest) -> ChatResponse:
        payload = {
            "model": self.entry.model_ref,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "options": {"temperature": request.temperature},
        }
        if request.max_tokens:
            payload["options"]["num_predict"] = request.max_tokens
        try:
            r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=1800)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            raise BackendError(f"Ollama вернул ошибку: {e.response.status_code} {e.response.text[:500]}")
        except Exception as e:
            raise BackendError(f"Ошибка запроса к Ollama: {e}")

        usage_raw = data.get("prompt_eval_count"), data.get("eval_count")
        usage = Usage(
            prompt_tokens=usage_raw[0] or 0,
            completion_tokens=usage_raw[1] or 0,
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return ChatResponse(
            model=self.entry.name,
            backend=self.backend_name,
            content=(data.get("message") or {}).get("content", ""),
            usage=usage,
        )
