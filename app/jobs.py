"""
Учёт задач (SQLite) и очередь к GPU.

Зачем: к сервису ходят несколько проектов (local_code_worker, auto_posting, IDE),
каждый со своей моделью, а видеокарта одна (инвариант I1 — одна активная модель).
Без очереди запрос проекта Б к другой модели выгружал модель посреди генерации
проекта А.

Правила планировщика:
- Каждый запрос к модели (чат, /v1, картинка, речь) — задача (job) в БД
  `data/ai_manager.db` (AIM_DB) со статусом и отметками времени.
- Задачи к ТЕКУЩЕЙ модели выполняются первыми (по порядку поступления),
  до `parallel` штук одновременно (поле реестра, по умолчанию 1).
- Модель переключается ТОЛЬКО когда по текущей модели нет ни выполняющихся,
  ни ожидающих задач. Затем берётся самая старая задача к другой модели.
- «Липкое» удержание: агенты (local_code_worker) шлют много коротких запросов подряд
  с паузами в секунды на выполнение инструментов. Чтобы в эти паузы не вклинивалась
  чужая модель (переключение + потеря KV-кэша на каждом шаге), после задачи модель
  держится ещё AIM_STICKY_SEC секунд. Удержание не даётся, если самая старая задача
  к другой модели ждёт дольше AIM_STICKY_MAX_SEC — так другие проекты не голодают.
- Ручной start/stop модели запрещён (409 model_busy), пока по активной модели
  есть задачи.
- Таймауты: ожидание в очереди — AIM_JOB_WAIT_TIMEOUT (статус expired),
  выполнение — AIM_JOB_RUN_TIMEOUT (статус timeout, генерация прерывается).
  Клиент может переопределить: заголовки X-Job-Wait-Timeout / X-Job-Timeout
  или поля wait_timeout / run_timeout в POST /api/jobs.
  Запуск модели ограничен AIM_MODEL_START_TIMEOUT (иначе зависший запуск держал GPU вечно).
- Зависание: поток OpenVINO/запуска модели прервать нельзя. Если после отмены/таймаута поток
  не завершился за AIM_STUCK_GRACE секунд или генерация картинки стоит на одном шаге дольше
  AIM_IMAGE_STALL_TIMEOUT — сервис перезапускается (on_hang → сторож, run.bat поднимает заново).
  Зависшая async-задача один раз возвращается в очередь (столбец restarts), затем — timeout.

Статусы: queued → running → done | failed | cancelled | timeout; queued → expired | cancelled.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .manager import ModelManager, NotInstalledError, WrongModelTypeError

logger = logging.getLogger(__name__)

TERMINAL = ("done", "failed", "cancelled", "timeout", "expired")
KIND_TYPES = {"chat": "text", "image": "image", "tts": "audio"}
MAX_TIMEOUT = 24 * 3600
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="aim-job")


class ModelBusyError(Exception):
    """Нельзя переключить/остановить модель: по ней есть задачи."""


class JobCancelled(Exception):
    """Задача отменена (клиентом, через API или истёк срок ожидания)."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status  # cancelled | timeout | expired


# ---------------------------------------------------------------- БД

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    project      TEXT NOT NULL DEFAULT 'unknown',
    kind         TEXT NOT NULL,              -- chat | image | tts
    model        TEXT NOT NULL,
    mode         TEXT NOT NULL DEFAULT 'sync', -- sync (ждёт HTTP-клиент) | async (POST /api/jobs)
    stream       INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    wait_timeout REAL NOT NULL,
    run_timeout  REAL NOT NULL,
    summary      TEXT,                       -- начало промпта, для просмотра очереди
    error        TEXT,
    usage        TEXT,                       -- JSON {prompt_tokens, completion_tokens, ...}
    payload      TEXT,                       -- JSON запроса (только async: нужен для повтора после рестарта)
    result       TEXT,                       -- JSON результата (только async)
    restarts     INTEGER NOT NULL DEFAULT 0  -- сколько раз задача зависла и сервис перезапускался
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at);
"""

_LIST_COLUMNS = ("id, project, kind, model, mode, stream, status, created_at, started_at, "
                 "finished_at, wait_timeout, run_timeout, summary, error, usage, restarts")


class JobStore:
    """Тонкая обёртка над SQLite. Все записи короткие — вызывается прямо из event loop."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(jobs)")}
        if "restarts" not in cols:  # БД старой версии
            self._db.execute("ALTER TABLE jobs ADD COLUMN restarts INTEGER NOT NULL DEFAULT 0")

    def create(self, **fields: Any) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            self._db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))

    def get(self, job_id: str, with_result: bool = True) -> Optional[dict]:
        cols = "*" if with_result else _LIST_COLUMNS
        with self._lock:
            row = self._db.execute(f"SELECT {cols} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row(row) if row else None

    def list(self, status: Optional[str] = None, project: Optional[str] = None,
             model: Optional[str] = None, limit: int = 100) -> List[dict]:
        where, args = [], []
        for col, val in (("project", project), ("model", model)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args += statuses
        sql = f"SELECT {_LIST_COLUMNS} FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def stats(self, since: float) -> List[dict]:
        """Сводка по проектам/моделям/статусам за период — для GET /api/jobs/stats."""
        with self._lock:
            rows = self._db.execute(
                "SELECT project, model, status, COUNT(*) AS n, "
                "AVG(CASE WHEN finished_at IS NOT NULL AND started_at IS NOT NULL "
                "    THEN finished_at - started_at END) AS avg_run_sec, "
                "AVG(CASE WHEN started_at IS NOT NULL THEN started_at - created_at END) AS avg_wait_sec "
                "FROM jobs WHERE created_at >= ? GROUP BY project, model, status "
                "ORDER BY project, model, status", (since,)).fetchall()
        return [dict(r) for r in rows]

    def recover_after_restart(self) -> List[dict]:
        """
        После рестарта сервиса: sync-задачи и прерванные running уже никому не нужны
        (HTTP-клиент отвалился) — помечаются failed. Ожидавшие async-задачи
        возвращаются для повторной постановки в очередь.
        """
        now = time.time()
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, "
                "error = 'Сервис перезапущен во время выполнения/ожидания' "
                "WHERE status IN ('queued', 'running') AND NOT (mode = 'async' AND status = 'queued')",
                (now,))
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE mode = 'async' AND status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [_row(r) for r in rows]

    def cleanup(self, retention_days: float) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cur = self._db.execute(
                f"DELETE FROM jobs WHERE created_at < ? AND status IN ({', '.join('?' for _ in TERMINAL)})",
                (cutoff, *TERMINAL))
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("usage", "payload", "result"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                pass
    if "stream" in d:
        d["stream"] = bool(d["stream"])
    return d


# ---------------------------------------------------------------- очередь

@dataclass
class Ticket:
    """Задача в памяти планировщика (пока она queued/running)."""
    id: str
    kind: str
    model: str
    project: str
    created: float
    wait_timeout: float
    run_timeout: float
    granted: asyncio.Event = field(default_factory=asyncio.Event)
    running: bool = False
    deadline: Optional[float] = None      # monotonic-срок: запуска модели, затем выполнения
    starting: bool = False                 # идёт запуск модели (deadline — срок запуска)
    stop_reason: Optional[JobCancelled] = None
    stop_at: Optional[float] = None        # monotonic-момент запроса остановки
    cancel_event: threading.Event = field(default_factory=threading.Event)  # для потоков (genai)
    work: Optional[asyncio.Future] = None  # текущая прерываемая работа (child-task)
    threads: int = 0                       # незавершённые потоки (картинка/речь не прерываются)
    released: bool = False
    finalized: bool = False

    @property
    def stop_requested(self) -> bool:
        return self.stop_reason is not None


class JobQueue:
    """Планировщик доступа к GPU. Работает в event loop сервиса (однопоточно)."""

    def __init__(self, manager: ModelManager, store: JobStore, settings: dict):
        self.manager = manager
        self.store = store
        self.wait_timeout = float(settings.get("job_wait_timeout", 3600))
        self.run_timeout = float(settings.get("job_run_timeout", 3600))
        self.retention_days = float(settings.get("job_retention_days", 14))
        self.autostart = bool(settings.get("autostart", True))
        self.start_timeout = float(settings.get("model_start_timeout", 1200))
        self.stuck_grace = float(settings.get("stuck_grace", 300))
        self.image_stall = float(settings.get("image_stall_timeout", 600))
        # удержание модели после задачи (0 — выкл.) и предел ожидания задач к другим моделям
        self.sticky_sec = float(settings.get("sticky_sec", 20))
        self.sticky_max = float(settings.get("sticky_max_sec", 180))
        self._hold: Optional[dict] = None          # {"model", "project", "until"} (monotonic)
        self._hold_timer: Optional[asyncio.TimerHandle] = None
        # вызывается при зависшем потоке: перезапуск процесса (main подключает сторож)
        self.on_hang: Optional[Callable[[str], None]] = None
        self._hang_reported = False
        self._waiting: List[Ticket] = []          # в порядке поступления
        self._running: Dict[str, Ticket] = {}
        self._maintenance: Optional[str] = None   # идёт ручной start/stop — новые задачи не выдаются
        self._watchdog: Optional[asyncio.Task] = None
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self._executors: Dict[str, Callable[["JobQueue", Ticket, dict], Awaitable[dict]]] = {}

    # ---------- запуск / остановка ----------

    def register_executor(self, kind: str, fn: Callable[["JobQueue", Ticket, dict], Awaitable[dict]]) -> None:
        """Исполнитель async-задач данного вида: (queue, ticket, payload) -> result dict."""
        self._executors[kind] = fn

    async def start(self) -> None:
        removed = self.store.cleanup(self.retention_days)
        if removed:
            logger.info("Очередь: удалено старых задач: %d", removed)
        for job in self.store.recover_after_restart():
            if job["kind"] not in self._executors:
                self.store.update(job["id"], status="failed", finished_at=time.time(),
                                  error=f"Нет исполнителя для вида '{job['kind']}'")
                continue
            logger.info("Очередь: повторная постановка async-задачи %s (%s)", job["id"], job["model"])
            self._spawn_async(job)
        self._watchdog = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        for t in list(self._waiting) + list(self._running.values()):
            self._request_stop(t, JobCancelled("cancelled", "Сервис останавливается"))
        for task in list(self._async_tasks.values()):
            task.cancel()

    # ---------- выбор модели ----------

    def current_model(self) -> Optional[str]:
        """Модель, которой сейчас «принадлежит» GPU: выполняющиеся задачи или активная модель."""
        for t in self._running.values():
            return t.model
        return self.manager.active_name()

    def _model_type(self, name: str) -> Optional[str]:
        try:
            return self.manager._backend_for(self.manager.registry.get(name)).model_type
        except Exception:
            return None

    def resolve_model(self, kind: str, requested: Optional[str]) -> str:
        """
        Какая модель выполнит запрос:
        - requested — установленная модель нужного типа из реестра → она (при AIM_AUTOSTART);
        - неизвестное имя (IDE шлют своё) или не указано → текущая модель, если подходит по типу.
        """
        need = KIND_TYPES[kind]
        if requested:
            try:
                entry = self.manager.registry.get(requested)
            except KeyError:
                entry = None
            if entry is not None:
                be = self.manager._backend_for(entry)
                if be.model_type != need:
                    raise WrongModelTypeError(
                        f"Модель '{requested}' имеет тип '{be.model_type}', а запрос требует '{need}'")
                if not be.is_installed():
                    raise NotInstalledError(
                        f"Модель '{requested}' не установлена. Сначала выполните POST /api/models/install")
                if self.autostart or requested == self.current_model():
                    return requested
        current = self.current_model()
        if current and self._model_type(current) == need:
            return current
        if current:
            raise WrongModelTypeError(
                f"Активна модель '{current}' типа '{self._model_type(current)}'; укажите модель типа "
                f"'{need}' в поле model")
        raise RuntimeError("NO_ACTIVE_MODEL")

    def _slots(self, model: str) -> int:
        """Одновременных задач на модель: слоты llama-server (-np), остальным бэкендам — 1."""
        try:
            be = self.manager._backend_for(self.manager.registry.get(model))
        except Exception:
            return 1
        return max(1, int(getattr(be, "parallel", 1) or 1))

    # ---------- жизненный цикл задачи ----------

    def _clamp(self, value: Optional[float], default: float) -> float:
        if value is None or value <= 0:
            return default
        return min(float(value), MAX_TIMEOUT)

    def new_ticket(self, *, kind: str, model: str, project: str, summary: str = "",
                   wait_timeout: Optional[float] = None, run_timeout: Optional[float] = None,
                   stream: bool = False, job_id: Optional[str] = None,
                   payload: Optional[dict] = None) -> Ticket:
        """Создаёт задачу (запись в БД со статусом queued) и ставит её в очередь."""
        t = Ticket(id=job_id or uuid.uuid4().hex[:16], kind=kind, model=model,
                   project=project or "unknown", created=time.monotonic(),
                   wait_timeout=self._clamp(wait_timeout, self.wait_timeout),
                   run_timeout=self._clamp(run_timeout, self.run_timeout))
        if job_id is None:
            self.store.create(
                id=t.id, project=t.project, kind=kind, model=model,
                mode="async" if payload is not None else "sync", stream=int(stream),
                status="queued", created_at=time.time(), wait_timeout=t.wait_timeout,
                run_timeout=t.run_timeout, summary=summary[:300],
                payload=json.dumps(payload, ensure_ascii=False) if payload is not None else None)
        self._waiting.append(t)
        self._schedule()
        return t

    async def acquire(self, t: Ticket,
                      is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None) -> None:
        """
        Ждёт очереди, затем запускает нужную модель. После возврата задача running,
        срок выполнения отсчитывается. При ошибке задача уже закрыта (release не нужен).
        """
        try:
            while not t.granted.is_set():
                if t.stop_reason:
                    raise t.stop_reason
                left = t.wait_timeout - (time.monotonic() - t.created)
                if left <= 0:
                    raise JobCancelled("expired", f"Задача не дождалась очереди за {int(t.wait_timeout)} с")
                try:
                    await asyncio.wait_for(t.granted.wait(), timeout=min(2.0, left))
                except TimeoutError:
                    if is_disconnected is not None and await is_disconnected():
                        raise JobCancelled("cancelled", "Клиент отключился, не дождавшись очереди")
            if t.stop_reason:
                raise t.stop_reason
            self.store.update(t.id, status="running", started_at=time.time())
            # Модель запускается в потоке: llama-server стартует до нескольких минут.
            # Переключение безопасно: задача получила слот, только когда по старой модели задач нет.
            t.starting, t.deadline = True, time.monotonic() + self.start_timeout
            await self.run_blocking(t, self.manager.start, t.model, cancellable=False)
            if t.stop_reason:
                raise t.stop_reason
            t.starting, t.deadline = False, time.monotonic() + t.run_timeout
        except JobCancelled as e:
            self.release(t, e.status, str(e))
            raise
        except asyncio.CancelledError:
            self.release(t, "cancelled", "Запрос отменён клиентом")
            raise
        except Exception as e:
            self.release(t, "failed", f"Не удалось запустить модель: {e}")
            raise

    async def execute(self, t: Ticket, aw: Awaitable[Any], *,
                      check: Optional[Callable[[Any], Optional[str]]] = None,
                      usage: Optional[Callable[[Any], Optional[dict]]] = None) -> Any:
        """
        Выполняет работу задачи и закрывает её: done / failed / timeout / cancelled.
        check(result) → текст ошибки, если результат — неуспех (например HTTP 500 от модели).
        """
        try:
            result = await aw
        except JobCancelled as e:
            self.release(t, e.status, str(e))
            raise
        except asyncio.CancelledError:
            self.release(t, "cancelled", "Запрос отменён клиентом")
            raise
        except Exception as e:
            self.release(t, "failed", str(e))
            raise
        error = check(result) if check else None
        self.release(t, "failed" if error else "done", error,
                     usage=usage(result) if usage and not error else None)
        return result

    async def run(self, t: Ticket, aw: Awaitable[Any]) -> Any:
        """
        Выполняет прерываемую работу задачи (корутину) в отдельной child-задаче:
        отмена через API или по таймауту прерывает её (стрим к llama-server закрывается —
        генерация останавливается сразу), а HTTP-обработчик получает JobCancelled.
        """
        if t.stop_reason:
            raise t.stop_reason
        work = asyncio.ensure_future(aw)
        t.work = work
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            work.cancel()
            if t.stop_reason and not _cancelling():
                raise t.stop_reason from None
            raise
        finally:
            if t.work is work:
                t.work = None

    async def run_blocking(self, t: Ticket, fn: Callable[..., Any], *args: Any,
                           cancellable: bool = True) -> Any:
        """
        Блокирующая работа (OpenVINO, llama-tts, запуск модели) в потоке.
        Поток прервать нельзя: при отмене клиент сразу получает ответ, но GPU-слот
        освобождается только когда поток закончит (иначе следующая задача столкнётся с ним).
        """
        loop = asyncio.get_running_loop()
        t.threads += 1

        def _done() -> None:
            t.threads -= 1
            if t.released:
                self._finalize(t)

        # Свой пул: колбэк завершения срабатывает, когда поток реально закончил,
        # а не когда отменили ожидающий его asyncio-future.
        cf = _POOL.submit(fn, *args)
        cf.add_done_callback(lambda _cf: loop.call_soon_threadsafe(_done))
        fut = asyncio.wrap_future(cf, loop=loop)
        if not cancellable:
            return await asyncio.shield(fut)
        return await self.run(t, fut)

    def release(self, t: Ticket, status: str, error: Optional[str] = None,
                usage: Optional[dict] = None, result: Optional[dict] = None) -> None:
        """Закрывает задачу: финальный статус в БД, освобождение слота, запуск следующих."""
        if t.released:
            return
        t.released = True
        if t.stop_reason and status == "failed":
            # отменили/истёк срок, и работа оборвалась ошибкой — главная причина — отмена
            status, error = t.stop_reason.status, str(t.stop_reason)
        fields: Dict[str, Any] = {"status": status, "finished_at": time.time()}
        if error:
            fields["error"] = error[:2000]
        if usage:
            fields["usage"] = json.dumps(usage, ensure_ascii=False)
        if result is not None:
            fields["result"] = json.dumps(result, ensure_ascii=False)
            fields["payload"] = None  # запрос больше не нужен, результат сохранён
        self.store.update(t.id, **fields)
        if status != "done":
            logger.info("Задача %s (%s, %s): %s %s", t.id, t.project, t.model, status, error or "")
        self._finalize(t)

    def _finalize(self, t: Ticket) -> None:
        if t.finalized or t.threads > 0:
            return
        t.finalized = True
        if t in self._waiting:
            self._waiting.remove(t)
        if self._running.pop(t.id, None) is not None and not t.stop_requested:
            self._start_hold(t)
        self._schedule()

    # ---------- отмена ----------

    def _request_stop(self, t: Ticket, reason: JobCancelled) -> None:
        if t.stop_reason or t.released:
            return
        t.stop_reason = reason
        t.stop_at = time.monotonic()
        t.cancel_event.set()  # генерация genai прервётся на ближайшем шаге
        if t.work is not None and not t.work.done():
            t.work.cancel()
        elif not t.granted.is_set():
            t.granted.set()  # разбудить ожидающего — он увидит stop_reason

    def cancel(self, job_id: str) -> bool:
        """Отмена через API. Возвращает False, если задача не найдена среди активных."""
        for t in list(self._waiting) + list(self._running.values()):
            if t.id == job_id:
                self._request_stop(t, JobCancelled("cancelled", "Задача отменена через API"))
                return True
        return False

    async def _watch(self) -> None:
        """Сторож: прерывает задачи, превысившие срок выполнения; раз в час чистит БД."""
        last_cleanup = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for t in list(self._running.values()):
                if self._check_hang(t, now):
                    break
                if t.deadline is None or now <= t.deadline:
                    continue
                if not t.stop_requested:
                    msg = (f"Модель не запустилась за {int(self.start_timeout)} с" if t.starting
                           else f"Превышено время выполнения ({int(t.run_timeout)} с)")
                    self._request_stop(t, JobCancelled("timeout", msg))
                elif (now > t.deadline + 30 and t.work is None and t.threads == 0
                      and not t.released and t.stop_reason is not None):
                    # «осиротевшая» задача: например, клиент отключился до начала стрима
                    # и генератор так и не запустился — освобождаем слот принудительно
                    self.release(t, t.stop_reason.status, str(t.stop_reason))
            if now - last_cleanup > 3600:
                last_cleanup = now
                try:
                    self.store.cleanup(self.retention_days)
                except Exception:
                    logger.exception("Очистка БД задач")

    # ---------- зависания ----------

    def _check_hang(self, t: Ticket, now: float) -> bool:
        """True — поток задачи завис, начат перезапуск сервиса."""
        if self._hang_reported or t.threads <= 0:
            return False
        if t.stop_at is not None:
            # после отмены поток должен закончиться: genai — на следующем шаге, запуск — сам
            grace = self.stuck_grace * (1 if t.stop_reason and t.stop_reason.status == "timeout" else 3)
            if now - t.stop_at > grace:
                self._hang(t, f"задача {t.id} ({t.model}) остановлена {int(now - t.stop_at)} с назад, "
                              f"но её поток не завершился")
                return True
        elif t.kind == "image" and not t.starting and self.image_stall > 0:
            progress = getattr(self._backend(t.model), "progress", None)
            # только между шагами: до первого шага идёт компиляция ядер (до 15+ мин), после последнего — VAE
            if progress and progress[0] < progress[1] and now - progress[2] > self.image_stall:
                self._request_stop(t, JobCancelled(
                    "timeout", f"Генерация изображения зависла на шаге {progress[0]}/{progress[1]}"))
                self._hang(t, f"задача {t.id} ({t.model}): шаг {progress[0]}/{progress[1]} "
                              f"не завершился за {int(now - progress[2])} с")
                return True
        return False

    def _backend(self, model: str):
        try:
            return self.manager._backend_for(self.manager.registry.get(model))
        except Exception:
            return None

    def _hang(self, t: Ticket, reason: str) -> None:
        """Зависший поток не освободит GPU — закрываем задачи и перезапускаем процесс."""
        self._hang_reported = True
        logger.error("Очередь: зависание — %s", reason)
        now = time.time()
        for job in list(self._running.values()):
            row = self.store.get(job.id) or {}
            restarts = row.get("restarts") or 0
            if row.get("mode") == "async" and row.get("payload") and restarts < 1:
                # async-задачу — один раз в очередь заново: после перезапуска её подхватит start()
                self.store.update(job.id, status="queued", started_at=None, created_at=now,
                                  finished_at=None, restarts=restarts + 1,
                                  error=f"Повтор после зависания: {reason}"[:2000])
                job.released = True
                logger.info("Очередь: задача %s будет повторена после перезапуска", job.id)
            else:
                self.store.update(job.id, restarts=restarts + 1)
                if job.released:
                    self.store.update(job.id, status="timeout",
                                      error=f"Зависание, сервис перезапущен: {reason}"[:2000])
                self.release(job, "timeout", f"Зависание, сервис перезапущен: {reason}")
        if self.on_hang is not None:
            threading.Thread(target=self.on_hang, args=(f"зависание задачи: {reason}",),
                             name="aim-hang", daemon=True).start()

    # ---------- планировщик ----------

    def _grant(self, t: Ticket) -> None:
        self._waiting.remove(t)
        self._running[t.id] = t
        t.running = True
        t.granted.set()

    def _schedule(self) -> None:
        if self._maintenance:
            return
        pending = [t for t in self._waiting if not t.stop_requested]
        if not pending:
            return
        current = self.current_model()
        if self._running:
            # GPU занят текущей моделью: добираем её задачи до числа слотов
            target = current
        elif current and any(t.model == current for t in pending):
            target = current                     # по активной модели ещё есть задачи — не переключаемся
        elif self._holding(current, pending):
            return                               # удержание: проект, вероятно, пришлёт следующий шаг
        else:
            target = pending[0].model            # самая старая задача к другой модели
        free = self._slots(target) - len(self._running)
        for t in pending:
            if free <= 0:
                break
            if t.model == target:
                self._grant(t)
                free -= 1

    def _start_hold(self, t: Ticket) -> None:
        """После успешно отработавшей задачи держим её модель ещё sticky_sec секунд."""
        if self.sticky_sec <= 0:
            return
        self._hold = {"model": t.model, "project": t.project,
                      "until": time.monotonic() + self.sticky_sec}

    def _holding(self, current: Optional[str], pending: List[Ticket]) -> bool:
        """
        True — не переключаться: удержание по текущей модели ещё идёт, и задачи к другим
        моделям ждут меньше sticky_max. По окончании удержания планировщик вызывается снова.
        """
        hold = self._hold
        if not hold or hold["model"] != current:
            return False
        now = time.monotonic()
        left = hold["until"] - now
        oldest = min(t.created for t in pending)
        if left <= 0 or now - oldest >= self.sticky_max:
            self._hold = None
            return False
        # проснуться к концу удержания или к пределу ожидания — что раньше
        delay = min(left, self.sticky_max - (now - oldest)) + 0.05
        if self._hold_timer is not None:
            self._hold_timer.cancel()
        try:
            self._hold_timer = asyncio.get_running_loop().call_later(delay, self._schedule)
        except RuntimeError:  # вне event loop (не должно случаться) — без удержания
            self._hold = None
            return False
        return True

    def busy_with(self, model: Optional[str]) -> int:
        """Сколько задач (ожидающих и выполняющихся) приходится на модель."""
        if not model:
            return 0
        return (sum(1 for t in self._running.values() if t.model == model)
                + sum(1 for t in self._waiting if t.model == model and not t.stop_requested))

    # ---------- ручное управление моделями ----------

    async def manual(self, fn: Callable[..., Any], *args: Any) -> Any:
        """
        Ручной start/stop: разрешён, только если по активной модели нет задач
        и ничего не выполняется. На время операции очередь не выдаёт слоты.
        """
        current = self.current_model()
        busy = self.busy_with(current)  # выполняющиеся задачи всегда относятся к текущей модели
        if self._maintenance or busy:
            raise ModelBusyError(
                f"Модель '{current}' занята: задач в работе/очереди — {busy}. "
                f"Переключение запрещено до их завершения (GET /api/queue)")
        self._maintenance = "manual"
        try:
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)
        finally:
            self._maintenance = None
            self._schedule()

    # ---------- async-задачи (POST /api/jobs) ----------

    def submit(self, *, kind: str, model: str, project: str, payload: dict, summary: str = "",
               wait_timeout: Optional[float] = None, run_timeout: Optional[float] = None) -> Ticket:
        if kind not in self._executors:
            raise ValueError(f"Неизвестный вид задачи: {kind}")
        t = self.new_ticket(kind=kind, model=model, project=project, summary=summary,
                            wait_timeout=wait_timeout, run_timeout=run_timeout, payload=payload)
        self._async_tasks[t.id] = asyncio.create_task(self._run_async(t, payload))
        return t

    def _spawn_async(self, job: dict) -> None:
        elapsed = max(0.0, time.time() - job["created_at"])
        t = self.new_ticket(kind=job["kind"], model=job["model"], project=job["project"],
                            wait_timeout=job["wait_timeout"], run_timeout=job["run_timeout"],
                            job_id=job["id"])
        t.created -= elapsed  # срок ожидания считается от исходной постановки
        self._async_tasks[t.id] = asyncio.create_task(self._run_async(t, job.get("payload") or {}))

    async def _run_async(self, t: Ticket, payload: dict) -> None:
        try:
            await self.acquire(t)
        except BaseException:
            self._async_tasks.pop(t.id, None)
            return
        try:
            result = await self._executors[t.kind](self, t, payload)
            self.release(t, "done", usage=result.get("usage") if isinstance(result, dict) else None,
                         result=result)
        except JobCancelled as e:
            self.release(t, e.status, str(e))
        except asyncio.CancelledError:
            self.release(t, "cancelled", "Сервис останавливается")
        except Exception as e:
            self.release(t, "failed", str(e))
        finally:
            self._async_tasks.pop(t.id, None)

    # ---------- обзор ----------

    def snapshot(self) -> dict:
        now = time.monotonic()

        def brief(t: Ticket) -> dict:
            d = {"id": t.id, "kind": t.kind, "model": t.model, "project": t.project,
                 "age_sec": round(now - t.created, 1)}
            if t.deadline is not None:
                d["run_left_sec"] = round(t.deadline - now, 1)
            if t.stop_reason:
                d["stopping"] = t.stop_reason.status
            return d

        waiting = [t for t in self._waiting if not t.stop_requested]
        by_model: Dict[str, int] = {}
        for t in waiting:
            by_model[t.model] = by_model.get(t.model, 0) + 1
        hold = self._hold
        if hold and (hold["until"] <= now or hold["model"] != self.current_model()):
            hold = None
        return {
            "active": self.manager.active_name(),
            "current": self.current_model(),
            "hold": {"model": hold["model"], "project": hold["project"],
                     "left_sec": round(hold["until"] - now, 1)} if hold else None,
            "switch_locked": bool(self._running) or self.busy_with(self.current_model()) > 0,
            "maintenance": self._maintenance,
            "running": [brief(t) for t in self._running.values()],
            "waiting": [dict(brief(t), position=i + 1) for i, t in enumerate(waiting)],
            "waiting_by_model": by_model,
        }

    def position(self, job_id: str) -> Optional[int]:
        waiting = [t for t in self._waiting if not t.stop_requested]
        for i, t in enumerate(waiting):
            if t.id == job_id:
                return i + 1
        return None


def _cancelling() -> bool:
    """True, если отменяют саму текущую задачу (клиент отключился), а не только child-работу."""
    task = asyncio.current_task()
    return bool(task and task.cancelling())
