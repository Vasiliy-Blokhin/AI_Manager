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
    "code": _cfg("DELEGATE_CODE_MODEL", "qwen3-coder-30b-a3b-instruct"),
    "text": _cfg("DELEGATE_TEXT_MODEL", "qwen36-35b-a3b"),
    "reason": _cfg("DELEGATE_REASON_MODEL", "gpt-oss-20b"),
}
DEFAULT_MODEL = _cfg("DELEGATE_DEFAULT_MODEL", ALIASES["text"])
TIMEOUT = int(_cfg("DELEGATE_TIMEOUT", "1800"))
CHARS_PER_TOKEN = 3.0  # грубая оценка для смеси кода/русского/английского

SYSTEM_PROMPT = (
    "You are a precise worker executing a task delegated by a senior engineer. "
    "Follow the instruction exactly. Output only the requested result: no preamble, "
    "no explanations, no closing remarks unless asked. Answer in the language of the task."
)
SYSTEM_FILE = (
    " Your output will be written verbatim to a file: output only the complete file content, "
    "without markdown code fences."
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
        "File contents are read by the server and never enter your context. "
        "Slow (seconds to minutes; model switch adds ~30-60s). Quality ~ small model: "
        "spot-check results."
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
                "description": "'code' (qwen3-coder-30b, 100k ctx), 'text' (qwen3.6-35b, RU text), "
                               "'reason' (gpt-oss-20b) or exact name. Default: the active model (no switch).",
            },
            "max_return_chars": {"type": "integer", "description": "Truncate returned text (default 4000)."},
        },
        "required": ["task"],
    },
}


# ---------------------------------------------------------------- AI Manager

def _http(method: str, path: str, body: dict | None = None, timeout: float = 15) -> dict:
    req = urllib.request.Request(
        AIM_URL + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", "X-API-Password": AIM_PASSWORD,
                 "Authorization": f"Bearer {AIM_PASSWORD}", "X-Project": PROJECT,
                 "X-Job-Timeout": str(TIMEOUT), "X-Job-Wait-Timeout": str(TIMEOUT)},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"AI Manager HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"AI Manager недоступен ({AIM_URL}): {e.reason}")


def _registry() -> dict[str, dict]:
    path = ROOT / _cfg("AIM_REGISTRY", "config/models.registry.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = data.get("models", data) if isinstance(data, dict) else data
    items = models.values() if isinstance(models, dict) else models
    return {m["name"]: m for m in items if isinstance(m, dict) and "name" in m}


def _pick_model(requested: str | None) -> str:
    if requested:
        return ALIASES.get(requested, requested)
    try:  # активная текстовая модель — без переключения GPU
        st = _http("GET", "/api/status", timeout=5)
        if st.get("active_type") == "text" and st.get("active"):
            return st["active"]
    except RuntimeError:
        pass
    return DEFAULT_MODEL


# ---------------------------------------------------------------- tool

_RANGE = re.compile(r"^(.*):(\d+)-(\d+)$")


def _read_file(spec: str) -> tuple[str, str]:
    m = _RANGE.match(spec)
    path, lo, hi = (m.group(1), int(m.group(2)), int(m.group(3))) if m else (spec, None, None)
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"Файл не найден: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if lo is not None:
        text = "\n".join(text.splitlines()[lo - 1:hi])
        spec = f"{path} (lines {lo}-{hi})"
    return spec, text


def _strip_fences(text: str) -> str:
    m = re.match(r"^\s*```[\w+-]*\n(.*?)\n```\s*$", text, re.S)
    return (m.group(1) if m else text.strip()) + "\n"


def run_local_llm(args: dict) -> str:
    task = (args.get("task") or "").strip()
    if not task:
        raise RuntimeError("Пустое поле task")
    out_path = args.get("output_file")
    max_ret = int(args.get("max_return_chars") or 4000)
    model = _pick_model(args.get("model"))
    ctx = int(_registry().get(model, {}).get("context") or 16384)

    parts, in_chars = [], 0
    for spec in args.get("files") or []:
        name, text = _read_file(spec)
        in_chars += len(text)
        parts.append(f"=== FILE: {name} ===\n{text}\n=== END FILE ===")
    prompt = ("\n\n".join(parts) + "\n\n" if parts else "") + "TASK:\n" + task

    in_tokens = int(len(prompt) / CHARS_PER_TOKEN) + 200
    if out_path:  # перезапись файла: ответ размером примерно с вход
        max_tokens = min(ctx - in_tokens, max(4096, int(in_chars / CHARS_PER_TOKEN * 1.3) + 1024))
    else:
        max_tokens = min(ctx - in_tokens, 8192)
    if max_tokens < 512:
        raise RuntimeError(
            f"Не влезает в контекст {model} ({ctx} ток.): вход ~{in_tokens} ток. "
            "Разбейте на части (диапазоны строк 'path:START-END') или возьмите model='code' (100k).")

    system = SYSTEM_PROMPT + (SYSTEM_FILE if out_path else "")
    t0 = time.time()
    data = _http("POST", "/v1/chat/completions", {
        "model": model, "max_tokens": max_tokens, "temperature": 0.2,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }, timeout=TIMEOUT + 60)
    dt = time.time() - t0
    choice = (data.get("choices") or [{}])[0]
    result = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")
    usage = data.get("usage") or {}
    stats = (f"[{model}, {dt:.0f}s, in {usage.get('prompt_tokens', '?')} / "
             f"out {usage.get('completion_tokens', '?')} tok]")

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
          "files_chars": in_chars, "task_chars": len(task), "returned_chars": len(returned),
          "result_chars": len(result), "output_file": bool(out_path),
          "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")})
    return f"{returned}\n{stats}"


def _log(rec: dict) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- MCP stdio

_out_lock = threading.Lock()


def _send(msg: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _call_tool(req_id, params: dict) -> None:
    if params.get("name") != TOOL["name"]:
        _send({"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}})
        return
    try:
        text, is_error = run_local_llm(params.get("arguments") or {}), False
    except Exception as e:  # ошибка инструмента — агенту текстом, а не протокольной ошибкой
        text, is_error = f"Ошибка: {e}", True
    _send({"jsonrpc": "2.0", "id": req_id,
           "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, req_id = msg.get("method"), msg.get("id")
        if req_id is None:  # уведомления (initialized, cancelled) — ответа не требуют
            continue
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ai-manager-delegate", "version": "1.0.0"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [TOOL]}})
        elif method == "tools/call":
            threading.Thread(target=_call_tool, args=(req_id, msg.get("params") or {}), daemon=True).start()
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        else:
            _send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()
