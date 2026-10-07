"""Claude Code hook 進入點：從 stdin 讀事件 JSON，寫入狀態檔。

永遠 exit 0 且不輸出任何東西，避免干擾 Claude Code。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import PurePath
from urllib.parse import urlparse

from . import state as st


def _tool_detail(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return name
    arg = ""
    if name == "Bash":
        arg = inp.get("command", "")
    elif "file_path" in inp:
        arg = PurePath(str(inp["file_path"])).name
    elif "notebook_path" in inp:
        arg = PurePath(str(inp["notebook_path"])).name
    elif name in ("Grep", "Glob"):
        arg = inp.get("pattern", "")
    elif name == "WebFetch":
        arg = urlparse(str(inp.get("url", ""))).netloc
    elif name == "WebSearch":
        arg = inp.get("query", "")
    elif name in ("Task", "Agent"):
        arg = inp.get("description", "")
    arg = " ".join(str(arg).split())
    return f"{name}: {arg}" if arg else name


def handle(ev: dict) -> None:
    name = ev.get("hook_event_name", "")
    sid = ev.get("session_id", "unknown")
    cwd = ev.get("cwd", "")
    transcript = ev.get("transcript_path", "")

    def put(state: str, detail: str = "") -> None:
        st.write_session(sid, state, detail, cwd, transcript)
        st.log_event(sid, name, f"-> {state} {detail}".rstrip())

    if name == "SessionStart":
        put("idle")
    elif name == "UserPromptSubmit":
        put("thinking")
    elif name == "PreToolUse":
        put("working", _tool_detail(ev.get("tool_name", ""), ev.get("tool_input", {})))
    # SubagentStop 刻意不處理：Claude Code 在一輪結束後還會跑背景小任務（例如產生標題），
    # 它們結束時也觸發 SubagentStop，若當成 thinking 會蓋掉剛寫入的 done。
    # 主對話用 Agent 工具時，子代理結束後會有 PostToolUse，狀態一樣會更新。
    elif name in ("PostToolUse", "PostToolUseFailure"):
        put("thinking")
    elif name == "PermissionRequest":
        put("waiting", _tool_detail(ev.get("tool_name", ""), ev.get("tool_input", {})))
    elif name == "Notification":
        kind = ev.get("notification_type", "")
        msg = str(ev.get("message", ""))
        low = msg.lower()
        if kind == "permission_prompt" or (not kind and "permission" in low):
            put("waiting", msg)
        elif kind == "idle_prompt" or (not kind and "waiting for your input" in low):
            put("idle")
    elif name == "PreCompact":
        put("compacting")
    elif name == "Stop":
        put("done")
    elif name == "SessionEnd":
        st.remove_session(sid)
        st.log_event(sid, name, "-> (removed)")
    else:
        st.log_event(sid, name or "?", "(ignored)")


def main() -> None:
    # 吉祥物自己呼叫 claude 產生造型時，不要回報狀態
    if os.environ.get("AI_PET_DISABLE_HOOK"):
        sys.exit(0)
    try:
        raw = sys.stdin.buffer.read()
        ev = json.loads(raw) if raw.strip() else {}
        if isinstance(ev, dict):
            handle(ev)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
