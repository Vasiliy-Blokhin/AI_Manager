"""Загрузка и валидация реестра моделей."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ModelEntry:
    name: str
    backend: str
    model_ref: str
    display_name: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self, path: str):
        self.path = path
        self._models: Dict[str, ModelEntry] = {}
        self.reload()

    def reload(self) -> None:
        if not os.path.exists(self.path):
            raise RegistryError(f"Реестр не найден: {self.path}")
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        models: Dict[str, ModelEntry] = {}
        for raw in data.get("models", []):
            entry = ModelEntry(
                name=raw["name"],
                backend=raw["backend"],
                model_ref=raw["model_ref"],
                display_name=raw.get("display_name", raw["name"]),
                description=raw.get("description", ""),
                extra={k: v for k, v in raw.items()
                       if k not in {"name", "backend", "model_ref",
                                    "display_name", "description"}},
            )
            if entry.name in models:
                raise RegistryError(f"Дубликат модели: {entry.name}")
            models[entry.name] = entry
        if not models:
            raise RegistryError("Реестр пуст")
        self._models = models

    def get(self, name: str) -> ModelEntry:
        if name not in self._models:
            raise KeyError(name)
        return self._models[name]

    def all(self) -> List[ModelEntry]:
        return list(self._models.values())
