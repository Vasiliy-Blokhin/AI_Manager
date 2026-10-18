"""
Смоук-тест защиты от осиротевших llama-server (без GPU и без настоящего llama-server).
Вместо него — копия ping.exe под уникальным именем, чтобы не задеть чужие процессы.

Запуск:  venv\\Scripts\\python -m tests.orphans_smoke
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

from app.orphans import find_orphans, kill_orphans

NAME = "aim-orphan-test.exe"
TMP = Path(tempfile.mkdtemp(prefix="aim-orphans-"))
EXE = TMP / NAME
shutil.copy(Path(r"C:\Windows\System32\PING.EXE"), EXE)
ROOT = Path(__file__).resolve().parents[1]

PARENT = f"""
import subprocess, sys, os
sys.path.insert(0, {str(ROOT)!r})
from app.orphans import bind_to_service
p = subprocess.Popen([{str(EXE)!r}, '-n', '120', '127.0.0.1'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if sys.argv[1] == 'bind':
    assert bind_to_service(p.pid)
print(p.pid, flush=True)
os._exit(0)  # «падение» сервиса: дочерний процесс остаётся без родителя
"""


def spawn(mode: str) -> int:
    out = subprocess.run([sys.executable, "-c", PARENT, mode], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.split()[-1])


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def main() -> None:
    # 1. Без Job Object ребёнок переживает родителя — уборщик его находит и завершает
    pid = spawn("plain")
    time.sleep(1)
    assert alive(pid), "ребёнок должен пережить родителя"
    assert pid in [p.pid for p in find_orphans(NAME)], "сирота не найден"
    assert pid in kill_orphans(NAME), "сирота не завершён"
    assert not alive(pid)
    print("OK: уборщик завершает осиротевший процесс")

    # 2. Живой родитель — не сирота
    me = subprocess.Popen([str(EXE), "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL)
    try:
        assert me.pid not in [p.pid for p in find_orphans(NAME)], "процесс с живым родителем принят за сироту"
        print("OK: процесс с живым родителем не трогается")
    finally:
        me.kill()

    # 3. С Job Object ребёнок умирает вместе с родителем
    pid = spawn("bind")
    deadline = time.time() + 10
    while alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not alive(pid), "Job Object не завершил ребёнка вместе с родителем"
    print("OK: Job Object завершает llama-server вместе с сервисом")

    shutil.rmtree(TMP, ignore_errors=True)
    print("Все проверки пройдены")


if __name__ == "__main__":
    main()
