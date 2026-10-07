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


class MascotDialog(QDialog):
    """選完圖片後：輸入各狀態的動作，選擇要讓 Claude 畫，還是直接用圖片。"""

    def __init__(self, image: Path, actions: str = "", parent: QWidget | None = None, allow_plain: bool = True):
        super().__init__(parent, Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("設定吉祥物")
        self.choice: str | None = None  # "claude" / "image"

        preview = QLabel()
        pix = QPixmap(str(image))
        if not pix.isNull():
            preview.setPixmap(pix.scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        preview.setAlignment(Qt.AlignCenter)

        self.text = QPlainTextEdit(actions)
        self.text.setPlaceholderText(PLACEHOLDER)
        self.text.setMinimumSize(420, 200)

        has_claude = find_claude() is not None
        note = QLabel(
            "Claude Code 會參考這張圖重畫成向量吉祥物並加上動畫，約需 1–5 分鐘，會使用你的 Claude 訂閱額度。"
            if has_claude
            else "找不到 claude 指令，無法用 Claude 繪製。請先安裝 Claude Code。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        draw = QPushButton("用 Claude 畫成會動的吉祥物")
        draw.setDefault(True)
        draw.setEnabled(has_claude)
        draw.clicked.connect(lambda: self._done("claude"))
        plain = QPushButton("直接使用圖片")
        plain.clicked.connect(lambda: self._done("image"))
        plain.setVisible(allow_plain)
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
