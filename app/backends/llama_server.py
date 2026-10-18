"""Текстовый бэкенд: GGUF-модели через llama-server (Vulkan/SYCL, 100% GPU на Intel Arc)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
from huggingface_hub import hf_hub_download

from ..orphans import bind_to_service
from ..registry import ModelEntry
from ..schemas import ChatRequest, ChatResponse, Usage
from .base import Backend, BackendError, StartInfo, download_with_retries


class LlamaServerBackend(Backend):
    backend_name = "llama_server"
    supports_images = False

    def __init__(self, entry: ModelEntry, settings: dict):
        super().__init__(entry, settings)
        self.bin = settings.get("llama_server_bin", "llama-server")
        self.cache_dir = Path(os.path.expandvars(settings.get("hf_cache", "~/.cache/ai-manager"))).expanduser() / entry.name
        self.port_range = settings.get("port_range", (8100, 8199))
        ngl = entry.extra.get("ngl", settings.get("gpu_layers", 99))
        # ngl = null в реестре → не передаём -ngl, llama-server сам подгоняет под VRAM (--fit)
        self.gpu_layers: Optional[int] = None if ngl is None else int(ngl)
        self.context = int(entry.extra.get("context", 100000))
        self.n_cpu_moe = entry.extra.get("n_cpu_moe")          # MoE-эксперты первых N слоёв — на CPU
        self.parallel = int(entry.extra.get("parallel", 1))   # 1 слот: весь контекст одному пользователю
        self.flash_attn = entry.extra.get("flash_attn")        # "on" | "off" | "auto"
        self.extra_args = [str(a) for a in entry.extra.get("args", [])]
        # «Зрение» мультимодальной модели (Qwen3.6 и др.): файл проектора изображений из того же репо.
        # Необязателен: без файла модель запускается как текстовая (is_installed проверяет только веса)
        self.mmproj: Optional[str] = entry.extra.get("mmproj")
        self.extra_env = {k: str(v) for k, v in entry.extra.get("env", {}).items()}
        # Параметры, подставляемые в каждый запрос, если клиент их не задал (reasoning_effort и т.п.)
        self.request_defaults: dict = dict(entry.extra.get("request_defaults", {}))
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._log_path: Optional[Path] = None

    # --- файлы ---
    def _weights_path(self) -> Path:
        repo, _, fname = self.entry.model_ref.partition(":")
        if not repo or not fname:
            raise BackendError(f"Неверный model_ref для llama_server (нужно 'repo:file'): {self.entry.model_ref}")
        return self.cache_dir / fname

    def is_installed(self) -> bool:
        return self._weights_path().exists()

    def install(self) -> None:
        repo, _, fname = self.entry.model_ref.partition(":")
        if not repo or not fname:
            raise BackendError(f"Неверный model_ref: {self.entry.model_ref}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        download_with_retries(
            lambda: hf_hub_download(repo_id=repo, filename=fname, local_dir=str(self.cache_dir)),
            f"{repo}/{fname}")
        if self.mmproj and not self._mmproj_path().exists():
            download_with_retries(
                lambda: hf_hub_download(repo_id=repo, filename=self.mmproj, local_dir=str(self.cache_dir)),
                f"{repo}/{self.mmproj}")

    def _mmproj_path(self) -> Path:
        return self.cache_dir / (self.mmproj or "")

    # --- поиск бинарника ---
    def _resolve_bin(self) -> Path:
        """Возвращает полный путь к исполняемому файлу llama-server.exe или raises BackendError."""
        raw = os.path.expandvars(os.path.expanduser(str(self.bin)))
        p = Path(raw)

        # 1) Абсолютный/относительный путь существует
        if p.is_file():
            return p.resolve()
        if p.exists():
            raise BackendError(
                f"LLAMA_SERVER_BIN указывает на каталог, а нужен .exe-файл: {p}. "
                f"Исправьте путь в .env (например: {p}\\llama-server.exe)")

        # 2) Имя без пути — ищем в PATH
        if not (os.sep in raw or "/" in raw):
            w = shutil.which(raw)
            if w:
                return Path(w).resolve()

        # 3) Автопоиск рядом с проектом: llama-server\llama-server.exe (и вложенные папки релизов)
        project_dir = Path(__file__).resolve().parents[2]  # app/backends/llama_server.py -> корень проекта
        search_root = project_dir / "llama-server"
        if search_root.is_dir():
            hits = sorted(search_root.rglob("llama-server.exe"))
            if hits:
                return hits[0].resolve()

        raise BackendError(
            f"Бинарник llama-server не найден: {self.bin}. "
            f"Выполните scripts\\setup.bat (скачает сборку в папку llama-server\\) "
            f"или задайте полный путь в .env: LLAMA_SERVER_BIN=D:\\...\\llama-server.exe")

    # --- жизненный цикл ---
    def _free_port(self) -> int:
        import socket
        for port in range(self.port_range[0], self.port_range[1] + 1):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        raise BackendError("Нет свободных портов в AIM_PORT_RANGE")

    def start(self) -> StartInfo:
        if not self.is_installed():
            raise BackendError(f"Веса {self.entry.model_ref} не установлены. Сначала выполните install.")
        bin_path = self._resolve_bin()  # чёткая ошибка, если бинарник не найден / указан каталог

        self.stop()  # инвариант I1: старый процесс этой модели не должен жить
        port = self._free_port()

        # логи llama-server — в logs\llama-server-<модель>.log (вместо DEVNULL)
        project_dir = Path(__file__).resolve().parents[2]
        logs_dir = project_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_path = logs_dir / f"llama-server-{self.entry.name}.log"
        self._log_path = log_path

        cmd = [str(bin_path), "-m", str(self._weights_path()),
               "--host", "127.0.0.1", "--port", str(port),
               "-c", str(self.context),
               "-np", str(self.parallel),
               "--alias", self.entry.name,
               "--no-webui"]
        if self.gpu_layers is not None:
            cmd += ["-ngl", str(self.gpu_layers)]
        if self.n_cpu_moe:
            cmd += ["--n-cpu-moe", str(int(self.n_cpu_moe))]
        if self.flash_attn:
            cmd += ["-fa", str(self.flash_attn)]
        if self.mmproj and self._mmproj_path().exists():
            cmd += ["--mmproj", str(self._mmproj_path())]
        cmd += self.extra_args
        env = {**os.environ, **self.extra_env}
        try:
            # env наследуется: ONEAPI_DEVICE_SELECTOR и др. берутся из .env/системы,
            # поверх — переменные из реестра (например, обходы для Vulkan на Intel Arc)
            log = open(log_path, "ab", buffering=0)
            log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n".encode())
            self._proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
            bind_to_service(self._proc.pid)  # упадёт сервис — Windows завершит и llama-server
        except FileNotFoundError as e:
            if getattr(e, "winerror", None) == 2:
                raise BackendError(
                    f"Не удалось запустить {bin_path}: файл или его зависимость (DLL) не найдены "
                    f"(WinError 2). Для Vulkan-сборки нужен установленный Vulkan runtime и драйвер "
                    f"Intel Arc. Подробности: {log_path}")
            raise BackendError(f"Не удалось запустить llama-server: {e}")
        except Exception as e:
            raise BackendError(f"Не удалось запустить llama-server: {e}")

        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 600
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise BackendError(
                    f"llama-server завершился сразу после запуска (код {self._proc.returncode}). "
                    f"Смотрите лог: {log_path}")
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    self._port = port
                    return StartInfo(pid=self._proc.pid, endpoint=url)
            except Exception:
                pass
            time.sleep(2)
        self.stop()
        raise BackendError(f"Таймаут ожидания запуска llama-server. Смотрите лог: {log_path}")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._port = None

    def process_alive(self) -> bool:
        """Дешёвая проверка без HTTP: процесс жив и порт назначен."""
        return bool(self._proc and self._proc.poll() is None and self._port)

    def is_running(self) -> bool:
        if not self.process_alive():
            return False
        try:
            return httpx.get(f"http://127.0.0.1:{self._port}/health", timeout=5).status_code == 200
        except Exception:
            return False

    @property
    def openai_base_url(self) -> str:
        """OpenAI-совместимый base активного llama-server (только localhost)."""
        if not self._port:
            raise BackendError("Модель не запущена")
        return f"http://127.0.0.1:{self._port}/v1"

    # --- инференс ---
    def chat(self, request: ChatRequest) -> ChatResponse:
        if not self.is_running() or not self._port:
            raise BackendError("Модель не запущена")
        payload = {
            **self.request_defaults,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "stream": False,
            "cache_prompt": True
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        try:
            r = httpx.post(f"http://127.0.0.1:{self._port}/v1/chat/completions",
                           json=payload, timeout=None)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            raise BackendError(f"llama-server вернул ошибку: {e.response.status_code}")
        except Exception as e:
            raise BackendError(f"Ошибка запроса к llama-server: {e}")

        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
        u = data.get("usage") or {}
        usage = Usage(prompt_tokens=u.get("prompt_tokens", 0),
                      completion_tokens=u.get("completion_tokens", 0))
        usage.total_tokens = u.get("total_tokens", usage.prompt_tokens + usage.completion_tokens)
        return ChatResponse(model=self.entry.name, backend=self.backend_name,
                            content=content, usage=usage)
