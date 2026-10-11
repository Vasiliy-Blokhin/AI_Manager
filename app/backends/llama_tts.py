"""Бэкенд синтеза речи: Qwen3-TTS (GGUF) через llama-tts из сборки llama.cpp (Vulkan, Intel Arc).

Модель не держится в памяти между запросами: каждый запрос — отдельный запуск
llama-tts (загрузка ~2 ГБ весов занимает секунды, VRAM освобождается сразу).
«Запуск» модели в менеджере лишь проверяет наличие бинарника и весов и
резервирует GPU (инвариант I1: пока активен TTS, другие модели остановлены).

Поля реестра:
- model_ref — "repo:backbone.gguf" на HuggingFace;
- mmproj — имя файла аудио-кодека в том же репозитории (обязательно);
- ngl — слои на GPU (по умолчанию 99);
- args / env — дополнительные аргументы и переменные окружения llama-tts.

Клонирование голоса: положите образец (wav/mp3, 5–15 с чистой речи) в папку
voices/ проекта и передайте его имя в поле speaker запроса /api/tts.
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from huggingface_hub import hf_hub_download

from ..registry import ModelEntry
from ..schemas import TTSRequest, TTSResponse
from .base import Backend, BackendError, StartInfo, download_with_retries
from .llama_server import LlamaServerBackend

PROJECT_DIR = Path(__file__).resolve().parents[2]
VOICES_DIR = PROJECT_DIR / "voices"


class LlamaTtsBackend(Backend):
    backend_name = "llama_tts"
    supports_audio = True

    def __init__(self, entry: ModelEntry, settings: dict):
        super().__init__(entry, settings)
        self.cache_dir = Path(os.path.expandvars(settings.get("hf_cache", "~/.cache/ai-manager"))).expanduser() / entry.name
        self.mmproj = entry.extra.get("mmproj")
        self.gpu_layers = int(entry.extra.get("ngl", 99))
        self.extra_args = [str(a) for a in entry.extra.get("args", [])]
        self.extra_env = {k: str(v) for k, v in entry.extra.get("env", {}).items()}
        self._ready = False
        # Поиск llama-server.exe переиспользуем: llama-tts.exe лежит в той же сборке
        self._server_helper = LlamaServerBackend(entry, settings)

    # --- файлы ---
    def _files(self) -> tuple[str, str, str]:
        repo, _, fname = self.entry.model_ref.partition(":")
        if not repo or not fname or not self.mmproj:
            raise BackendError("Для llama_tts нужны model_ref 'repo:file.gguf' и поле mmproj в реестре")
        return repo, fname, self.mmproj

    def is_installed(self) -> bool:
        _, fname, mmproj = self._files()
        return (self.cache_dir / fname).exists() and (self.cache_dir / mmproj).exists()

    def install(self) -> None:
        repo, fname, mmproj = self._files()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for f in (fname, mmproj):
            download_with_retries(
                lambda f=f: hf_hub_download(repo_id=repo, filename=f, local_dir=str(self.cache_dir)),
                f"{repo}/{f}")

    def _tts_bin(self) -> Path:
        server = self._server_helper._resolve_bin()
        tts = server.with_name("llama-tts.exe" if server.suffix == ".exe" else "llama-tts")
        if not tts.is_file():
            raise BackendError(f"llama-tts не найден рядом с {server}. Обновите сборку: scripts\\download_llama_server.ps1")
        return tts

    # --- жизненный цикл ---
    def start(self) -> StartInfo:
        if not self.is_installed():
            raise BackendError(f"Веса {self.entry.model_ref} не установлены. Сначала выполните install.")
        self._tts_bin()
        self._ready = True
        return StartInfo(pid=0, endpoint="subprocess")

    def stop(self) -> None:
        self._ready = False

    def is_running(self) -> bool:
        return self._ready

    # --- инференс ---
    def synthesize(self, request: TTSRequest) -> TTSResponse:
        if not self._ready:
            raise BackendError("Модель не запущена")
        _, fname, mmproj = self._files()
        cmd = [str(self._tts_bin()),
               "-m", str(self.cache_dir / fname),
               "-mm", str(self.cache_dir / mmproj),
               "-ngl", str(self.gpu_layers),
               "--tts-lang", request.language,
               "-p", request.text]
        if request.speaker:
            speaker = (VOICES_DIR / request.speaker).resolve()
            if VOICES_DIR.resolve() not in speaker.parents or not speaker.is_file():
                raise BackendError(f"Образец голоса не найден в папке voices/: {request.speaker}")
            cmd += ["--tts-speaker-file", str(speaker)]
        if request.temperature is not None:
            cmd += ["--temp", str(request.temperature)]
        if request.seed is not None:
            cmd += ["--seed", str(request.seed)]
        cmd += self.extra_args

        logs_dir = PROJECT_DIR / "logs"
        logs_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.wav"
            cmd += ["--output", str(out)]
            env = {**os.environ, **self.extra_env}
            with open(logs_dir / f"llama-tts-{self.entry.name}.log", "ab", buffering=0) as log:
                log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {len(request.text)} симв.\n".encode())
                try:
                    p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=1800)
                except subprocess.TimeoutExpired:
                    raise BackendError("Таймаут синтеза речи (30 мин)")
            if p.returncode != 0 or not out.exists():
                raise BackendError(f"llama-tts завершился с кодом {p.returncode}. Смотрите logs/llama-tts-{self.entry.name}.log")
            data = out.read_bytes()
            try:
                with wave.open(str(out)) as w:
                    duration = w.getnframes() / float(w.getframerate())
            except Exception:
                duration = 0.0
        return TTSResponse(model=self.entry.name, backend=self.backend_name,
                           duration_sec=round(duration, 3),
                           audio_base64=base64.b64encode(data).decode("ascii"))
