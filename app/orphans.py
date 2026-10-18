"""
Защита от осиротевших llama-server.

Если AI Manager падает или его процесс убивают снаружи (не через сторожа), запущенный им
llama-server остаётся жить: держит ~14 ГБ памяти и порт, а новый экземпляр сервиса о нём
не знает. Из-за нехватки памяти SDXL идёт по 12+ минут на кадр, ffmpeg у клиентов не может
выделить память (так было 18.10.2026).

Две меры:
1. bind_to_service(proc) — дочерний процесс кладётся в Job Object Windows с
   KILL_ON_JOB_CLOSE: когда процесс сервиса завершается (как угодно, хоть через диспетчер
   задач), Windows сама завершает и llama-server.
2. OrphanSweeper — при старте и периодически завершает llama-server, чей родитель
   уже умер (на случай серверов от старых версий сервиса или если Job Object не удался).
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import List, Optional

import psutil

logger = logging.getLogger(__name__)

LLAMA_EXE = "llama-server.exe" if sys.platform == "win32" else "llama-server"


# ---------- поиск и уборка сирот ----------

def find_orphans(name: str = LLAMA_EXE) -> List[psutil.Process]:
    """Процессы name, чей родитель умер (или PID родителя уже занят более новым процессом)."""
    found = []
    for proc in psutil.process_iter(["name", "ppid", "create_time"]):
        try:
            if (proc.info["name"] or "").lower() != name.lower():
                continue
            try:
                parent = psutil.Process(proc.info["ppid"])
                orphan = parent.create_time() > proc.info["create_time"]  # PID переиспользован
            except psutil.NoSuchProcess:
                orphan = True
            if orphan:
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def kill_orphans(name: str = LLAMA_EXE) -> List[int]:
    """Завершает осиротевшие процессы name. Возвращает PID завершённых."""
    killed = []
    for proc in find_orphans(name):
        try:
            mem = proc.memory_info()
            mb = int(getattr(mem, "private", mem.rss) / 2**20)
            proc.kill()
            proc.wait(timeout=10)
            logger.warning("Завершён осиротевший %s: PID %s, %d МБ", name, proc.pid, mb)
            killed.append(proc.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as e:
            logger.error("Не удалось завершить осиротевший %s (PID %s): %s", name, proc.pid, e)
    return killed


class OrphanSweeper:
    """Фоновая уборка: сразу при старте сервиса и затем раз в interval секунд."""

    def __init__(self, interval: float = 300.0, name: str = LLAMA_EXE):
        self.interval = interval
        self.name = name
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="aim-orphans", daemon=True).start()
        logger.info("Уборка осиротевших %s: при старте и раз в %d с", self.name, int(self.interval))

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                kill_orphans(self.name)
            except Exception:
                logger.exception("Уборка осиротевших процессов не удалась")
            self._stop.wait(self.interval)


# ---------- Job Object: дочерние процессы умирают вместе с сервисом ----------

_job: Optional[int] = None
_job_lock = threading.Lock()


def _service_job() -> Optional[int]:
    """Job Object сервиса с KILL_ON_JOB_CLOSE (один на процесс; дескриптор держится до выхода)."""
    global _job
    if sys.platform != "win32":
        return None
    with _job_lock:
        if _job is not None:
            return _job
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        _job = handle
        return _job


def bind_to_service(pid: int) -> bool:
    """Привязать дочерний процесс к сервису: умрёт сервис — Windows завершит и его. True — удалось."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        job = _service_job()
        proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k32.AssignProcessToJobObject(job, proc):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k32.CloseHandle(proc)
        return True
    except OSError as e:
        # не критично: на этот случай есть OrphanSweeper
        logger.warning("Не удалось привязать PID %s к сервису (Job Object): %s", pid, e)
        return False
