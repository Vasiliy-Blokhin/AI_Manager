"""RealVisXL V5.0 → OpenVINO FP16 для бэкенда openvino_sd (модель realvisxl-v5-ov в реестре).

Готовой OpenVINO-сборки нет, поэтому: скачать fp16-веса diffusers и экспортировать без квантования
(INT8 заметно портит детали). Нужно ~16 ГБ свободного лимита памяти (commit) — без загруженной LLM и ~7 ГБ на диске под исходники.

    venv\\Scripts\\python scripts\\convert_realvisxl.py [папка-с-исходниками]

Если загрузка через huggingface_hub зависает (WinError 10038), скачайте файлы curl'ом
(resolve/main/<путь>, только *.fp16.safetensors и конфиги) и передайте папку аргументом.
"""
import os
import sys
import time
from pathlib import Path

REPO = "SG161222/RealVisXL_V5.0"
CACHE = Path(os.path.expandvars(os.getenv("AIM_HF_CACHE", r"D:\ai-cache\ai-manager"))).expanduser()


def _fix_py314_partials() -> None:
    """Python 3.14: functools.partial стал дескриптором метода — атрибут класса NORMALIZED_CONFIG_CLASS
    (partial) получает лишний self («multiple values for argument 'allow_new'»). Оборачиваем в staticmethod."""
    import functools
    import optimum.exporters.openvino  # noqa: F401 — регистрирует конфиги экспорта
    from optimum.exporters.base import ExporterConfig

    todo = [ExporterConfig]
    while todo:
        cls = todo.pop()
        todo.extend(cls.__subclasses__())
        val = cls.__dict__.get("NORMALIZED_CONFIG_CLASS")
        if isinstance(val, functools.partial):
            setattr(cls, "NORMALIZED_CONFIG_CLASS", staticmethod(val))


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else CACHE.parent / "hf-src" / "RealVisXL_V5.0"
    dst = CACHE / "realvisxl-v5-ov"
    t = time.time()
    if not (src / "unet" / "diffusion_pytorch_model.fp16.safetensors").exists():
        from huggingface_hub import snapshot_download
        snapshot_download(REPO, local_dir=str(src),
                          allow_patterns=["model_index.json", "*/config.json", "scheduler/*", "tokenizer*/*",
                                          "*.fp16.safetensors"])
    import torch
    _fix_py314_partials()
    from optimum.intel import OVStableDiffusionXLPipeline
    pipe = OVStableDiffusionXLPipeline.from_pretrained(str(src), export=True, compile=False, variant="fp16",
                                                       torch_dtype=torch.float32, load_in_8bit=False)
    pipe.save_pretrained(str(dst))
    del pipe
    # экспорт сохраняет FP32 (UNet 9.6 ГБ) — сжимаем веса до FP16 (4.9 ГБ, помещается в 12 ГБ VRAM)
    import gc
    import shutil
    import openvino as ov
    core = ov.Core()
    for xml in dst.glob("*/openvino_model.xml"):
        tmp = xml.with_name("fp16.xml")
        ov.save_model(core.read_model(str(xml)), str(tmp), compress_to_fp16=True)
        gc.collect()
        for ext in (".xml", ".bin"):
            os.replace(tmp.with_suffix(ext), xml.with_suffix(ext))
    # OpenVINO-токенизаторы экспорт не создаёт (openvino-tokenizers на Python 3.14); у дообученных SDXL
    # словари те же, что у базового SDXL — берём готовые из sdxl-base-int8-ov
    for d in ("tokenizer", "tokenizer_2"):
        for f in (CACHE / "sdxl-base-int8-ov" / d).glob("openvino_*"):
            shutil.copy2(f, dst / d / f.name)
    print(f"Готово за {time.time() - t:.0f} с: {dst}")


if __name__ == "__main__":
    main()
