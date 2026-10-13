"""Сводка по делегированию: сколько токенов облачного агента сэкономлено (оценка).

Запуск: venv\\Scripts\\python delegate\\stats.py
Сэкономлено ≈ то, что агент прочитал бы сам (файлы) + написал бы сам (результат);
потрачено ≈ постановка задачи + то, что вернулось агенту + накладные на вызов.
"""
import json
import sys
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "data" / "delegate_log.jsonl"
CPT = 3.0          # символов на токен (оценка)
CALL_OVERHEAD = 150  # токенов: обёртка tool_use/tool_result, пути файлов

sys.stdout.reconfigure(encoding="utf-8")
rows = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()] if LOG.exists() else []
if not rows:
    sys.exit("Лог пуст: data/delegate_log.jsonl")

saved = spent = local_sec = 0.0
by_model: dict[str, int] = {}
for r in rows:
    saved += (r["files_chars"] + r["result_chars"]) / CPT
    spent += (r["task_chars"] + r["returned_chars"]) / CPT + CALL_OVERHEAD
    local_sec += r["sec"]
    by_model[r["model"]] = by_model.get(r["model"], 0) + 1

print(f"Вызовов: {len(rows)}  ({', '.join(f'{m}: {n}' for m, n in by_model.items())})")
print(f"Время локальных моделей: {local_sec / 60:.1f} мин")
print(f"Без делегирования агент потратил бы ≈ {saved:,.0f} ток.")
print(f"На делегирование потрачено       ≈ {spent:,.0f} ток.")
print(f"Чистая экономия                  ≈ {saved - spent:,.0f} ток. ({(1 - spent / saved) * 100 if saved else 0:.0f}%)")
