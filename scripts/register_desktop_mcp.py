"""Добавляет MCP-сервер делегирования (delegate/server.py) в конфиг Claude Desktop.

Версия из Microsoft Store читает конфиг из папки пакета (LocalCache), обычная — из %APPDATA%:
пишем во все найденные. Приложение должно быть закрыто — иначе при выходе оно перезапишет файл.
"""
import glob
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = {"command": str(ROOT / "venv" / "Scripts" / "python.exe"),
          "args": [str(ROOT / "delegate" / "server.py")]}

paths = [os.path.expandvars(r"%APPDATA%\Claude\claude_desktop_config.json")]
paths += glob.glob(os.path.expandvars(
    r"%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json"))

for p in paths:
    data = {}
    if os.path.exists(p):
        shutil.copy(p, p + ".bak")
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    data.setdefault("mcpServers", {})["ai-manager-delegate"] = SERVER
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p + ".tmp").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(p + ".tmp", p)
    print("OK:", p)
