"""
Смоук-тест «липкого» удержания модели в очереди (AIM_STICKY_SEC / AIM_STICKY_MAX_SEC).
Без GPU и HTTP: JobQueue с фейковым менеджером моделей, временная БД.

Сценарий из жизни: агент local_code_worker шлёт шаги к gpt-oss подряд с паузами на
инструменты, а auto_posting ждёт qwen. Без удержания каждая пауза агента — переключение
модели и потеря KV-кэша.

Запуск:  venv\\Scripts\\python -m tests.sticky_smoke
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from app.jobs import JobCancelled, JobQueue, JobStore

TMP = Path(tempfile.mkdtemp(prefix="aim-sticky-"))
FAILS: list = []


def check(name: str, cond: bool, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeManager:
    """Ровно то, что нужно JobQueue: активная модель, запуск, тип и слоты бэкенда."""

    def __init__(self):
        self.active = None
        self.events = []  # (monotonic, "start", model)
        self.registry = SimpleNamespace(get=lambda name: SimpleNamespace(name=name))

    def active_name(self):
        return self.active

    def start(self, name):
        if self.active != name:
            time.sleep(0.05)
            self.active = name
            self.events.append((time.monotonic(), "start", name))

    def _backend_for(self, entry):
        return SimpleNamespace(model_type="text", parallel=1, is_installed=lambda: True)

    def starts(self):
        return [e[2] for e in self.events if e[1] == "start"]


def make_queue(n: int, sticky: float, sticky_max: float = 60):
    m = FakeManager()
    q = JobQueue(m, JobStore(str(TMP / f"q{n}.db")),
                 {"sticky_sec": sticky, "sticky_max_sec": sticky_max})
    return m, q


async def job(q: JobQueue, model: str, project: str, work: float = 0.1, log=None, fail=False):
    t = q.new_ticket(kind="chat", model=model, project=project)
    await q.acquire(t)
    if log is not None:
        log.append((time.monotonic(), project))
    await asyncio.sleep(work)
    if fail:
        q._request_stop(t, JobCancelled("cancelled", "Задача отменена через API"))
        q.release(t, "cancelled", "Задача отменена через API")
    else:
        q.release(t, "done")


async def main():
    print("1. Шаг агента в пределах удержания идёт раньше чужой модели")
    m, q = make_queue(1, sticky=1.0)
    log = []
    await job(q, "gpt", "agent", log=log)                         # шаг 1 агента
    other = asyncio.create_task(job(q, "qwen", "auto_posting", log=log))
    await asyncio.sleep(0.3)                                       # агент «выполняет инструмент»
    snap = q.snapshot()
    check("snapshot: удержание gpt за agent", snap["hold"] and snap["hold"]["model"] == "gpt"
          and snap["hold"]["project"] == "agent" and 0 < snap["hold"]["left_sec"] <= 1.0, snap)
    check("qwen ждёт, переключения нет", m.starts() == ["gpt"] and not snap["running"], snap)
    await job(q, "gpt", "agent", log=log)                         # шаг 2 агента
    t_done = time.monotonic()
    await other
    order = [p for _, p in log]
    check("порядок: agent, agent, auto_posting", order == ["agent", "agent", "auto_posting"], order)
    check("одно переключение gpt → qwen", m.starts() == ["gpt", "qwen"], m.starts())
    wait = log[-1][0] - t_done
    check("qwen запущен по окончании удержания (~1 с)", 0.8 < wait < 1.6, round(wait, 2))

    print("2. Удержание не даётся, если чужая задача ждёт дольше AIM_STICKY_MAX_SEC")
    m, q = make_queue(2, sticky=1.0, sticky_max=1.0)
    log = []
    await job(q, "gpt", "agent", log=log)
    other = asyncio.create_task(job(q, "qwen", "auto_posting", log=log))
    t_other = time.monotonic()

    async def agent_loop():
        # агент без конца шлёт шаги с паузой 0.2 с — без предела qwen ждал бы вечно
        for _ in range(15):
            await job(q, "gpt", "agent", work=0.2, log=log)
            await asyncio.sleep(0.2)
            if other.done():
                break

    await asyncio.gather(agent_loop(), other)
    t_qwen = next(ts for ts, p in log if p == "auto_posting")
    check("qwen получил GPU за ~1 с ожидания (предел)", 0.9 < t_qwen - t_other < 1.8,
          round(t_qwen - t_other, 2))
    check("агент успел сделать шаги до переключения",
          sum(1 for ts, p in log if p == "agent" and ts < t_qwen) >= 3, log)

    print("3. AIM_STICKY_SEC=0 — прежнее поведение: переключение сразу")
    m, q = make_queue(3, sticky=0)
    log = []
    await job(q, "gpt", "agent", log=log)
    t_done = time.monotonic()
    await job(q, "qwen", "auto_posting", log=log)
    check("qwen запущен без ожидания", log[-1][0] - t_done < 0.4, round(log[-1][0] - t_done, 2))
    check("snapshot: удержания нет", q.snapshot()["hold"] is None)

    print("4. Отменённая задача удержание не включает")
    m, q = make_queue(4, sticky=5.0)
    log = []
    await job(q, "gpt", "agent", log=log, fail=True)
    t_done = time.monotonic()
    await job(q, "qwen", "auto_posting", log=log)
    check("qwen запущен без ожидания", log[-1][0] - t_done < 0.4, round(log[-1][0] - t_done, 2))

    print("5. Задачи к той же модели от других проектов в удержании тоже идут сразу")
    m, q = make_queue(5, sticky=5.0)
    log = []
    await job(q, "gpt", "agent", log=log)
    t0 = time.monotonic()
    await job(q, "gpt", "claude-delegate", log=log)
    check("без ожидания и без переключения", log[-1][0] - t0 < 0.3 and m.starts() == ["gpt"],
          (round(log[-1][0] - t0, 2), m.starts()))

    print("6. Без чужих задач удержание ничего не задерживает; удержание другой модели не мешает")
    m, q = make_queue(6, sticky=5.0)
    log = []
    await job(q, "gpt", "agent", log=log)
    m.active = "qwen"                     # модель сменили вручную (POST /api/models/.../start)
    t0 = time.monotonic()
    await job(q, "img", "auto_posting", log=log)
    check("удержание gpt не действует при активной qwen", log[-1][0] - t0 < 0.4,
          round(log[-1][0] - t0, 2))

    print("\nИТОГ:", "все проверки пройдены" if not FAILS else f"провалено {len(FAILS)}: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
