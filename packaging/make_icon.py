"""把預設吉祥物畫成 1024×1024 的 PNG，當作 App 圖示（PyInstaller 會再轉成 .ico / .icns）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def make_icon(out: Path) -> Path:
    # 用空的設定資料夾，確保畫出來的是預設吉祥物，不是使用者自己換的造型
    os.environ["AI_PET_HOME"] = tempfile.mkdtemp(prefix="ai-pet-icon-")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from ai_pet.app import BODY, GROUND, Manager, Pet

    app = QApplication.instance() or QApplication([])
    pet = Pet(Manager(), 0)  # 不呼叫 Manager.start()，不會輪詢 session 或跑動畫

    size = 1024
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    # 吉祥物在視窗座標中大約佔 x 30–190、y 60–220，縮放到整張圖
    p.scale(size / 160, size / 160)
    p.translate(-30, -60)
    pet._draw_body(p, "idle", 1.0, BODY, 110, GROUND, 1.0, 1.0, 0.0)
    p.end()
    pet.deleteLater()
    del app

    out.parent.mkdir(parents=True, exist_ok=True)
    if not img.save(str(out)):
        raise OSError(f"無法寫入 {out}")
    return out


if __name__ == "__main__":
    print(make_icon(Path(sys.argv[1] if len(sys.argv) > 1 else "build/icon.png")))
