from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from .generate import find_claude

PLACEHOLDER = """\
例如：
工作中：拿著槌子敲敲打打
需要確認：舉起雙手揮舞
完成：原地轉一圈
睡覺：閉上眼睛、頭上冒泡泡

沒寫到的狀態會用預設動作，全部留空也可以。
可用的狀態：待命、思考中、工作中、需要確認、完成、整理記憶、睡覺"""


DRAW_NOTE = "Claude Code 會參考這張圖重畫成向量吉祥物並加上動畫，約需 1–5 分鐘，會使用你的 Claude 訂閱額度。"
LIVE2D_NOTE = (
    "Claude Code 會先看過模型每個參數的效果，再寫出各狀態的動作檔（模型資料夾裡的 aipet.json，"
    "原本的檔案會備份成 aipet.json.bak），約需 1–3 分鐘，會使用你的 Claude 訂閱額度。"
)


class MascotDialog(QDialog):
    """輸入各狀態想要的動作，再選擇要讓 Claude 產生，或另一個選項（直接用圖片 / 還原內建動作）。

    choice 結果："claude" 或 "secondary"。
    """

    def __init__(
        self,
        image: Path | QPixmap,
        actions: str = "",
        parent: QWidget | None = None,
        secondary: str | None = "直接使用圖片",
        primary: str = "用 Claude 畫成會動的吉祥物",
        note: str = DRAW_NOTE,
        title: str = "設定吉祥物",
    ):
        super().__init__(parent, Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(title)
        self.choice: str | None = None

        preview = QLabel()
        pix = image if isinstance(image, QPixmap) else QPixmap(str(image))
        if not pix.isNull():
            preview.setPixmap(pix.scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        preview.setAlignment(Qt.AlignCenter)

        self.text = QPlainTextEdit(actions)
        self.text.setPlaceholderText(PLACEHOLDER)
        self.text.setMinimumSize(420, 200)

        has_claude = find_claude() is not None
        note = QLabel(note if has_claude else "找不到 claude 指令，無法使用 Claude。請先安裝 Claude Code。")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        draw = QPushButton(primary)
        draw.setDefault(True)
        draw.setEnabled(has_claude)
        draw.clicked.connect(lambda: self._done("claude"))
        plain = QPushButton(secondary or "")
        plain.clicked.connect(lambda: self._done("secondary"))
        plain.setVisible(secondary is not None)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(plain)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(draw)

        layout = QVBoxLayout(self)
        layout.addWidget(preview)
        layout.addWidget(QLabel("想要吉祥物在不同狀態做什麼動作？"))
        layout.addWidget(self.text)
        layout.addWidget(note)
        layout.addLayout(buttons)

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def actions(self) -> str:
        return self.text.toPlainText().strip()
