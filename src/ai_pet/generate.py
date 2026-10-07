"""呼叫 Claude Code（claude -p）把使用者的圖片畫成會動的吉祥物。"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from . import rig
from . import state as st

TIMEOUT_MS = 15 * 60 * 1000
MAX_ATTEMPTS = 3  # 第一次產生 + 最多兩次依驗證錯誤修正

PROMPT = """You are turning a user's mascot picture into an animated desktop pet for a status indicator app. The current directory contains `{image}` (the user's picture). Read it first.

Create exactly `mascot.svg` and `rig.json` in the current directory following the spec below, then stop. Do not create any other files. When both files are written, reply with one short line.

<spec>
{spec}
</spec>

User's wishes for the states (may be empty, may be in Chinese):
<<<
{actions}
>>>
"""

FIX_PROMPT = """\
The app validated your files and found these problems:
{errors}

Fix `mascot.svg` and/or `rig.json` in place so they satisfy the rules, then reply with one short line.
"""


def spec_text() -> str:
    return (Path(__file__).parent / "mascot_spec.md").read_text(encoding="utf-8")


def new_folder(image: Path, actions: str = "") -> Path:
    """建立 ~/.ai-pet/mascots/<時間>/，放入原圖副本與動作描述。"""
    base = st.home() / "mascots"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = base / stamp
    n = 1
    while folder.exists():
        n += 1
        folder = base / f"{stamp}-{n}"
    folder.mkdir(parents=True)
    shutil.copy2(image, folder / f"source{image.suffix.lower()}")
    (folder / "actions.txt").write_text(actions.strip(), encoding="utf-8")
    return folder


def _extra_paths() -> list[str]:
    """從 Finder / 開始選單啟動的 App 拿到的 PATH 很精簡，補上 claude 常見的安裝位置。"""
    home = Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".claude" / "local",
        home / ".npm-global" / "bin",
        home / ".bun" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    if appdata := os.environ.get("APPDATA"):
        candidates.append(Path(appdata) / "npm")
    return [str(p) for p in candidates if p.is_dir()]


def search_path() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(parts + [p for p in _extra_paths() if p not in parts])


def find_claude() -> str | None:
    return shutil.which("claude", path=search_path())


class Generator(QObject):
    """非同步執行 claude -p；結束時發出 finished(成功與否, 資料夾或錯誤訊息)。"""

    finished = Signal(bool, str)

    def __init__(self, image: Path, actions: str, parent: QObject | None = None):
        super().__init__(parent)
        self.image = image
        self.actions = actions.strip()
        self.folder: Path | None = None
        self.started_at = time.monotonic()
        self.attempt = 0
        self.proc: QProcess | None = None
        self._timer = QTimer(self, singleShot=True, interval=TIMEOUT_MS)
        self._timer.timeout.connect(self._on_timeout)
        self._cancelled = False

    def start(self) -> None:
        exe = find_claude()
        if exe is None:
            self.finished.emit(False, "找不到 claude 指令，請確認 Claude Code 已安裝並在 PATH 中。")
            return
        self.exe = exe
        self.folder = new_folder(self.image, self.actions)
        source = next(self.folder.glob("source.*")).name
        prompt = PROMPT.format(image=source, spec=spec_text(), actions=self.actions or "(none)")
        self._run(prompt)

    def cancel(self) -> None:
        self._cancelled = True
        self._timer.stop()
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)

    def _run(self, prompt: str, resume: str | None = None) -> None:
        self.attempt += 1
        proc = QProcess(self)
        proc.setWorkingDirectory(str(self.folder))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("AI_PET_DISABLE_HOOK", "1")  # 別讓這個 claude 本身觸發吉祥物狀態
        env.insert("PATH", search_path())  # npm 安裝的 claude 需要找得到 node
        proc.setProcessEnvironment(env)
        args = [
            "-p",
            "--output-format", "json",
            "--tools", "Read,Write,Edit",
            "--permission-mode", "acceptEdits",
        ]
        if resume:
            args += ["--resume", resume]
        program = self.exe
        if Path(program).suffix.lower() in (".cmd", ".bat"):
            args = ["/c", program, *args]
            program = "cmd.exe"
        proc.finished.connect(self._on_finished)
        self.proc = proc
        proc.start(program, args)
        # prompt 從 stdin 傳入，避免命令列跳脫與長度問題
        proc.write(prompt.encode("utf-8"))
        proc.closeWriteChannel()
        self._timer.start()

    def _on_timeout(self) -> None:
        if self.proc is not None:
            self.proc.kill()
        self.finished.emit(False, "Claude 超過 15 分鐘沒有完成，已中止。")
        self._cancelled = True

    def _on_finished(self, code: int, _status) -> None:
        self._timer.stop()
        if self._cancelled:
            return
        out = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        session_id = None
        result_text = ""
        try:
            data = json.loads(out)
            session_id = data.get("session_id")
            result_text = str(data.get("result", ""))
            if data.get("is_error"):
                self.finished.emit(False, f"Claude 執行失敗：{result_text or err}".strip())
                return
        except ValueError:
            if code != 0:
                self.finished.emit(False, f"claude 結束碼 {code}：{(err or out).strip()[:500]}")
                return

        errors = rig.validate(self.folder)
        if not errors:
            self.finished.emit(True, str(self.folder))
        elif self.attempt < MAX_ATTEMPTS and session_id:
            self._run(FIX_PROMPT.format(errors="\n".join(f"- {e}" for e in errors)), resume=session_id)
        else:
            detail = "\n".join(errors[:10])
            self.finished.emit(False, f"產生的檔案驗證失敗：\n{detail}\n\n{result_text}".strip())
