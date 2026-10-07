"""Session 狀態檔的讀寫與彙總。只用標準函式庫，讓 hook 啟動夠快。"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

STATES = ("idle", "thinking", "working", "waiting", "done", "compacting", "sleeping")

# 多個 session 同時存在時，顯示優先度最高的那個
PRIORITY = {
    "waiting": 6,
    "working": 5,
    "thinking": 4,
    "compacting": 3,
    "done": 2,
    "idle": 1,
    "sleeping": 0,
}

DONE_SECS = 20  # done 顯示多久後回到 idle
SLEEP_SECS = 600  # idle 多久後睡著
BUSY_TIMEOUT = 1800  # thinking/working 太久沒更新（例如被 Esc 中斷）視為 idle
STALE_SECS = 6 * 3600  # 超過這個時間的 session 檔直接清掉


def home() -> Path:
    return Path(os.environ.get("AI_PET_HOME") or Path.home() / ".ai-pet")


def sessions_dir() -> Path:
    return home() / "sessions"


def _safe_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")[:128]


def _atomic_write(target: Path, text: str) -> bool:
    """先寫暫存檔再整個換上，讀取端不會讀到寫一半的內容。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    # Windows 上若讀取端剛好開著檔案，replace 會失敗，稍微重試
    for _ in range(20):
        try:
            os.replace(tmp, target)
            return True
        except PermissionError:
            time.sleep(0.02)
    tmp.unlink(missing_ok=True)
    return False


def write_session(
    session_id: str, state: str, detail: str = "", cwd: str = "", transcript: str = ""
) -> None:
    data = {
        "state": state,
        "detail": detail,
        "cwd": cwd,
        "project": Path(cwd).name if cwd else "",
        "transcript": transcript,
        "ts": time.time(),
    }
    _atomic_write(sessions_dir() / f"{_safe_id(session_id)}.json", json.dumps(data, ensure_ascii=False))


EVENTS_LOG_MAX = 512 * 1024


def log_event(session_id: str, event: str, result: str) -> None:
    """每次 hook 觸發記一行到 events.log，狀態不對時可以查是哪個事件寫的。超過大小就從頭開始。"""
    path = home() / "events.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if path.exists() and path.stat().st_size > EVENTS_LOG_MAX else "a"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        with path.open(mode, encoding="utf-8") as f:
            f.write(f"{stamp}  {_safe_id(session_id)[:8]}  {event:<18} {result}\n")
    except OSError:
        pass


# ---------- 偵測沒有正常結束的回合 ----------
# 按 Esc 中斷或 API 錯誤時 Claude Code 不會觸發 Stop，狀態會停在 thinking / working。
# 讀對話紀錄（transcript）最後一筆對話，若是中斷或錯誤訊息，就當成已經結束。

INTERRUPT_CHECK_AFTER = 2.0  # 狀態停留超過幾秒才檢查，避免剛送出就誤判
_TAIL_BYTES = 64 * 1024
_turn_cache: dict[str, tuple[float, int, bool]] = {}


def _turn_aborted(transcript: str) -> bool:
    try:
        st_ = os.stat(transcript)
    except OSError:
        return False
    cached = _turn_cache.get(transcript)
    if cached and cached[0] == st_.st_mtime and cached[1] == st_.st_size:
        return cached[2]
    aborted = False
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, st_.st_size - _TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # 被截斷的第一行或寫到一半的最後一行
            if entry.get("type") not in ("user", "assistant") or entry.get("isMeta"):
                continue
            if entry.get("type") == "assistant":
                aborted = bool(entry.get("isApiErrorMessage"))
            else:
                aborted = "[Request interrupted by user" in line
            break
    except OSError:
        return False
    _turn_cache[transcript] = (st_.st_mtime, st_.st_size, aborted)
    return aborted


# ---------- 設定檔 ----------
# config.json 內容：
#   x, y     視窗位置
#   mascot   最後使用的 Claude 繪製造型資料夾
#   image    最後使用的圖片（直接使用圖片時）
# mascot 和 image 都沒有就是預設吉祥物。找不到記錄的造型時暫時顯示預設吉祥物，
# 但不清掉紀錄，檔案回來（例如外接硬碟重新接上）後下次啟動就會恢復。


class ConfigError(Exception):
    def __init__(self, message: str, corrupt: bool = False):
        super().__init__(message)
        self.corrupt = corrupt  # True：檔案內容壞掉；False：暫時讀不到


def config_path() -> Path:
    return home() / "config.json"


def load_config() -> dict:
    """讀取設定；檔案不存在回傳空設定，檔案壞掉則丟出 ConfigError。"""
    path = config_path()
    for attempt in range(5):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:  # Windows 上檔案正在被替換時可能暫時打不開
            if attempt == 4:
                raise ConfigError(str(e)) from e
            time.sleep(0.02)
            continue
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as e:
            raise ConfigError(f"{path} 不是合法 JSON：{e}", corrupt=True) from e
        if not isinstance(data, dict):
            raise ConfigError(f"{path} 最外層必須是物件", corrupt=True)
        return data
    return {}


def update_config(**values) -> dict:
    """合併寫入設定，值為 None 表示刪除該欄位。

    暫時讀不到現有設定時丟出 ConfigError、不寫入；內容壞掉的檔案先備份成 config.json.bad。
    """
    try:
        cfg = load_config()
    except ConfigError as e:
        if not e.corrupt:
            raise  # 暫時讀不到：這次不寫，免得蓋掉原本的設定
        path = config_path()
        os.replace(path, path.with_name("config.json.bad"))
        cfg = {}
    for key, value in values.items():
        if value is None:
            cfg.pop(key, None)
        else:
            cfg[key] = value
    if not _atomic_write(config_path(), json.dumps(cfg, ensure_ascii=False, indent=2)):
        raise ConfigError("無法寫入設定檔")
    return cfg


def remove_session(session_id: str) -> None:
    (sessions_dir() / f"{_safe_id(session_id)}.json").unlink(missing_ok=True)


def clear_sessions() -> None:
    d = sessions_dir()
    if d.is_dir():
        for f in d.glob("*.json"):
            f.unlink(missing_ok=True)


@dataclass
class Session:
    id: str
    state: str
    detail: str
    project: str
    ts: float
    transcript: str = ""


@dataclass
class View:
    state: str  # STATES 之一，或 "offline"（沒有任何 session）
    detail: str
    project: str
    count: int
    sessions: list[Session]


def _effective(state: str, age: float, transcript: str = "") -> str:
    if state == "done" and age > DONE_SECS:
        state = "idle"
    if state in ("thinking", "working", "compacting") and age > BUSY_TIMEOUT:
        state = "idle"
    if (
        state in ("thinking", "working", "waiting")
        and transcript
        and age > INTERRUPT_CHECK_AFTER
        and _turn_aborted(transcript)
    ):
        state = "idle"
    if state == "idle" and age > SLEEP_SECS:
        state = "sleeping"
    return state


def load_sessions(now: float | None = None) -> list[Session]:
    now = time.time() if now is None else now
    d = sessions_dir()
    out: list[Session] = []
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            ts = float(raw.get("ts", 0))
        except (OSError, ValueError, AttributeError):
            continue
        age = now - ts
        if age > STALE_SECS:
            f.unlink(missing_ok=True)
            continue
        state = raw.get("state", "idle")
        if state not in PRIORITY:
            state = "idle"
        transcript = str(raw.get("transcript", ""))
        effective = _effective(state, age, transcript)
        out.append(
            Session(
                id=f.stem,
                state=effective,
                detail=str(raw.get("detail", "")) if effective == state else "",
                project=str(raw.get("project", "")),
                ts=ts,
                transcript=transcript,
            )
        )
    return out


def aggregate(sessions: list[Session]) -> View:
    if not sessions:
        return View("offline", "", "", 0, [])
    top = max(sessions, key=lambda s: (PRIORITY[s.state], s.ts))
    return View(top.state, top.detail, top.project, len(sessions), sessions)
