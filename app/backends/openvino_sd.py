"""Графический бэкенд: генерация изображений через OpenVINO на Intel Arc.

Два движка:
- **genai** (по умолчанию для готовых OpenVINO-сборок) — `openvino_genai.Text2ImagePipeline`,
  официальный способ запуска репозиториев OpenVINO/* (SD 1.5, SDXL, FLUX.1, SD3);
- **optimum** — `optimum.intel.OVDiffusionPipeline`, нужен для обычных diffusers-репозиториев
  (конвертирует при первом запуске).

Поля реестра (необязательные):
- engine — "genai" | "optimum" (по умолчанию genai, если модель уже в формате OpenVINO);
- devices — где считать части пайплайна, например
  {"text_encoder": "CPU", "denoise": "GPU", "vae": "GPU"}: текстовый энкодер (T5 у FLUX, ~2.7–4.7 ГБ)
  на CPU освобождает видеопамять под основную модель; по умолчанию всё на GPU;
- default_steps / default_guidance — значения по умолчанию для модели;
- max_side — ограничение стороны изображения (по умолчанию 1536).

Замечание (Arc B580, 12 ГБ): FLUX.1-schnell INT8 (трансформер 11.9 ГБ + T5 4.7 ГБ) в видеопамять
не помещается — на GPU даёт шум, на CPU работает (~6 мин/кадр). Для GPU — сборка INT4.
"""
from __future__ import annotations

import base64
import os
import gc
import io
from pathlib import Path

from ..registry import ModelEntry
from ..schemas import ImageRequest, ImageResponse
from .base import Backend, BackendError, StartInfo, download_with_retries


class OpenVinoSdBackend(Backend):
    backend_name = "openvino_sd"
    supports_images = True

    def __init__(self, entry: ModelEntry, settings: dict):
        super().__init__(entry, settings)
        self.cache_dir = Path(settings.get("hf_cache", "~/.cache/ai-manager")).expanduser() / entry.name
        self._pipe = None
        self._i2i = None   # Image2ImagePipeline (genai): режим «картинка → картинка», создаётся по запросу
        self._devices: dict = {}
        self._engine = None

    # --- установка ---
    def is_installed(self) -> bool:
        return (self.cache_dir / "model_index.json").exists()

    def install(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        from huggingface_hub import snapshot_download
        download_with_retries(
            lambda: snapshot_download(repo_id=self.entry.model_ref, local_dir=str(self.cache_dir)),
            self.entry.model_ref)

    def _converted(self) -> bool:
        return any(self.cache_dir.glob("*/openvino_model.xml"))

    # --- жизненный цикл (пайплайн в памяти процесса сервиса, GPU освобождается при stop) ---
    def start(self) -> StartInfo:
        if not self.is_installed():
            raise BackendError(f"Модель {self.entry.model_ref} не установлена. Сначала выполните install.")
        engine = self.entry.extra.get("engine") or ("genai" if self._converted() else "optimum")
        if engine == "genai":
            self._pipe = self._start_genai()
        else:
            self._pipe = self._start_optimum()
        self._engine = engine
        return StartInfo(pid=0, endpoint=f"in-process ({engine})")

    def _start_genai(self):
        try:
            import openvino_genai as ov_genai
        except ImportError as e:
            raise BackendError(f"Не установлен openvino-genai ({e}): venv\\Scripts\\python -m pip install openvino-genai")
        self._devices = {"text_encoder": "GPU", "denoise": "GPU", "vae": "GPU", **self.entry.extra.get("devices", {})}
        try:
            # ВАЖНО: Text2ImagePipeline, созданный поверх Image2ImagePipeline (общие веса), на Arc B580
            # выдавал полностью чёрные кадры (13.10.2026). Поэтому обычная генерация — отдельным
            # конвейером (проверенная схема), а «картинка → картинка» — отдельным, по первому запросу.
            pipe = ov_genai.Text2ImagePipeline(str(self.cache_dir))
            pipe.compile(self._devices["text_encoder"], self._devices["denoise"], self._devices["vae"],
                         CACHE_DIR=str(self._ov_cache()))
            return pipe
        except Exception as e:
            raise BackendError(f"Не удалось загрузить пайплайн (genai, {self._devices}): {e}")

    def _ov_cache(self) -> Path:
        """Кэш скомпилированных ядер GPU: первая генерация каждого размера иначе длится 15+ минут."""
        path = Path(os.path.expandvars(self.settings.get("hf_cache", "~/.cache/ai-manager"))).expanduser() \
            .parent / "ov-cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _img2img(self):
        """Конвейер «картинка → картинка» — создаётся при первом запросе с исходным изображением."""
        if self._i2i is None:
            import openvino_genai as ov_genai
            i2i = ov_genai.Image2ImagePipeline(str(self.cache_dir))
            i2i.compile(self._devices["text_encoder"], self._devices["denoise"], self._devices["vae"],
                        CACHE_DIR=str(self._ov_cache()))
            self._i2i = i2i
        return self._i2i

    def _start_optimum(self):
        try:
            from optimum.intel import OVDiffusionPipeline
        except ImportError as e:
            raise BackendError(
                f"Не установлены зависимости OpenVINO ({e}). "
                f'Установите в venv сервиса: venv\\Scripts\\python -m pip install "optimum-intel[openvino]" torch')
        try:
            pipe = OVDiffusionPipeline.from_pretrained(str(self.cache_dir), export=not self._converted())
            pipe.to("GPU")
            return pipe
        except ImportError as e:
            raise BackendError(f"Ошибка импорта при загрузке пайплайна (конфликт версий?): {e}")
        except Exception as e:
            raise BackendError(f"Не удалось загрузить пайплайн на GPU: {e}")

    def stop(self) -> None:
        self._pipe = None
        self._i2i = None
        self._engine = None
        gc.collect()  # освобождение VRAM (инвариант I1)

    def is_running(self) -> bool:
        return self._pipe is not None

    # --- инференс ---
    def generate_image(self, request: ImageRequest) -> ImageResponse:
        if self._pipe is None:
            raise BackendError("Модель не запущена")
        max_side = int(self.entry.extra.get("max_side", 1536))
        if max(request.width, request.height) > max_side:
            raise BackendError(f"Сторона изображения больше {max_side} для модели {self.entry.name}")
        guidance = request.guidance_scale
        if guidance is None:
            guidance = self.entry.extra.get("default_guidance")
        try:
            image = (self._generate_genai(request, guidance) if self._engine == "genai"
                     else self._generate_optimum(request, guidance))
        except BackendError:
            raise
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

    def _generate_genai(self, request: ImageRequest, guidance):
        from PIL import Image
        kwargs = dict(width=request.width, height=request.height, num_inference_steps=request.steps)
        if request.seed is not None:
            kwargs["rng_seed"] = int(request.seed)
        if guidance is not None:
            kwargs["guidance_scale"] = float(guidance)
        if request.negative_prompt and "flux" not in self.entry.model_ref.lower():
            kwargs["negative_prompt"] = request.negative_prompt
        if request.init_image_base64:
            if self._engine != "genai":
                raise BackendError("Режим «картинка → картинка» доступен только для движка genai")
            import openvino as ov
            import numpy as np
            src = Image.open(io.BytesIO(base64.b64decode(request.init_image_base64))).convert("RGB")
            # обрезка по центру под целевые пропорции и размер
            w, h = request.width, request.height
            scale = max(w / src.width, h / src.height)
            src = src.resize((max(w, round(src.width * scale)), max(h, round(src.height * scale))), Image.LANCZOS)
            left, top = (src.width - w) // 2, (src.height - h) // 2
            src = src.crop((left, top, left + w, top + h))
            kwargs.pop("width"), kwargs.pop("height")
            tensor = self._img2img().generate(request.prompt, ov.Tensor(np.array(src, dtype=np.uint8)[None]),
                                        strength=float(request.strength), **kwargs)
        else:
            tensor = self._pipe.generate(request.prompt, **kwargs)
        return Image.fromarray(tensor.data[0])

    def _generate_optimum(self, request: ImageRequest, guidance):
        import torch
        generator = torch.Generator().manual_seed(request.seed) if request.seed is not None else None
        kwargs = dict(prompt=request.prompt, num_inference_steps=request.steps,
                      height=request.height, width=request.width, generator=generator)
        if guidance is not None:
            kwargs["guidance_scale"] = float(guidance)
        # FLUX не принимает negative_prompt без true_cfg — передаём только для других моделей
        if request.negative_prompt and "Flux" not in type(self._pipe).__name__:
            kwargs["negative_prompt"] = request.negative_prompt
        return self._pipe(**kwargs).images[0]
