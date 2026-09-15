# app/manager.py
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, List, Any
import aiofiles

class ModelProcess:
    """
    Класс-обертка над системным процессом llama-server.
    
    Attributes:
        name: Читаемое имя модели (например, 'qwen25-coder-14b').
        cmd: Полная команда запуска процесса (список аргументов CLI).
        port: Локальный порт, на котором висит сервер.
        process: Объект Popen для управления потоками stdin/stdout.
        gpu_layers: Количество слоев нейросети, выгруженных на GPU (-ngl флаг).
    """
    def __init__(self, name: str, cmd: list[str], port: int):
        self.name = name
        self.cmd = cmd
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self.gpu_layers: int = 99

    async def start(self):
        """Запуск процесса и базовая проверка его жизнеспособности."""
        print(f"[PROCESS] Starting {self.name} on port {self.port}...")
        env = os.environ.copy()
        
        try:
            # bufsize=1 и text=True включают line-buffering для чтения логов в реальном времени
            self.process = subprocess.Popen(
                self.cmd, 
                env=env, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            # Даем процессу время на инициализацию тяжелого контекста Vulkan/SYCL
            await asyncio.sleep(3.0)
            
            if self.process.poll() is not None:
                output = ""
                if self.process.stdout:
                    output = "".join([line async for line in self.process.stdout])
                raise RuntimeError(f"Model {self.name} failed to start. Exit code: {self.process.returncode}. Output: {output}")
                
        except Exception as e:
            print(f"[ERROR] Failed to spawn process for {self.name}: {e}", file=sys.stderr)
            raise

    async def stop(self):
        """Корректная остановка процесса с ожиданием завершения потоков."""
        if self.process and self.process.poll() is None:
            print(f"[PROCESS] Terminating {self.name}...")
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout=15.0)
            except asyncio.TimeoutError:
                print(f"[WARN] Force killing {self.name} after timeout.")
                self.process.kill()
            finally:
                if self.process.stdout:
                    await asyncio.to_thread(self.process.stdout.close)

class ModelManager:
    """
    Ядро системы управления моделями.
    
    Обеспечивает выполнение главного правила архитектуры:
    В памяти Intel Arc активна строго одна модель одновременно.
    При запуске новой модели старая автоматически выгружается для освобождения VRAM.
    """
    def __init__(self):
        self.active_model: Optional[str] = None
        self.processes: Dict[str, ModelProcess] = {}
        
        registry_path_str = os.getenv("AIM_REGISTRY", "config/models.registry.json")
        self.registry_path = Path(registry_path_str)
        
        if not self.registry_path.exists():
            raise FileNotFoundError(f"Models registry not found at {self.registry_path.resolve()}")
            
        self.load_registry()

    def load_registry(self):
        """Считывание JSON-файла с описанием доступных моделей и их параметров."""
        with open(self.registry_path, 'r', encoding='utf-8') as f:
            self.registry: Dict[str, Any] = json.load(f)

    async def ensure_single_active(self, requested_model: str):
        """
        Проверяет наличие активной модели. Если она отличается от запрашиваемой,
        инициирует процедуру её остановки для освобождения VRAM перед запуском новой.
        """
        if self.active_model and self.active_model != requested_model:
            print(f"[MEMORY] Switching active model from '{self.active_model}' to '{requested_model}'. Unloading old weights...")
            await self.stop_model(self.active_model)

    async def start_model(self, name: str):
        """
        Публичный метод запуска модели по имени из реестра.
        Формирует команду CLI для llama.cpp с учетом настроек Intel Arc.
        """
        if name not in self.registry:
            raise ValueError(f"Model '{name}' not found in registry.")
            
        await self.ensure_single_active(name)
        
        config = self.registry[name]
        bin_path = os.getenv("LLAMA_SERVER_BIN", "llama-server")
        
        cmd = [
            bin_path,
            "--hf-repo", config["repo"],
            "--model", config["filename"],
            "--port", str(config["port"]),
            "--ctx", "8192",
            "--batch", "2048",
            "--gpu-layers", str(config.get("gpu_layers", 99)),
            "--flash-attn", "on"
        ]
        
        proc = ModelProcess(name, cmd, config["port"])
        await proc.start()
        
        self.processes[name] = proc
        self.active_model = name
        print(f"[SUCCESS] Model '{name}' is now active.")

    async def stop_model(self, name: str):
        """Остановка конкретной модели и удаление её объекта из памяти менеджера."""
        if name in self.processes:
            await self.processes[name].stop()
            del self.processes[name]
            if self.active_model == name:
                self.active_model = None
            print(f"[SUCCESS] Model '{name}' stopped.")

    async def stop_all_models(self):
        """Массовая остановка всех запущенных процессов (вызывается при shutdown сервера)."""
        if not self.processes:
            return
            
        tasks = [proc.stop() for proc in self.processes.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.processes.clear()
        self.active_model = None

    async def generate_chat(self, model_name: str, messages: List[Dict], **kwargs) -> dict:
        """
        Проксирование запроса к локальному инстансу llama-server через curl.
        Использует временный файл для передачи payload, так как это самый стабильный способ
        взаимодействия с бинарными серверами GGUF из Python AsyncIO.
        """
        if model_name not in self.processes:
            raise ConnectionRefusedError(f"Port for model {model_name} is not open.")
            
        url = f"http://127.0.0.1:{self.processes[model_name].port}/v1/chat/completions"
        payload = {
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 2048)
        }
        
        async with aiofiles.tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.json') as tmp:
            await tmp.write(json.dumps(payload, ensure_ascii=False))
            temp_path = tmp.name
            
        process = await asyncio.create_subprocess_exec(
            "curl", "-s", "-X", "POST", url,
            "-H", "Content-Type: application/json",
            "-d", f"@{temp_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        os.unlink(temp_path)
        
        if process.returncode != 0:
            raise ConnectionError(f"curl error: {stderr.decode()}")
            
        return json.loads(stdout.decode())