"""
Тест реакции очереди на зависший поток (без GPU и без HTTP): фейковый менеджер.

1. Запуск модели завис → таймаут запуска → поток не завершился → on_hang, async-задача снова queued.
2. Генерация картинки встала между шагами → on_hang; повторно зависшая задача (restarts=1) → timeout.
3. Генерация с колбэком отмены: таймаут прерывает её, слот освобождается без перезапуска.

Запуск:  venv\\Scripts\\python -m tests.hang_smoke
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

from app.jobs import JobQueue, JobStore

FAILS: list = []
FOREVER = threading.Event()  # «зависшие» потоки ждут его до конца теста


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeBackend:
    model_type = "image"
    parallel = 1

    def __init__(self):
        self.progress = None


class FakeManager:
    def __init__(self, hang_start: bool):
        self.hang_start = hang_start
        self.active = None
        self.be = FakeBackend()
        self.registry = self

    def get(self, name):
        return name

    def _backend_for(self, entry):
        return self.be

    def active_name(self):
        return self.active

    def start(self, name):
        if self.hang_start:
            FOREVER.wait()
        self.active = name


def make_queue(tmp: Path, hang_start: bool, **settings) -> tuple[JobQueue, list]:
    store = JobStore(str(tmp / f"jobs-{time.monotonic_ns()}.db"))
    q = JobQueue(FakeManager(hang_start), store, {"job_run_timeout": 1, **settings})
    hangs: list = []
    q.on_hang = hangs.append
    return q, hangs


async def wait_for(cond, timeout: float = 15) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.2)
    return False


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aim-hang-"))

    print("1. Зависший запуск модели")
    q, hangs = make_queue(tmp, True, model_start_timeout=1, stuck_grace=1)
    q.register_executor("image", lambda q_, t, p: asyncio.sleep(0, {}))
    await q.start()
    t = q.submit(kind="image", model="img", project="p", payload={"prompt": "x"})
    check("сработал on_hang", await wait_for(lambda: hangs))
    row = q.store.get(t.id)
    check("async-задача возвращена в очередь", row["status"] == "queued" and row["restarts"] == 1, str(row))
    check("в БД есть причина", "Модель не запустилась" in (row["error"] or "")
          or "не завершился" in (row["error"] or ""), row["error"])
    await q.stop()

    print("2. Генерация встала между шагами (повторное зависание)")
    q, hangs = make_queue(tmp, False, image_stall_timeout=1, job_run_timeout=60)

    def stuck_gen(cancel):
        q.manager.be.progress = (3, 20, time.monotonic())
        FOREVER.wait()
        return {}

    async def exec_stuck(q_, t, p):
        return await q_.run_blocking(t, stuck_gen, t.cancel_event)

    q.register_executor("image", exec_stuck)
    await q.start()
    t = q.submit(kind="image", model="img", project="p", payload={"prompt": "x"})
    q.store.update(t.id, restarts=1)  # уже повторялась
    check("сработал on_hang", await wait_for(lambda: hangs))
    check("причина — шаг 3/20", bool(hangs) and "3/20" in hangs[0], str(hangs))
    row = q.store.get(t.id)
    check("второй раз — timeout без повтора", row["status"] == "timeout" and row["restarts"] == 2, str(row))
    await q.stop()

    print("3. Отмена через колбэк шага — без перезапуска")
    q, hangs = make_queue(tmp, False, stuck_grace=5)

    def cooperative_gen(cancel):
        for step in range(1000):
            q.manager.be.progress = (step + 1, 1000, time.monotonic())
            if cancel.wait(0.1):
                raise RuntimeError("Генерация прервана")
        return {}

    async def exec_coop(q_, t, p):
        return await q_.run_blocking(t, cooperative_gen, t.cancel_event)

    q.register_executor("image", exec_coop)
    await q.start()
    t = q.submit(kind="image", model="img", project="p", payload={"prompt": "x"})
    check("задача — timeout", await wait_for(lambda: q.store.get(t.id)["status"] == "timeout"))
    check("слот освобождён", await wait_for(lambda: not q._running, 5))
    await asyncio.sleep(1)
    check("перезапуска не было", not hangs, str(hangs))
    await q.stop()

    FOREVER.set()
    print("ИТОГ:", "OK" if not FAILS else f"FAIL {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
