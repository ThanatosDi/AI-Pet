"""在背景呼叫 Claude Code（claude -p）產生吉祥物的檔案。

- Generator：把使用者的圖片畫成會動的吉祥物（mascot.svg + rig.json）。
- Live2DMotionJob：替 Live2D 模型產生各狀態的動作檔（aipet.json）。
兩者共用 ClaudeJob：執行 claude、驗證產物、有錯時把錯誤交回同一個對話修正。
"""

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

LIVE2D_PROMPT = """You are writing the animation file for a Live2D model that is used as a desktop pet showing what Claude Code is doing. The current directory contains:
- `info.json`: the model's parameters (id, author's display name, group, min, max, default).
- `sheet.png`: the model rendered with each parameter at its min and max. Read it as an image to see what every parameter really does.
{current_note}
Read the spec below, then `info.json` and `sheet.png`. Write `aipet.json` in the current directory following the spec. Do not create or modify any other files. Put the user's wishes verbatim in the "request" field. When the file is written, reply with one short line.

<spec>
{spec}
</spec>

User's wishes for the states (may be empty, may be in Chinese):
<<<
{request}
>>>
"""

FIX_PROMPT = """\
The app validated your files and found these problems:
{errors}

Fix {files} in place so they satisfy the rules, then reply with one short line.
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


class ClaudeJob(QObject):
    """非同步執行 claude -p；結束時發出 finished(成功與否, 結果或錯誤訊息)。

    子類別實作 prepare()（建立工作資料夾、回傳 prompt）、validate()（回傳錯誤清單）
    與 complete()（驗證通過後的收尾，回傳結果字串）。
    """

    finished = Signal(bool, str)
    label = "Claude 處理中"  # 吉祥物底下顯示的進度文字
    files = "the files"  # 修正提示裡要 Claude 修的檔案

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.folder: Path | None = None
        self.started_at = time.monotonic()
        self.attempt = 0
        self.proc: QProcess | None = None
        self._timer = QTimer(self, singleShot=True, interval=TIMEOUT_MS)
        self._timer.timeout.connect(self._on_timeout)
        self._cancelled = False

    def prepare(self) -> str:
        raise NotImplementedError

    def validate(self) -> list[str]:
        raise NotImplementedError

    def complete(self) -> str:
        return str(self.folder)

    def start(self) -> None:
        exe = find_claude()
        if exe is None:
            self.finished.emit(False, "找不到 claude 指令，請確認 Claude Code 已安裝並在 PATH 中。")
            return
        self.exe = exe
        try:
            prompt = self.prepare()
        except Exception as e:  # 準備資料失敗（例如 OpenGL 畫對照圖出錯）時直接回報
            self.finished.emit(False, f"準備資料時發生錯誤：{e}")
            return
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

        errors = self.validate()
        if not errors:
            try:
                self.finished.emit(True, self.complete())
            except Exception as e:
                self.finished.emit(False, f"套用結果時發生錯誤：{e}")
        elif self.attempt < MAX_ATTEMPTS and session_id:
            fix = FIX_PROMPT.format(files=self.files, errors="\n".join(f"- {e}" for e in errors))
            self._run(fix, resume=session_id)
        else:
            detail = "\n".join(errors[:10])
            self.finished.emit(False, f"產生的檔案驗證失敗：\n{detail}\n\n{result_text}".strip())


class Generator(ClaudeJob):
    """把使用者的圖片畫成 mascot.svg + rig.json。"""

    label = "Claude 繪製新造型中"
    files = "`mascot.svg` and/or `rig.json`"

    def __init__(self, image: Path, actions: str, parent: QObject | None = None):
        super().__init__(parent)
        self.image = image
        self.actions = actions.strip()

    def prepare(self) -> str:
        self.folder = new_folder(self.image, self.actions)
        source = next(self.folder.glob("source.*")).name
        return PROMPT.format(image=source, spec=spec_text(), actions=self.actions or "(none)")

    def validate(self) -> list[str]:
        return rig.validate(self.folder)


class Live2DMotionJob(ClaudeJob):
    """替 Live2D 模型產生 aipet.json，通過驗證後放進模型資料夾（原本的檔案會備份）。"""

    label = "Claude 產生動作中"
    files = "`aipet.json`"

    def __init__(self, model_json: Path, request: str, renderer, parent: QObject | None = None):
        super().__init__(parent)
        self.model_json = model_json
        self.request = request.strip()
        self.renderer = renderer  # 只在 prepare() 用來畫對照圖

    def prepare(self) -> str:
        from . import live2d_model, live2d_motion

        base = st.home() / "cache" / "live2d-jobs"
        self.folder = base / time.strftime("%Y%m%d-%H%M%S")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.param_info = dict(self.renderer.param_info)
        info = self.renderer.info()
        (self.folder / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        live2d_model.render_sheet(self.renderer, self.folder / "sheet.png")
        current = live2d_motion.motion_path(self.model_json)
        note = ""
        if current.is_file():
            shutil.copy2(current, self.folder / "current.json")
            note = "- `current.json`: the model's current animation file. Start from it and change what the user asks for.\n"
        return LIVE2D_PROMPT.format(current_note=note, spec=live2d_motion.spec_text(), request=self.request or "(none)")

    def validate(self) -> list[str]:
        from . import live2d_motion

        path = self.folder / live2d_motion.MOTION_FILE
        if not path.is_file():
            return [f"沒有產生 {live2d_motion.MOTION_FILE}"]
        return live2d_motion.validate(path, self.param_info)

    def complete(self) -> str:
        from . import live2d_motion

        data = json.loads((self.folder / live2d_motion.MOTION_FILE).read_text(encoding="utf-8"))
        data["request"] = self.request  # 下次修改動作時當作起點
        target = live2d_motion.motion_path(self.model_json)
        if target.is_file():
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(self.model_json)
