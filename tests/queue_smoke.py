"""
Смоук-тест очереди задач без GPU: фейковые бэкенды + поддельный llama-server (httpx.MockTransport).
Поднимает сервис на отдельном порту (не мешает рабочему на 8000) с временной БД.

Запуск:  venv\\Scripts\\python -m tests.queue_smoke
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="aim-queue-"))
PORT = 8799
REGISTRY = {"models": [
    {"name": "m1", "backend": "fake_text", "model_ref": "x"},
    {"name": "m2", "backend": "fake_text", "model_ref": "x"},
    {"name": "par", "backend": "fake_text", "model_ref": "x", "parallel": 2},
    {"name": "img", "backend": "fake_image", "model_ref": "x"},
]}
(TMP / "registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
os.environ.update({
    "AIM_PASSWORD": "test", "AIM_REGISTRY": str(TMP / "registry.json"), "AIM_DB": str(TMP / "jobs.db"),
    "AIM_JOB_WAIT_TIMEOUT": "60", "AIM_JOB_RUN_TIMEOUT": "60", "AIM_AUTO_INSTALL": "false",
    "AIM_STICKY_SEC": "0",  # удержание модели проверяет tests.sticky_smoke, здесь — порядок без него
})

import httpx  # noqa: E402

from app import executors, manager as manager_mod  # noqa: E402
from app.backends.base import Backend, StartInfo  # noqa: E402
from app.schemas import ImageResponse  # noqa: E402

EVENTS: list = []          # ("start", name) / ("stop", name) / ("done", tag)
STREAMS = {"open": 0, "closed": 0}


class FakeText(Backend):
    backend_name = "fake_text"

    def __init__(self, entry, settings):
        super().__init__(entry, settings)
        self._on = False
        self.request_defaults = {}
        self.parallel = int(entry.extra.get("parallel", 1))

    def is_installed(self): return True
    def install(self): pass

    def start(self):
        time.sleep(0.3)
        self._on = True
        EVENTS.append(("start", self.entry.name))
        return StartInfo(pid=1, endpoint="fake")

    def stop(self):
        if self._on:
            EVENTS.append(("stop", self.entry.name))
        self._on = False

    def is_running(self): return self._on

    @property
    def openai_base_url(self): return f"http://fake/{self.entry.name}/v1"


class FakeImage(FakeText):
    backend_name = "fake_image"
    supports_images = True

    def generate_image(self, request, cancel=None):
        time.sleep(request.steps * 0.1)
        return ImageResponse(model=self.entry.name, backend=self.backend_name, width=request.width,
                             height=request.height, steps=request.steps, image_base64="AA==")


manager_mod.BACKENDS["fake_text"] = FakeText
manager_mod.BACKENDS["fake_image"] = FakeImage


def _sleep_of(body: dict) -> float:
    text = body["messages"][-1]["content"]
    return float(text.split("sleep:")[1].split()[0]) if "sleep:" in text else 0.0


async def _fake_llama(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    model = request.url.path.split("/")[1]
    delay = _sleep_of(body)
    tag = body["messages"][-1]["content"]
    if not body.get("stream"):
        await asyncio.sleep(delay)
        EVENTS.append(("done", tag))
        return httpx.Response(200, json={
            "model": model, "choices": [{"message": {"role": "assistant", "content": f"{model}:{tag}"},
                                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

    async def gen():
        STREAMS["open"] += 1
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < delay:
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                await asyncio.sleep(0.1)
            yield b"data: [DONE]\n\n"
            EVENTS.append(("done", tag))
        finally:
            STREAMS["closed"] += 1
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())


executors._CLIENT = httpx.AsyncClient(transport=httpx.MockTransport(_fake_llama), timeout=None)

from app.main import app, queue  # noqa: E402
import uvicorn  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}"
H = {"X-API-Password": "test"}
FAILS: list = []


def check(name: str, cond: bool, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def chat(c, model, text, project="p", **headers):
    return c.post(f"{BASE}/v1/chat/completions", headers={**H, "X-Project": project, **headers},
                  json={"model": model, "messages": [{"role": "user", "content": text}]})


async def wait_idle(c):
    for _ in range(100):
        q = (await c.get(f"{BASE}/api/queue", headers=H)).json()
        if not q["running"] and not q["waiting"]:
            return
        await asyncio.sleep(0.1)


async def main():
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    srv = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    async with httpx.AsyncClient(timeout=30) as c:
        print("1. Порядок: задачи текущей модели первыми, переключение — когда их нет")
        a1 = asyncio.create_task(chat(c, "m1", "a1 sleep:1", "A"))
        await asyncio.sleep(0.5)                       # m1 запущена, a1 выполняется
        b1 = asyncio.create_task(chat(c, "m2", "b1", "B"))
        await asyncio.sleep(0.1)
        a2 = asyncio.create_task(chat(c, "m1", "a2 sleep:0.5", "A"))   # пришла позже b1, но к m1
        await asyncio.sleep(0.2)
        q = (await c.get(f"{BASE}/api/queue", headers=H)).json()
        check("switch_locked при задачах m1", q["switch_locked"] is True, q)
        r = await c.post(f"{BASE}/api/models/m2/start", headers=H)
        check("ручной start m2 → 409 model_busy", r.status_code == 409 and r.json()["error"]["code"] == "model_busy", r.text)
        r = await c.post(f"{BASE}/api/models/stop", headers=H)
        check("ручной stop → 409 model_busy", r.status_code == 409, r.text)
        r = await c.post(f"{BASE}/api/restart", headers=H)
        check("restart при задачах → 409 queue_busy", r.status_code == 409, r.text)
        ra1, rb1, ra2 = await asyncio.gather(a1, b1, a2)
        check("все три 200", [x.status_code for x in (ra1, rb1, ra2)] == [200] * 3,
              [x.text[:200] for x in (ra1, rb1, ra2)])
        done = [e[1].split()[0] for e in EVENTS if e[0] == "done"]
        check("порядок выполнения a1, a2, b1", done == ["a1", "a2", "b1"], done)
        starts = [e[1] for e in EVENTS if e[0] == "start"]
        check("модели запускались m1 → m2 (одно переключение)", starts == ["m1", "m2"], starts)
        check("X-Job-Id в ответе", bool(ra1.headers.get("X-Job-Id")))
        job = (await c.get(f"{BASE}/api/jobs/{ra1.headers['X-Job-Id']}", headers=H)).json()
        check("задача в БД: done, проект A, usage", job["status"] == "done" and job["project"] == "A"
              and job["usage"]["total_tokens"] == 5, job)

        print("2. Неизвестное имя модели (IDE) → текущая модель")
        r = await chat(c, "gpt-4o", "hello")
        check("ответ от m2", r.status_code == 200 and r.json()["choices"][0]["message"]["content"].startswith("m2:"), r.text)

        print("3. Таймаут выполнения")
        r = await chat(c, "m2", "slow sleep:5", **{"X-Job-Timeout": "1"})
        check("504 timeout", r.status_code == 504 and r.json()["error"]["code"] == "timeout", r.text)
        await wait_idle(c)
        jobs = (await c.get(f"{BASE}/api/jobs?status=timeout", headers=H)).json()["jobs"]
        check("в БД статус timeout", len(jobs) == 1, jobs)

        print("4. Таймаут ожидания в очереди")
        long = asyncio.create_task(chat(c, "m2", "long sleep:2.5"))
        await asyncio.sleep(0.3)
        r = await chat(c, "m1", "late", **{"X-Job-Wait-Timeout": "1"})
        check("503 expired", r.status_code == 503 and r.json()["error"]["code"] == "expired", r.text)
        check("длинная задача завершилась", (await long).status_code == 200)
        starts = [e[1] for e in EVENTS if e[0] == "start"]
        check("истёкшая задача не переключила модель", starts == ["m1", "m2"], starts)

        print("5. Стрим: таймаут и обрыв клиентом закрывают upstream")
        closed0 = STREAMS["closed"]
        r = await c.post(f"{BASE}/v1/chat/completions", headers={**H, "X-Job-Timeout": "1"},
                         json={"model": "m2", "stream": True,
                               "messages": [{"role": "user", "content": "st sleep:5"}]})
        check("стрим оборван ошибкой timeout", '"code": "timeout"' in r.text, r.text[-300:])
        await wait_idle(c)
        check("upstream закрыт после таймаута", STREAMS["closed"] == closed0 + 1, STREAMS)
        async with c.stream("POST", f"{BASE}/v1/chat/completions", headers=H,
                            json={"model": "m2", "stream": True,
                                  "messages": [{"role": "user", "content": "drop sleep:5"}]}) as resp:
            async for _ in resp.aiter_bytes():
                break                                   # читаем первый кусок и уходим
        await asyncio.sleep(1.0)
        await wait_idle(c)
        check("upstream закрыт после обрыва клиентом", STREAMS["closed"] == closed0 + 2, STREAMS)
        last = (await c.get(f"{BASE}/api/jobs?limit=1", headers=H)).json()["jobs"][0]
        check("статус cancelled", last["status"] == "cancelled", last)
        r = await chat(c, "m2", "after")
        check("после обрыва очередь работает", r.status_code == 200, r.text)

        print("6. Async-задачи: постановка, результат, отмена")
        r = await c.post(f"{BASE}/api/jobs", headers=H, json={
            "kind": "chat", "model": "m1", "project": "auto_posting",
            "payload": {"messages": [{"role": "user", "content": "async1 sleep:0.5"}]}})
        check("202 + id", r.status_code == 202 and r.json()["id"], r.text)
        jid = r.json()["id"]
        r2 = await c.post(f"{BASE}/api/jobs", headers=H, json={
            "kind": "image", "model": "img", "payload": {"prompt": "cat", "steps": 3}})
        jid2 = r2.json()["id"]
        r3 = await c.post(f"{BASE}/api/jobs", headers=H, json={
            "kind": "chat", "model": "m2", "payload": {"messages": [{"role": "user", "content": "to-cancel"}]}})
        jid3 = r3.json()["id"]
        rc = await c.post(f"{BASE}/api/jobs/{jid3}/cancel", headers=H)
        check("отмена ожидающей задачи", rc.json().get("cancelling") is True, rc.text)
        await asyncio.sleep(0.3)
        await wait_idle(c)
        j1 = (await c.get(f"{BASE}/api/jobs/{jid}", headers=H)).json()
        j2 = (await c.get(f"{BASE}/api/jobs/{jid2}", headers=H)).json()
        j3 = (await c.get(f"{BASE}/api/jobs/{jid3}", headers=H)).json()
        check("chat-задача done с результатом", j1["status"] == "done" and j1["result"]["content"] == "m1:async1 sleep:0.5", j1)
        check("image-задача done", j2["status"] == "done" and j2["result"]["image_base64"] == "AA==", j2)
        check("отменённая — cancelled", j3["status"] == "cancelled", j3)

        print("7. /api/image через очередь, parallel=2")
        r = await c.post(f"{BASE}/api/image", headers=H, json={"prompt": "dog", "steps": 2})
        check("/api/image 200 (активна img)", r.status_code == 200, r.text)
        t0 = time.monotonic()
        rs = await asyncio.gather(*[chat(c, "par", f"p{i} sleep:1") for i in range(4)])
        el = time.monotonic() - t0
        check("4 задачи par по 1 с за ~2 с (2 слота)", all(x.status_code == 200 for x in rs) and 1.8 < el < 3.2, el)

        print("8. Ручной start/stop, когда задач нет")
        r = await c.post(f"{BASE}/api/models/m1/start", headers=H)
        check("start m1 → 200", r.status_code == 200, r.text)
        r = await c.post(f"{BASE}/api/models/stop", headers=H)
        check("stop → 200", r.status_code == 200, r.text)
        r = await c.post(f"{BASE}/api/chat", headers=H, json={"messages": [{"role": "user", "content": "x"}]})
        check("/api/chat без модели и без активной → 409 no_active_model", r.status_code == 409, r.text)
        r = await c.post(f"{BASE}/api/chat", headers=H, json={"model": "m1", "messages": [{"role": "user", "content": "x"}]})
        check("/api/chat с model → 200", r.status_code == 200 and r.json()["content"] == "m1:x", r.text)
        r = await c.post(f"{BASE}/api/chat", headers=H, json={"model": "img", "messages": [{"role": "user", "content": "x"}]})
        check("/api/chat с графической моделью → 409 wrong_model_type", r.status_code == 409, r.text)
        st = (await c.get(f"{BASE}/api/jobs/stats", headers=H)).json()
        check("статистика есть", len(st["rows"]) > 0)

    server.should_exit = True
    await srv

    print("9. Восстановление после рестарта")
    from app.jobs import JobStore
    s = JobStore(str(TMP / "recover.db"))
    now = time.time()
    base = dict(project="p", kind="chat", model="m1", created_at=now, wait_timeout=60, run_timeout=60)
    s.create(id="sync-q", mode="sync", status="queued", **base)
    s.create(id="async-run", mode="async", status="running", payload="{}", **base)
    s.create(id="async-q", mode="async", status="queued", payload='{"messages": []}', **base)
    back = s.recover_after_restart()
    check("возвращена только async queued", [j["id"] for j in back] == ["async-q"], back)
    check("остальные failed", s.get("sync-q")["status"] == "failed" and s.get("async-run")["status"] == "failed")
    s.close()

    print("\nИТОГ:", "все проверки пройдены" if not FAILS else f"провалено {len(FAILS)}: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
