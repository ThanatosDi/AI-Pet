"""把 hook 寫入（或移出）Claude Code 的 settings.json。"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

# 事件 -> matcher（None 表示該事件不需要 matcher）
EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "*",
    "PostToolUse": "*",
    "PermissionRequest": "*",
    "Notification": None,
    "PreCompact": None,
    "Stop": None,
    "SessionEnd": None,
}

# 用來辨認哪些 hook 是 ai-pet 的：開發版用 python -m ai_pet.hook，打包版用 <程式> ai-pet-hook
MARKS = ("ai_pet.hook", "ai-pet-hook")
HOOK_SUBCOMMAND = "ai-pet-hook"


def default_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def hook_command() -> str:
    exe = Path(sys.executable).as_posix()
    if is_frozen():  # PyInstaller 打包版：由程式本身的子指令處理 hook
        return f'"{exe}" {HOOK_SUBCOMMAND}'
    return f'"{exe}" -m ai_pet.hook'


def _is_ours(hook: dict) -> bool:
    cmd = str(hook.get("command", ""))
    return any(m in cmd for m in MARKS)


def status(path: Path | None = None) -> str:
    """hooks 安裝狀態：
    "none"    沒裝
    "current" 裝的就是目前這個程式
    "other"   指向另一個還存在的 ai-pet（例如開發版），一樣能運作
    "broken"  指向的程式已不存在（例如 App 被搬走或刪除）
    """
    try:
        data = _load(path or default_settings())
    except (OSError, ValueError):
        return "none"
    current = hook_command()
    result = "none"
    for entries in (data.get("hooks") or {}).values():
        for entry in entries if isinstance(entries, list) else []:
            for h in entry.get("hooks", []):
                if not _is_ours(h):
                    continue
                cmd = str(h.get("command", ""))
                if cmd == current:
                    return "current"
                m = re.match(r'\s*"([^"]+)"', cmd)
                exists = bool(m) and Path(m.group(1)).exists()
                if exists:
                    result = "other"
                elif result == "none":
                    result = "broken"
    return result


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else {}


def _save(path: Path, data: dict) -> Path | None:
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return backup


def _strip(hooks: dict) -> None:
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            items = [h for h in entry.get("hooks", []) if not _is_ours(h)]
            if items:
                kept.append({**entry, "hooks": items})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]


def install(path: Path | None = None) -> str:
    """寫入 hooks，回傳給使用者看的說明文字。"""
    path = path or default_settings()
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    _strip(hooks)
    cmd = hook_command()
    for event, matcher in EVENTS.items():
        entry: dict = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if matcher is not None:
            entry = {"matcher": matcher, **entry}
        hooks.setdefault(event, []).append(entry)
    backup = _save(path, data)
    lines = [f"已寫入 hooks：{path}"]
    if backup:
        lines.append(f"原檔備份：{backup}")
    lines += [f"hook 指令：{cmd}", "新開的 Claude Code session 才會生效。"]
    return "\n".join(lines)


def uninstall(path: Path | None = None) -> str:
    path = path or default_settings()
    data = _load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return "沒有找到 hooks，不需移除。"
    _strip(hooks)
    if not hooks:
        del data["hooks"]
    backup = _save(path, data)
    lines = [f"已移除 ai-pet hooks：{path}"]
    if backup:
        lines.append(f"原檔備份：{backup}")
    return "\n".join(lines)
