"""MCP-сервер (stdio) для делегирования простых задач локальным моделям AI Manager.

Смысл — экономия токенов облачного агента (Claude Code): файлы читает сам сервер,
их содержимое уходит прямо в локальную модель и не попадает в контекст агента;
результат можно сразу записать в файл и вернуть агенту только короткую сводку.

Без зависимостей (только stdlib). Протокол — JSON-RPC 2.0 построчно через stdin/stdout.
Настройки — переменные окружения (см. delegate/README.md), пароль берётся из ../.env.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "data" / "delegate_log.jsonl"


def _dotenv() -> dict[str, str]:
    """Читает файл .env и возвращает словарь переменных окружения."""
    env: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


_ENV = _dotenv()


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name) or _ENV.get(name) or default


AIM_URL = _cfg("DELEGATE_AIM_URL", "http://127.0.0.1:" + _cfg("AIM_PORT", "8000")).rstrip("/")
AIM_PASSWORD = _cfg("AIM_PASSWORD", "")
PROJECT = _cfg("DELEGATE_PROJECT", "claude-delegate")
ALIASES = {
    "code": _cfg("DELEGATE_CODE_MODEL", "gpt-oss-20b"),
    "text": _cfg("DELEGATE_TEXT_MODEL", "qwen36-35b-a3b"),
    "reason": _cfg("DELEGATE_REASON_MODEL", "gpt-oss-20b"),
}
DEFAULT_MODEL = _cfg("DELEGATE_DEFAULT_MODEL", ALIASES["text"])
TIMEOUT = int(_cfg("DELEGATE_TIMEOUT", "3600"))            # выполнение одного запроса к модели, сек
WAIT_TIMEOUT = int(_cfg("DELEGATE_WAIT_TIMEOUT", "3600"))  # ожидание в очереди AI Manager, сек
MAX_TOKENS = int(_cfg("DELEGATE_MAX_TOKENS", "100000"))     # потолок ответа (без output_file)
MAX_CHUNKS = int(_cfg("DELEGATE_MAX_CHUNKS", "24"))        # сколько частей максимум при нарезке
LONG_LINE = int(_cfg("DELEGATE_LONG_LINE", "2000"))        # длиннее — обрезается при сжатии
HEARTBEAT = 15  # сек между notifications/progress, чтобы клиент не рвал долгий вызов
# Claude Code рвёт вызов инструмента через ~60 с (прогресс таймаут не сбрасывает), поэтому
# один вызов ждёт не дольше SYNC_WAIT, а дальше отдаёт job_id для local_llm_wait.
SYNC_WAIT = int(_cfg("DELEGATE_SYNC_WAIT", "40"))
JOB_TTL = int(_cfg("DELEGATE_JOB_TTL", "3600"))  # сколько хранить готовый результат, сек
CHARS_PER_TOKEN = 3.0  # стартовая оценка для смеси кода/русского/английского; уточняется по usage

SYSTEM_PROMPT = (
    "You are a precise worker executing a task delegated by a senior engineer. "
    "Follow the instruction exactly. Output only the requested result: no preamble, "
    "no explanations, no closing remarks unless asked. Answer in the language of the task."
)
SYSTEM_FILE = (
    " Your output will be written verbatim to a file: output only the complete file content, "
    "without markdown code fences."
)
SYSTEM_MAP = (
    " The input is split into parts; you see part {i} of {n}. Apply the task to this part only. "
    "Keep everything relevant to the task (facts, names, line numbers) - partial results will be "
    "merged later. If the part has nothing relevant, answer exactly: NOTHING."
)
SYSTEM_MAP_FILE = (
    " The file is split into parts; you see part {i} of {n}. Output only the rewritten content of "
    "this part - the parts will be concatenated in order."
)
SYSTEM_REDUCE = (
    " You get partial results of ONE task, each produced from a consecutive part of a large input. "
    "Merge them into one final answer to the task: deduplicate, keep all relevant facts, ignore "
    "parts that say NOTHING. Do not mention the parts."
)

TOOL = {
    "name": "local_llm",
    "description": (
        "Delegate a simple, well-specified task to a free local LLM (AI Manager, Arc B580). "
        "Use to save your tokens when inputs are large and the needed output is small, or the "
        "output can go straight to a file: summarize/grep-by-meaning logs and long files, "
        "extract facts, translate, write docstrings/comments, boilerplate, test data, "
        "simple scripts, rewrite text. Do NOT use for tasks needing deep reasoning across "
        "the codebase, subtle bugs, or where the result must be verified line by line anyway. "
        "File contents are read by the server and never enter your context; inputs larger than "
        "the model context are split into parts automatically (map-reduce). "
        "Slow (seconds to tens of minutes; model switch adds ~30-60s). Quality ~ small model: "
        "spot-check results. If the task is not done within ~40s the call returns a job_id: "
        "then call local_llm_wait(job_id) (repeat while it says 'running') instead of "
        "re-submitting; an identical re-submit attaches to the running job anyway."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Exact instruction. Specify output format."},
            "files": {
                "type": "array", "items": {"type": "string"},
                "description": "Absolute paths given to the model as context; 'path:START-END' for a line range.",
            },
            "output_file": {
                "type": "string",
                "description": "Write the result to this path (fences stripped) and return only a summary. "
                               "May equal an input file for in-place rewrite.",
            },
            "model": {
                "type": "string",
                "description": "'code' (gpt-oss-20b, 100k ctx), 'text' (qwen3.6-35b, RU text), "
                               "'reason' (gpt-oss-20b) or exact name. Default: the active model (no switch).",
            },
            "compact": {
                "type": "string", "enum": ["none", "light", "logs"],
                "description": "Shrink inputs before sending: 'light' (default; trailing spaces, extra blank "
                               "lines, repeated lines, very long lines), 'logs' (+ collapse lines that differ "
                               "only by numbers/timestamps), 'none'. Always 'none' with output_file.",
            },
            "line_numbers": {"type": "boolean",
                             "description": "Prefix input lines with their numbers (for 'where is X' tasks)."},
            "max_tokens": {"type": "integer", "description": f"Answer token limit (default {MAX_TOKENS})."},
            "timeout_sec": {"type": "integer", "description": f"Per-request run limit (default {TIMEOUT})."},
            "max_return_chars": {"type": "integer", "description": "Truncate returned text (default 4000)."},
        },
        "required": ["task"],
    },
}

TOOL_WAIT = {
    "name": "local_llm_wait",
    "description": (
        f"Wait up to ~{SYNC_WAIT}s for a local_llm job that is still running and return its result "
        "(or 'running' again — then call once more). Use the job_id from local_llm."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"job_id": {"type": "string", "description": "job_id returned by local_llm."}},
        "required": ["job_id"],
    },
}


# ---------------------------------------------------------------- AI Manager

def _http(method: str, path: str, body: dict | None = None, timeout: float = 15,
          run_timeout: int = TIMEOUT) -> dict:
    """Отправляет HTTP‑запрос к AIM‑сервису, обрабатывает ответ и выбрасывает исключения при ошибках."""
    req = urllib.request.Request(
        AIM_URL + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", "X-API-Password": AIM_PASSWORD,
                 "Authorization": f"Bearer {AIM_PASSWORD}", "X-Project": PROJECT,
                 "X-Job-Timeout": str(run_timeout), "X-Job-Wait-Timeout": str(WAIT_TIMEOUT)},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"AI Manager HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"AI Manager недоступен ({AIM_URL}): {e.reason}")


def _registry() -> dict[str, dict]:
    """Загружает и возвращает словарь моделей из конфигурационного файла registry.json."""
    path = ROOT / _cfg("AIM_REGISTRY", "config/models.registry.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = data.get("models", data) if isinstance(data, dict) else data
    items = models.values() if isinstance(models, dict) else models
    return {m["name"]: m for m in items if isinstance(m, dict) and "name" in m}


def _pick_model(requested: str | None) -> str:
    """Определяет модель для использования: сначала проверяет запрос, затем статус сервера, иначе возвращает
    модель по умолчанию.
    """
    if requested:
        return ALIASES.get(requested, requested)
    try:  # активная текстовая модель — без переключения GPU
        st = _http("GET", "/api/status", timeout=5)
        if st.get("active_type") == "text" and st.get("active"):
            return st["active"]
    except RuntimeError:
        pass
    return DEFAULT_MODEL


# символов на токен по моделям — уточняется по фактическим prompt_tokens
_cpt: dict[str, float] = {}


def _tokens(model: str, chars: int) -> int:
    return int(chars / _cpt.get(model, CHARS_PER_TOKEN)) + 1


def _learn_cpt(model: str, chars: int, prompt_tokens) -> None:
    """Обновляет коэффициент контекста модели, если количество токенов превышает 200, с учётом запасов."""
    if isinstance(prompt_tokens, int) and prompt_tokens > 200:
        # с запасом 10%: недооценка входа хуже переоценки (ошибка контекста у llama-server)
        _cpt[model] = min(5.0, max(1.5, chars / prompt_tokens * 0.9))


class Call:
    """Один вызов инструмента: модель, лимиты, прогресс для клиента."""

    def __init__(self, model: str, ctx: int, run_timeout: int, progress):
        self.model, self.ctx, self.run_timeout, self.progress = model, ctx, run_timeout, progress
        self.requests = 0
        self.prompt_tokens = self.completion_tokens = 0

    def chat(self, system: str, prompt: str, max_tokens: int, label: str) -> tuple[str, str | None]:
        """Отправляет чат‑запрос к AIM‑сервису, выводит прогресс, обновляет статистику и возвращает ответ и
        причину завершения.
        """
        stop = threading.Event()
        t0 = time.time()

        def beat() -> None:
            while not stop.wait(HEARTBEAT):
                self.progress(f"{label}: {time.time() - t0:.0f}s")

        threading.Thread(target=beat, daemon=True).start()
        self.progress(f"{label}: start")
        try:
            data = _http("POST", "/v1/chat/completions", {
                "model": self.model, "max_tokens": max_tokens, "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            }, timeout=self.run_timeout + WAIT_TIMEOUT + 60, run_timeout=self.run_timeout)
        finally:
            stop.set()
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        self.requests += 1
        self.prompt_tokens += usage.get("prompt_tokens") or 0
        self.completion_tokens += usage.get("completion_tokens") or 0
        _learn_cpt(self.model, len(system) + len(prompt), usage.get("prompt_tokens"))
        return (choice.get("message") or {}).get("content") or "", choice.get("finish_reason")


# ---------------------------------------------------------------- входные данные

_RANGE = re.compile(r"^(.*):(\d+)-(\d+)$")
_NUMS = re.compile(r"\d+")


def _read_file(spec: str) -> tuple[str, int, list[str]]:
    """(путь, номер первой строки, строки)."""
    m = _RANGE.match(spec)
    path, lo, hi = (m.group(1), int(m.group(2)), int(m.group(3))) if m else (spec, 1, None)
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"Файл не найден: {path}")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return path, lo, lines[lo - 1:hi]


def _compact(lines: list[str], first: int, mode: str, numbers: bool) -> list[str]:
    """Сжатие входа: меньше токенов при том же смысле. Номера строк сохраняются (если включены)."""
    out: list[str] = []
    prev_key, repeats, blank = None, 0, False

    def flush() -> None:
        """Добавляет сообщение о повторениях в вывод, если они есть, и сбрасывает счётчик."""
        nonlocal repeats
        if repeats:
            what = "похожих строк" if mode == "logs" else "повторов строки"
            out.append(f"[...ещё {repeats} {what}]")
            repeats = 0

    for i, line in enumerate(lines, first):
        if mode != "none":
            line = line.rstrip()
            if len(line) > LONG_LINE:
                line = line[:LONG_LINE] + f" [...+{len(line) - LONG_LINE} симв.]"
            key = _NUMS.sub("#", line) if mode == "logs" else line
            if line and key == prev_key:
                repeats += 1
                continue
            flush()
            if not line:
                if blank:
                    continue
                blank = True
            else:
                blank = False
            prev_key = key
        out.append(f"{i}: {line}" if numbers else line)
    flush()
    return out


def _chunks(docs: list[tuple[str, int, list[str]]], budget_chars: int) -> list[str]:
    """Упаковка файлов в части не длиннее budget_chars. Файл, не влезающий в текущую часть,
    начинает новую; слишком большой режется по строкам — по возможности на пустой строке."""
    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for path, first, lines in docs:
        start = 0
        while True:
            rest = sum(len(l) + 1 for l in lines[start:])
            room = budget_chars - cur_len - len(path) - 80
            if cur and rest > room:  # не влезает остаток — новая часть
                parts.append("\n\n".join(cur))
                cur, cur_len = [], 0
                continue
            end, size = start, 0
            while end < len(lines) and size + len(lines[end]) + 1 <= room:
                size += len(lines[end]) + 1
                end += 1
            if end < len(lines):
                blank = next((j for j in range(end - 1, start + (end - start) * 2 // 3, -1)
                              if not lines[j].strip()), None)
                if blank is not None:  # режем на границе блока, а не посреди функции/абзаца
                    size -= sum(len(l) + 1 for l in lines[blank + 1:end])
                    end = blank + 1
                end = max(end, start + 1)  # одна огромная строка — всё равно отдельной частью
            whole = start == 0 and end >= len(lines)
            name = path if whole else f"{path} (lines {first + start}-{first + end - 1})"
            cur.append(f"=== FILE: {name} ===\n" + "\n".join(lines[start:end]) + "\n=== END FILE ===")
            cur_len += size + len(name) + 40
            start = end
            if start >= len(lines):
                break
            parts.append("\n\n".join(cur))
            cur, cur_len = [], 0
    if cur:
        parts.append("\n\n".join(cur))
    return parts


def _strip_fences(text: str) -> str:
    m = re.match(r"^\s*```[\w+-]*\n(.*?)\n```\s*$", text, re.S)
    return (m.group(1) if m else text.strip()) + "\n"


# ---------------------------------------------------------------- tool

def run_local_llm(args: dict, progress=lambda msg: None) -> str:
    """Запускает локальный LLM, обрабатывая файлы, формируя запрос, разбивая на части при необходимости и
    записывая результат в файл или возвращая строку. Возвращает строку с ответом и статистикой.
    """
    task = (args.get("task") or "").strip()
    if not task:
        raise RuntimeError("Пустое поле task")
    out_path = args.get("output_file")
    max_ret = int(args.get("max_return_chars") or 4000)
    model = _pick_model(args.get("model"))
    ctx = int(_registry().get(model, {}).get("context") or 100000)
    call = Call(model, ctx, int(args.get("timeout_sec") or TIMEOUT), progress)
    mode = "none" if out_path else (args.get("compact") or "light")
    numbers = bool(args.get("line_numbers")) and not out_path

    docs, raw_chars = [], 0
    for spec in args.get("files") or []:
        path, first, lines = _read_file(spec)
        raw_chars += sum(len(l) + 1 for l in lines)
        docs.append((path, first, _compact(lines, first, mode, numbers)))
    in_chars = sum(len(l) + 1 for _, _, lines in docs for l in lines)

    system = SYSTEM_PROMPT + (SYSTEM_FILE if out_path else "")
    # Подсчёт токенов заголовка и задачи
    overhead = _tokens(model, len(system) + len(task) + 400) + 256  # шаблон чата, заголовки файлов
    in_tokens = _tokens(model, in_chars)
    if out_path:  # перезапись файла: ответ размером примерно с вход
        want_out = max(4096, int(in_tokens * 1.3) + 1024)
    else:
        want_out = int(args.get("max_tokens") or MAX_TOKENS)
    t0 = time.time()

    if overhead + in_tokens + min(want_out, ctx // 2) <= ctx or not docs:
        # всё влезает целиком — один запрос
        parts = _chunks(docs, 10 ** 12)
        prompt = (parts[0] + "\n\n" if parts else "") + "TASK:\n" + task
        max_tokens = ctx - overhead - in_tokens
        max_tokens = min(max_tokens, want_out)
        if max_tokens < 512:
            raise RuntimeError(f"Задача не влезает в контекст {model} ({ctx} ток.)")
        result, finish = call.chat(system, prompt, max_tokens, "запрос")
        n_parts = 1
    else:
        # не влезает — режем на части
        if out_path and len(docs) != 1:
            raise RuntimeError("Перезапись по частям — только для одного входного файла.")
        out_part = (ctx - overhead) // 2 if out_path else min(want_out, max(2048, ctx // 4))
        budget = (ctx - overhead - out_part) * _cpt.get(model, CHARS_PER_TOKEN)
        if out_path:  # ответ ~ вход: делим поровну между входом и выходом
            budget = (ctx - overhead) / 2.4 * _cpt.get(model, CHARS_PER_TOKEN)
        parts = _chunks(docs, int(budget))
        n_parts = len(parts)
        if n_parts > MAX_CHUNKS:
            raise RuntimeError(
                f"Вход ~{in_tokens} ток. → {n_parts} частей для {model} ({ctx} ток.), лимит {MAX_CHUNKS} "
                "(DELEGATE_MAX_CHUNKS). Сузьте диапазоны строк, compact='logs' или model='code' (100k).")
        partials, finish = [], None
        for i, part in enumerate(parts, 1):
            sys_i = system + (SYSTEM_MAP_FILE if out_path else SYSTEM_MAP).format(i=i, n=n_parts)
            text, fin = call.chat(sys_i, part + "\n\nTASK:\n" + task, out_part, f"часть {i}/{n_parts}")
            if fin == "length":
                finish = "length"
            if out_path:  # часть кончалась пустой строкой (там и резали) — вернём её при склейке
                gap = "\n\n" if part.endswith("\n\n=== END FILE ===") and i < n_parts else "\n"
                partials.append(_strip_fences(text).strip("\n") + gap)
            else:
                partials.append(text.strip())
        if out_path:
            result = "".join(partials)
        else:
            result, fin = _reduce(call, task, partials, want_out, overhead)
            finish = finish or fin
    dt = time.time() - t0
    stats = (f"[{model}, {dt:.0f}s, {call.requests} запр." + (f", {n_parts} частей" if n_parts > 1 else "")
             + f", in {call.prompt_tokens} / out {call.completion_tokens} tok"
             + (f", сжатие {raw_chars} → {in_chars} симв." if in_chars < raw_chars * 0.95 else "") + "]")

    if out_path:
        if finish == "length":
            raise RuntimeError(f"Ответ обрезан по max_tokens — файл НЕ записан. {stats}")
        content = _strip_fences(result)
        p = Path(out_path)
        old = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
        new = content.splitlines()
        if old is None:
            reply = f"Записан {out_path}: {len(new)} строк."
        else:
            old_set, new_set = set(old), set(new)
            reply = (f"Перезаписан {out_path}: {len(old)} → {len(new)} строк, "
                     f"+{sum(1 for l in new if l not in old_set)} "
                     f"-{sum(1 for l in old if l not in new_set)}. Проверьте git diff.")
        returned = reply
    else:
        returned = result.strip()
        if finish == "length":
            returned += "\n[ответ обрезан по max_tokens]"
        if len(returned) > max_ret:
            returned = returned[:max_ret] + f"\n[...обрезано, всего {len(result)} симв.]"

    _log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model, "sec": round(dt, 1),
          "files_chars": raw_chars, "sent_chars": in_chars, "task_chars": len(task),
          "returned_chars": len(returned), "result_chars": len(result), "output_file": bool(out_path),
          "parts": n_parts, "requests": call.requests,
          "prompt_tokens": call.prompt_tokens, "completion_tokens": call.completion_tokens})
    return f"{returned}\n{stats}"


def _reduce(call: Call, task: str, partials: list[str], want_out: int, overhead: int) -> tuple[str, str | None]:
    """Слияние частичных ответов; если они сами не влезают — сливаем группами, по кругу."""
    partials = [p for p in partials if p and p.strip().upper() != "NOTHING"] or ["NOTHING"]
    system = SYSTEM_PROMPT + SYSTEM_REDUCE
    out = min(want_out, call.ctx // 3)
    budget = int((call.ctx - overhead - out) * _cpt.get(call.model, CHARS_PER_TOKEN))
    level, finish = 1, None
    cap = budget // 2  # любые две части гарантированно сливаются за один запрос
    while len(partials) > 1:
        groups, cur, cur_len = [], [], 0
        for p in partials:
            p = p if len(p) <= cap else p[:cap] + "\n[...обрезано]"
            if cur and cur_len + len(p) + 40 > budget:
                groups.append(cur)
                cur, cur_len = [], 0
            cur.append(p)
            cur_len += len(p) + 40
        groups.append(cur)
        merged = []
        for gi, g in enumerate(groups, 1):
            if len(g) == 1:
                merged.append(g[0])
                continue
            prompt = "\n\n".join(f"=== PARTIAL RESULT {i} ===\n{p}" for i, p in enumerate(g, 1))
            text, fin = call.chat(system, prompt + "\n\nTASK:\n" + task, out,
                                  f"слияние {level}.{gi}/{len(groups)}")
            finish = fin if fin == "length" else finish
            merged.append(text.strip())
        partials, level = merged, level + 1
    return partials[0], finish


def _log(rec: dict) -> None:
    """Записывает словарь в логовый файл в формате JSON, создавая директорию при необходимости. Игнорирует
    ошибки I/O.
    """
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- MCP stdio

_out_lock = threading.Lock()


def _send(msg: dict) -> None:
    """Отправляет сообщение в stdout в виде JSON‑строки и очищает буфер. Используется для коммуникации с
    внешним агентом.
    """
    with _out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class Job:
    """Задача local_llm в фоновом потоке: переживает обрыв вызова клиентом по таймауту."""

    def __init__(self, job_id: str, key: str, args: dict):
        self.id, self.key, self.args = job_id, key, args
        self.t0 = time.time()
        self.done = threading.Event()
        self.text, self.is_error, self.delivered = "", False, False
        self.stage = "в очереди"
        self.finished_at = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        """Выполняет задачу в отдельном потоке, обновляя статус прогресса и обрабатывая исключения, логируя
        ошибки.
        """
        def progress(message: str) -> None:
            self.stage = message

        try:
            self.text = run_local_llm(self.args, progress)
        except Exception as e:  # ошибка инструмента — агенту текстом, а не протокольной ошибкой
            self.text, self.is_error = f"Ошибка: {e}", True
            _log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "error", "job": self.id,
                  "sec": round(time.time() - self.t0, 1), "task_chars": len(self.args.get("task") or ""),
                  "error": str(e)[:300]})
        self.finished_at = time.time()
        self.done.set()


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _purge() -> None:
    """Удаляет завершённые задания из кэша, если они не были доставлены в течение заданного времени, и
    логирует их статус.
    """
    now = time.time()
    for jid, job in list(_jobs.items()):
        if job.done.is_set() and now - job.finished_at > JOB_TTL:
            if not job.delivered:  # результат так никто и не забрал — видно в статистике
                _log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "abandoned", "job": jid,
                      "sec": round(job.finished_at - job.t0, 1)})
            del _jobs[jid]


def _submit(args: dict) -> Job:
    """Создаёт или возвращает существующее задание, избегая дублирования одних и тех же параметров. Возвращает
    объект Job.
    """
    key = json.dumps(args, sort_keys=True, ensure_ascii=False)
    with _jobs_lock:
        _purge()
        # повтор той же задачи (агент не дождался ответа) — подключаемся к идущей/готовой,
        # а не ставим вторую копию в очередь AI Manager за первой
        for job in _jobs.values():
            if job.key == key and not (job.done.is_set() and job.delivered):
                return job
        job = Job(f"j{int(time.time() * 1000) % 10 ** 9:09d}", key, args)
        _jobs[job.id] = job
        return job


def _await(job: Job, progress) -> tuple[str, bool]:
    """Ожидает завершения задания, обновляя прогресс, и возвращает результат либо сообщение о длительном
    выполнении.
    """
    deadline = time.time() + SYNC_WAIT
    while not job.done.wait(min(HEARTBEAT, max(0.0, deadline - time.time()))):
        if time.time() >= deadline:
            return (f"running: job_id={job.id}, прошло {time.time() - job.t0:.0f}s, этап: {job.stage}. "
                    f"Задача выполняется в фоне. Вызови local_llm_wait(job_id=\"{job.id}\") — "
                    "не перезапускай local_llm."), False
        progress(f"{job.stage} ({time.time() - job.t0:.0f}s)")
    job.delivered = True
    return job.text, job.is_error


def _call_tool(req_id, params: dict) -> None:
    """Обрабатывает вызов инструмента: извлекает имя и аргументы, запускает задачу или ждёт её завершения,
    отправляет прогресс и результат клиенту.
    """
    name = params.get("name")
    args = params.get("arguments") or {}
    token = (params.get("_meta") or {}).get("progressToken")
    counter = [0]

    def progress(message: str) -> None:
        """Отправляет уведомление о прогрессе, если задан токен; увеличивает счётчик и передаёт сообщение
        клиенту.
        """
        # heartbeat: клиенты с resetTimeoutOnProgress не обрывают долгий вызов по таймауту
        if token is not None:
            counter[0] += 1
            _send({"jsonrpc": "2.0", "method": "notifications/progress",
                   "params": {"progressToken": token, "progress": counter[0], "message": message}})

    if name == TOOL["name"]:
        if not (args.get("task") or "").strip():
            text, is_error = "Ошибка: Пустое поле task", True
        else:
            text, is_error = _await(_submit(args), progress)
    elif name == TOOL_WAIT["name"]:
        with _jobs_lock:
            job = _jobs.get(str(args.get("job_id") or ""))
        if job is None:
            text, is_error = (f"Ошибка: нет задачи {args.get('job_id')!r} (сервер перезапускался "
                              "или результат уже забран больше часа назад)."), True
        else:
            text, is_error = _await(job, progress)
    else:
        _send({"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
        return
    _send({"jsonrpc": "2.0", "id": req_id,
           "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def main() -> None:
    """Главный цикл сервера: читает JSON‑сообщения из stdin, обрабатывает методы initialize, tools/list,
    tools/call, ping и отправляет ответы или ошибки.
    """
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line.lstrip("﻿"))  # BOM, если stdin пришёл из PowerShell
        except ValueError:
            continue
        method, req_id = msg.get("method"), msg.get("id")
        if req_id is None:  # уведомления (initialized, cancelled) — ответа не требуют
            continue
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ai-manager-delegate", "version": "1.2.0"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [TOOL, TOOL_WAIT]}})
        elif method == "tools/call":
            threading.Thread(target=_call_tool, args=(req_id, msg.get("params") or {}), daemon=True).start()
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        else:
            _send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()
