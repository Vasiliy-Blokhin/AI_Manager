"""Графический бэкенд: генерация изображений через OpenVINO (optimum-intel) на Intel Arc."""
from __future__ import annotations

import base64
import gc
import io
from pathlib import Path
from typing import Optional

from ..registry import ModelEntry
from ..schemas import ImageRequest, ImageResponse
from .base import Backend, BackendError, StartInfo


class OpenVinoSdBackend(Backend):
    backend_name = "openvino_sd"
    supports_images = True

    def __init__(self, entry: ModelEntry, settings: dict):
        super().__init__(entry, settings)
        self.cache_dir = Path(settings.get("hf_cache", "~/.cache/ai-manager")).expanduser() / entry.name
        self._pipe = None

    # --- установка ---
    def is_installed(self) -> bool:
        return (self.cache_dir / "model_index.json").exists()

    def install(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=self.entry.model_ref, local_dir=str(self.cache_dir))
        except Exception as e:
            raise BackendError(f"Не удалось скачать модель: {e}")

    # --- жизненный цикл (пайплайн в памяти процесса сервиса, GPU освобождается при stop) ---
    def start(self) -> StartInfo:
        if not self.is_installed():
            raise BackendError(f"Модель {self.entry.model_ref} не установлена. Сначала выполните install.")

        # 1) Импорты — отдельно, с реальным текстом ошибки (не маскируем под «не установлено»)
        try:
            from optimum.intel import OVStableDiffusionPipeline
        except ImportError as e:
            raise BackendError(
                f"Не установлены зависимости OpenVINO ({e}). "
                f'Установите в venv сервиса: venv\\Scripts\\python -m pip install "optimum-intel[openvino]" torch')

        # 2) Загрузка пайплайна — отдельно: ImportError здесь — это не «нет пакетов»,
        #    а конфликт версий/проблема внутри optimum — показываем как есть
        try:
            pipe = OVStableDiffusionPipeline.from_pretrained(str(self.cache_dir), export=True)
            # Intel Arc: устройство GPU выбирается автоматически
            pipe.to("GPU")
        except ImportError as e:
            raise BackendError(f"Ошибка импорта при загрузке пайплайна (конфликт версий?): {e}")
        except Exception as e:
            raise BackendError(f"Не удалось загрузить пайплайн на GPU: {e}")
        self._pipe = pipe
        return StartInfo(pid=0, endpoint="in-process")

    def stop(self) -> None:
        self._pipe = None
        gc.collect()  # освобождение VRAM (инвариант I1)

    def is_running(self) -> bool:
        return self._pipe is not None

    # --- инференс ---
    def generate_image(self, request: ImageRequest) -> ImageResponse:
        if self._pipe is None:
            raise BackendError("Модель не запущена")
        try:
            import torch
            generator = torch.Generator().manual_seed(request.seed) if request.seed is not None else None
            out = self._pipe(
                prompt=request.prompt,
                negative_prompt=request.negative_prompt or None,
                num_inference_steps=request.steps,
                height=request.height,
                width=request.width,
                generator=generator,
            )
            image = out.images[0]
        except Exception as e:
            raise BackendError(f"Ошибка генерации изображения: {e}")

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return ImageResponse(
            model=self.entry.name,
            backend=self.backend_name,
            width=request.width,
            height=request.height,
            steps=request.steps,
            seed=request.seed,
            image_base64=base64.b64encode(buf.getvalue()).decode("ascii"),
        )