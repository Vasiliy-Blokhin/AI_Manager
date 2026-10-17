"""
Сторож живости сервиса (внутри процесса).

Ловит два вида «тихой смерти», при которых процесс жив, но не отвечает,
и поэтому цикл перезапуска в scripts/run.bat не срабатывает:

1. Зависший event loop (бесконечный цикл / блокирующий вызов в корутине):
   asyncio-задача раз в секунду обновляет метку, отдельный поток проверяет,
   что метка не устарела дольше AIM_HANG_TIMEOUT секунд.
2. Потерянный слушающий сокет (loop жив, но порт закрыт — asyncio Proactor
   на Windows закрывает listener при ошибке accept): поток раз в
   AIM_PROBE_INTERVAL секунд делает GET /health на свой порт; после
   AIM_PROBE_FAILS неудач подряд — перезапуск.

Перед выходом стеки всех потоков пишутся в logs/hang-*.log (для разбора причины),
затем активная модель останавливается (с таймаутом), и процесс завершается
вместе с дочерними (llama-server) — run.bat поднимает сервис заново.
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

EXIT_CODE = 3


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class Watchdog:
    def __init__(self, log_dir: Path, port: int, shutdown: Callable[[], None]):
        self.log_dir = log_dir
        self.url = f"http://127.0.0.1:{port}/health"
        self.shutdown = shutdown
        self.enabled = os.getenv("AIM_WATCHDOG", "true").lower() in ("1", "true", "yes")
        self.hang_timeout = _env_float("AIM_HANG_TIMEOUT", 60)
        self.probe_interval = _env_float("AIM_PROBE_INTERVAL", 30)
        self.probe_fails = int(_env_float("AIM_PROBE_FAILS", 3))
        self._beat = time.monotonic()
        self._task: Optional[asyncio.Task] = None
        self._stop = threading.Event()
        self._restart_lock = threading.Lock()

    # ---------- запуск / остановка ----------

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Сторож отключён (AIM_WATCHDOG=false)")
            return
        self._beat = time.monotonic()
        self._task = asyncio.create_task(self._heartbeat())
        threading.Thread(target=self._monitor, name="aim-watchdog", daemon=True).start()
        logger.info("Сторож: loop > %ds без отклика или %d× неудачных /health — перезапуск",
                    int(self.hang_timeout), self.probe_fails)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _heartbeat(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(1.0)

    # ---------- проверки ----------

    def _probe(self) -> bool:
        try:
            with urllib.request.urlopen(self.url, timeout=10) as r:
                return r.status == 200
        except Exception:
            return False

    def _monitor(self) -> None:
        # первая проверка порта — после старта uvicorn (lifespan отработал раньше bind)
        next_probe = time.monotonic() + self.probe_interval
        fails = 0
        while not self._stop.wait(2.0):
            stale = time.monotonic() - self._beat
            if stale > self.hang_timeout:
                self.restart(f"event loop не отвечает {int(stale)} с")
                return
            if time.monotonic() >= next_probe:
                next_probe = time.monotonic() + self.probe_interval
                if self._probe():
                    fails = 0
                    continue
                fails += 1
                logger.warning("Сторож: %s не отвечает (%d/%d)", self.url, fails, self.probe_fails)
                if fails >= self.probe_fails:
                    self.restart(f"{self.url} не отвечает {fails} раз подряд")
                    return

    # ---------- аварийный перезапуск ----------

    def _dump(self, reason: str) -> Optional[Path]:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"hang-{datetime.now():%Y%m%d-%H%M%S}.log"
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} PID {os.getpid()}: {reason}\n\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
            return path
        except Exception:
            logger.exception("Сторож: не удалось записать стеки")
            return None

    def restart(self, reason: str) -> None:
        """Аварийный перезапуск (также по зависшей задаче очереди — работает и при AIM_WATCHDOG=false)."""
        with self._restart_lock:
            self._do_restart(reason)

    def _do_restart(self, reason: str) -> None:
        path = self._dump(reason)
        msg = f"Сторож: {reason} — перезапуск сервиса (стеки: {path})"
        logger.error(msg)
        print(msg, file=sys.stderr, flush=True)

        # мягко выгрузить модель; lock менеджера может быть занят зависшим кодом — не ждём вечно
        t = threading.Thread(target=self._safe_shutdown, daemon=True)
        t.start()
        t.join(15)

        if sys.platform == "win32":
            # завершить себя вместе с дочерними процессами (llama-server), если они остались
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                           capture_output=True, timeout=30)
        os._exit(EXIT_CODE)

    def _safe_shutdown(self) -> None:
        try:
            self.shutdown()
        except Exception:
            logger.exception("Сторож: ошибка остановки модели")
