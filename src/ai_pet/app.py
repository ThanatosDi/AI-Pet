"""置頂的桌面吉祥物視窗（PySide6，Windows / macOS / Linux）。"""

from __future__ import annotations

import math
import signal
import sys
import time

from pathlib import Path

from PySide6.QtCore import QLockFile, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QImageReader,
    QMovie,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMenu,
    QMessageBox,
    QProxyStyle,
    QStyle,
    QWidget,
)

from . import install
from . import state as st
from .dialogs import MascotDialog
from .generate import Generator
from .rig import Rig, RigError

W, H = 220, 200
GROUND = 182  # 腳底的 y 座標
IMG_W, IMG_H = 150, 124  # 自訂圖片的最大顯示範圍（泡泡下方到地面）
BODY = QColor("#D97757")
INK = QColor("#2B2118")
ALERT = QColor("#E5484D")

LABELS = {
    "thinking": "思考中",
    "working": "工作中",
    "waiting": "需要你確認！",
    "done": "完成了 ✓",
    "compacting": "整理記憶中",
}

STATE_NAMES = {
    "idle": "待命",
    "thinking": "思考中",
    "working": "工作中",
    "waiting": "等待確認",
    "done": "完成",
    "compacting": "壓縮中",
    "sleeping": "睡覺",
    "offline": "沒有 session",
}


class _BigIconStyle(QProxyStyle):
    """讓選單裡的吉祥物縮圖顯示成 32px（預設只有 16px）。"""

    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PM_SmallIconSize:
            return 32
        return super().pixelMetric(metric, option, widget)


_BIG_ICONS: _BigIconStyle | None = None


def _pen(color: QColor, width: float) -> QPen:
    return QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)


class Pet(QWidget):
    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setFixedSize(W, H)
        self.setWindowTitle("AI Pet")

        self.t0 = time.monotonic()
        self.state_t0 = self.t0
        self.view = st.View("offline", "", "", 0, [])
        self._drag: QPoint | None = None
        self._thumbs: dict[tuple[str, float], QIcon] = {}
        self.image: QPixmap | None = None
        self.movie: QMovie | None = None
        self.rig: Rig | None = None
        self.generator: Generator | None = None
        self._look: tuple = (None, None)  # 目前套用的 (mascot, image) 設定
        self._cfg_mtime = 0.0
        self._apply_look(self._load_config())

        self._save_timer = QTimer(self, singleShot=True, interval=500)
        self._save_timer.timeout.connect(self._save_pos)
        self._restore_pos()

        self._poll = QTimer(self, interval=300)
        self._poll.timeout.connect(self.refresh)
        self._poll.start()
        self._anim = QTimer(self, interval=33)
        self._anim.timeout.connect(self.update)
        self._anim.start()
        self.refresh()

    # ---------- 狀態 ----------
    def refresh(self) -> None:
        self._watch_config()
        view = st.aggregate(st.load_sessions())
        if view.state != self.view.state:
            self.state_t0 = time.monotonic()
        self.view = view
        if view.sessions:
            lines = [
                f"{s.project or s.id[:8]}：{STATE_NAMES.get(s.state, s.state)}"
                + (f"  {s.detail[:60]}" if s.detail else "")
                for s in sorted(view.sessions, key=lambda s: -s.ts)
            ]
            self.setToolTip("\n".join(lines))
        else:
            self.setToolTip("沒有 Claude Code session")

    # ---------- 設定檔 ----------
    def _load_config(self) -> dict:
        """讀設定給畫面顯示用；讀不到時回傳空設定（不會拿來寫回）。"""
        try:
            return st.load_config()
        except st.ConfigError:
            return {}

    def _update_config(self, **values) -> None:
        try:
            cfg = st.update_config(**values)
        except (st.ConfigError, OSError) as e:
            print(f"ai-pet：無法寫入設定：{e}", file=sys.stderr)
            return
        # 自己寫的變更已經套用了，不要讓 _watch_config 再載入一次
        try:
            self._cfg_mtime = st.config_path().stat().st_mtime
        except OSError:
            pass
        self._look = (cfg.get("mascot"), cfg.get("image"))

    def _apply_look(self, cfg: dict) -> None:
        """套用設定裡記錄的造型；找不到時暫時顯示預設吉祥物，但保留紀錄。"""
        look = (cfg.get("mascot"), cfg.get("image"))
        if look == self._look:
            return
        self._look = look
        if look[0]:
            if self._set_rig(look[0]):
                return
            print(f"ai-pet：找不到或無法載入造型 {look[0]}，改用預設吉祥物", file=sys.stderr)
        elif look[1]:
            if self._set_image(look[1]):
                return
            print(f"ai-pet：找不到或無法載入圖片 {look[1]}，改用預設吉祥物", file=sys.stderr)
        self._clear_image()

    def _watch_config(self) -> None:
        """設定檔被外部修改（例如 ai-pet use）時套用新造型。"""
        try:
            mtime = st.config_path().stat().st_mtime
            cfg = st.load_config()
        except (OSError, st.ConfigError):
            return  # 下一輪再試，不要因為暫時讀不到就換回預設
        if mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            self._apply_look(cfg)

    # ---------- 自訂圖片 ----------
    def _set_image(self, path: str) -> bool:
        """載入圖片；GIF 等動畫格式用 QMovie 播放。失敗時保留原本的外觀。"""
        if not Path(path).is_file():
            return False
        reader = QImageReader(path)
        if reader.supportsAnimation() and reader.imageCount() != 1:
            movie = QMovie(path)
            if not movie.isValid():
                return False
            movie.jumpToFrame(0)
            size = movie.currentPixmap().size()
            if size.isEmpty():
                return False
            movie.setScaledSize(size.scaled(QSize(IMG_W, IMG_H) * 2, Qt.KeepAspectRatio))
            movie.start()
            self._clear_image()
            self.movie = movie
            return True
        pix = QPixmap(path)
        if pix.isNull():
            return False
        self._clear_image()
        # 預先縮成顯示大小的兩倍，高 DPI 螢幕也清楚，每格繪製不必縮放原圖
        self.image = pix.scaled(QSize(IMG_W, IMG_H) * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return True

    def _clear_image(self) -> None:
        if self.movie is not None:
            self.movie.stop()
            self.movie.deleteLater()
        self.movie = None
        self.image = None
        self.rig = None

    def _set_rig(self, folder: str) -> bool:
        try:
            rig = Rig(Path(folder))
        except RigError:
            return False
        self._clear_image()
        self.rig = rig
        return True

    def _choose_image(self) -> None:
        formats = sorted({bytes(f).decode() for f in QImageReader.supportedImageFormats()})
        patterns = " ".join(f"*.{f}" for f in formats)
        current = self._load_config().get("image") or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇吉祥物圖片", str(Path(current).parent if Path(current).is_file() else current),
            f"圖片 ({patterns});;所有檔案 (*)",
        )
        if not path:
            return
        if QPixmap(path).isNull():
            QMessageBox.warning(self, "AI Pet", f"無法讀取這張圖片：\n{path}")
            return
        dlg = MascotDialog(Path(path), parent=self)
        if not dlg.exec():
            return
        if dlg.choice == "claude":
            self._generate(Path(path), dlg.actions())
        elif self._set_image(path):
            self._update_config(image=str(Path(path).resolve()), mascot=None)

    def _redraw(self) -> None:
        """用同一張原圖重新產生，可以修改動作描述。"""
        folder = Path(self._load_config().get("mascot", ""))
        sources = sorted(folder.glob("source.*")) if folder.is_dir() else []
        if not sources:
            QMessageBox.warning(self, "AI Pet", "找不到原始圖片，請重新選擇圖片。")
            return
        actions_file = folder / "actions.txt"
        actions = actions_file.read_text(encoding="utf-8") if actions_file.is_file() else ""
        dlg = MascotDialog(sources[0], actions, parent=self, allow_plain=False)
        if dlg.exec() and dlg.choice == "claude":
            self._generate(sources[0], dlg.actions())

    def _generate(self, image: Path, actions: str) -> None:
        if self.generator is not None:
            return
        gen = Generator(image, actions, self)
        gen.finished.connect(self._on_generated)
        self.generator = gen
        gen.start()

    def _cancel_generate(self) -> None:
        if self.generator is not None:
            self.generator.cancel()
            self.generator.deleteLater()
            self.generator = None

    def _on_generated(self, ok: bool, result: str) -> None:
        if self.generator is not None:
            self.generator.deleteLater()
        self.generator = None
        if ok and self._set_rig(result):
            self._update_config(mascot=result, image=None)
            self.state_t0 = time.monotonic()
        else:
            box = QMessageBox(QMessageBox.Warning, "AI Pet", "Claude 繪製吉祥物失敗。", parent=self)
            box.setInformativeText(result if not ok else "產生的檔案無法載入。")
            box.setWindowFlag(Qt.WindowStaysOnTopHint)
            box.exec()

    def _mascot_folders(self) -> list[tuple[Path, Rig]]:
        """~/.ai-pet/mascots/ 裡可以正常載入的造型，新的在前。"""
        base = st.home() / "mascots"
        if not base.is_dir():
            return []
        found = []
        for folder in sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
            try:
                found.append((folder, Rig(folder)))
            except RigError:
                continue  # 繪製到一半、失敗或被改壞的資料夾不列出
        return found

    def _thumbnail(self, folder: Path, rig: Rig) -> QIcon:
        try:
            key = (str(folder), max(f.stat().st_mtime for f in folder.iterdir()))
        except (OSError, ValueError):
            key = (str(folder), 0.0)
        icon = self._thumbs.get(key)
        if icon is None:
            size = 48
            pix = QPixmap(size * 2, size * 2)  # 兩倍解析度，高 DPI 選單也清楚
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            rig.paint(p, "idle", 0.0, 0.0, QRectF(4, 4, size * 2 - 8, size * 2 - 8))
            p.end()
            icon = self._thumbs[key] = QIcon(pix)
        return icon

    def _add_switch_menu(self, menu: QMenu) -> None:
        menu.setToolTipsVisible(True)
        global _BIG_ICONS
        if _BIG_ICONS is None:  # 要在 QApplication 建立之後才能產生
            _BIG_ICONS = _BigIconStyle()
        menu.setStyle(_BIG_ICONS)
        current = self._load_config().get("mascot")
        current = Path(current).resolve() if current and self.rig is not None else None

        default = menu.addAction("預設吉祥物", self._reset_image)
        default.setCheckable(True)
        default.setChecked(self.rig is None and self.image is None and self.movie is None)
        if self.image is not None or self.movie is not None:
            img = menu.addAction(f"圖片：{Path(self._load_config().get('image', '')).name}")
            img.setCheckable(True)
            img.setChecked(True)
            img.setEnabled(False)

        entries = self._mascot_folders()
        if entries:
            menu.addSeparator()
        for folder, rig in entries:
            name = folder.name
            try:  # 20261006-221914 → 2026-10-06 22:19
                label = time.strftime("%Y-%m-%d %H:%M", time.strptime(name[:15], "%Y%m%d-%H%M%S"))
            except ValueError:
                label = name
            actions_file = folder / "actions.txt"
            actions = actions_file.read_text(encoding="utf-8").strip() if actions_file.is_file() else ""
            if actions:
                lines = actions.splitlines()
                more = len(lines[0]) > 20 or len(lines) > 1
                label += "　" + lines[0][:20] + ("…" if more else "")
            act = menu.addAction(self._thumbnail(folder, rig), label)
            act.setToolTip(f"{folder}\n\n{actions or '（預設動作）'}")
            act.setCheckable(True)
            act.setChecked(current is not None and folder.resolve() == current)
            act.triggered.connect(lambda _=False, f=folder: self._switch_mascot(f))
        if not entries:
            hint = menu.addAction("（還沒有 Claude 畫過的吉祥物）")
            hint.setEnabled(False)
        menu.addSeparator()
        menu.addAction("開啟吉祥物資料夾", self._open_mascots_dir)

    def _switch_mascot(self, folder: Path) -> None:
        if self._set_rig(str(folder)):
            self._update_config(mascot=str(folder), image=None)
            self.state_t0 = time.monotonic()
        else:
            QMessageBox.warning(self, "AI Pet", f"無法載入這個吉祥物：\n{folder}")

    def _open_mascots_dir(self) -> None:
        base = st.home() / "mascots"
        base.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(base)))

    # ---------- 連接 Claude Code（hooks） ----------
    def _message(self, icon, text: str, info: str = "", buttons=QMessageBox.Ok) -> int:
        box = QMessageBox(icon, "AI Pet", text, buttons, self)
        box.setInformativeText(info)
        box.setWindowFlag(Qt.WindowStaysOnTopHint)
        return box.exec()

    def _toggle_hooks(self) -> None:
        if install.status() in ("current", "other"):
            ok = self._message(
                QMessageBox.Question,
                "要解除和 Claude Code 的連接嗎？",
                "會從 ~/.claude/settings.json 移除 AI Pet 的 hooks（會先備份），吉祥物之後就不會顯示 Claude 的狀態。",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ok == QMessageBox.Yes:
                self._run_install(install.uninstall)
        else:
            self._offer_hooks(force=True)

    def _offer_hooks(self, force: bool = False) -> None:
        """詢問是否把 hooks 寫入 Claude Code 設定。非 force 時只在第一次啟動、還沒連接時問。"""
        hook_status = install.status()
        if hook_status in ("current", "other"):
            return
        if not force and self._load_config().get("hooks_prompted"):
            return
        self._update_config(hooks_prompted=True)
        broken = hook_status == "broken"
        ok = self._message(
            QMessageBox.Question,
            "原本的連接已失效，要重新連接 Claude Code 嗎？" if broken else "要連接 Claude Code 嗎？",
            "AI Pet 需要在 Claude Code 的設定（~/.claude/settings.json）加入 hooks，才能知道 Claude 正在思考、"
            "工作或等你確認。原本的設定會先備份，其他 hooks 不受影響，之後可以從右鍵 →「設定」解除。\n\n"
            "連接後，新開的 Claude Code session 才會生效。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok == QMessageBox.Yes:
            self._run_install(install.install)

    def _run_install(self, action) -> None:
        try:
            result = action()
        except (OSError, ValueError) as e:
            self._message(QMessageBox.Warning, "無法修改 Claude Code 設定。", str(e))
            return
        self._message(QMessageBox.Information, result.splitlines()[0], "\n".join(result.splitlines()[1:]))

    def _reset_image(self) -> None:
        self._clear_image()
        self._update_config(image=None, mascot=None)

    # ---------- 位置 ----------
    def _restore_pos(self) -> None:
        pos = None
        try:
            cfg = self._load_config()
            pos = QPoint(int(cfg["x"]), int(cfg["y"]))
            if QGuiApplication.screenAt(pos + QPoint(W // 2, H // 2)) is None:
                pos = None
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.move(pos or self._default_pos())

    def _default_pos(self) -> QPoint:
        geo = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(geo.right() - W - 20, geo.bottom() - H - 10)

    def _save_pos(self) -> None:
        self._update_config(x=self.x(), y=self.y())

    def moveEvent(self, e) -> None:
        self._save_timer.start()
        super().moveEvent(e)

    # ---------- 滑鼠 ----------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            handle = self.windowHandle()
            # startSystemMove 在 Wayland 也能拖曳；不支援時改用手動拖曳
            if handle is not None and handle.startSystemMove():
                self._drag = None
            else:
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e) -> None:
        self._drag = None

    def contextMenuEvent(self, e) -> None:
        menu = QMenu(self)
        if self.view.sessions:
            for s in sorted(self.view.sessions, key=lambda s: -s.ts):
                act = menu.addAction(f"{s.project or s.id[:8]}：{STATE_NAMES.get(s.state, s.state)}")
                act.setEnabled(False)
        else:
            menu.addAction("沒有 Claude Code session").setEnabled(False)
        menu.addSeparator()
        settings = menu.addMenu("設定")
        if self.generator is not None:
            settings.addAction("取消 Claude 繪製", self._cancel_generate)
        else:
            settings.addAction("選擇吉祥物圖片…", self._choose_image)
            if self.rig is not None:
                settings.addAction("修改動作並重新繪製…", self._redraw)
        self._add_switch_menu(settings.addMenu("切換吉祥物"))
        settings.addSeparator()
        settings.addAction("回到預設位置", lambda: self.move(self._default_pos()))
        settings.addSeparator()
        hook_status = install.status()
        label = {
            "current": "已連接 Claude Code",
            "other": "已連接 Claude Code（使用另一個 ai-pet）",
            "broken": "重新連接 Claude Code（原本的連接已失效）",
            "none": "連接 Claude Code…",
        }[hook_status]
        connect = settings.addAction(label, self._toggle_hooks)
        connect.setCheckable(True)
        connect.setChecked(hook_status in ("current", "other"))
        menu.addAction("清除 session 紀錄", st.clear_sessions)
        menu.addAction("結束", QApplication.quit)
        menu.exec(e.globalPos())

    # ---------- 繪圖 ----------
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = time.monotonic()
        t = now - self.t0
        ts = now - self.state_t0
        s = self.view.state

        dx = dy = 0.0
        sx = sy = 1.0
        color = QColor(BODY)
        if s == "idle":
            sy = 1 + 0.025 * math.sin(t * 2.2)
        elif s == "thinking":
            dx = 4 * math.sin(t * 1.6)
        elif s == "working":
            dy = -abs(math.sin(t * 7)) * 6
        elif s == "waiting":
            dx = 3 * math.sin(t * 40) if (t % 1.6) < 0.35 else 0.0
        elif s == "done":
            dy = -abs(math.sin(ts * 5)) * 14 * max(0.0, 1 - ts / 2.5)
        elif s == "compacting":
            sy = 0.82 + 0.1 * math.sin(t * 4)
            sx = 1 + (1 - sy) * 0.6
        else:  # sleeping / offline
            sy = 1 + 0.015 * math.sin(t * 1.2)
            color = color.darker(140 if s == "offline" else 115)

        cx = W / 2 + dx
        foot = GROUND + dy

        pix = self.movie.currentPixmap() if self.movie is not None else self.image
        if self.rig is not None:
            badge_at = self._draw_rig(p, s, t, ts, dx, dy, sx, sy)
        elif pix is not None and not pix.isNull():
            badge_at = self._draw_image(p, pix, s, cx, foot, sx, sy)
        else:
            self._draw_body(p, s, t, color, cx, foot, sx, sy, dy)
            badge_at = QPointF(cx + 52, foot - 86)

        if s in ("sleeping", "offline"):
            self._zzz(p, t)
        if self.view.count > 1:
            self._badge(p, self.view.count, badge_at)
        self._bubble(p, s, t)
        if self.generator is not None:
            self._generating_label(p, now - self.generator.started_at)

    def _draw_rig(
        self, p: QPainter, s: str, t: float, ts: float, dx: float, dy: float, sx: float, sy: float
    ) -> QPointF:
        rig = self.rig
        box = QRectF(W / 2 - IMG_W / 2, GROUND - IMG_H, IMG_W, IMG_H)
        rect = rig.target_rect(box)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 45))
        p.drawEllipse(QPointF(W / 2, GROUND + 2), rect.width() * 0.35, 4)
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        # offline 沿用 sleeping 的動作；rig 沒定義的狀態套用預設的整體動作
        rig_state = "sleeping" if s == "offline" else s
        if s == "offline":
            p.setOpacity(0.5)
        if not rig.has_state(rig_state):
            p.translate(W / 2 + dx, GROUND + dy)
            p.scale(sx, sy)
            p.translate(-W / 2, -GROUND)
        rig.paint(p, rig_state, t, ts, box)
        p.restore()
        return QPointF(rect.right() - 6, rect.top() + 6)

    def _generating_label(self, p: QPainter, elapsed: float) -> None:
        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)
        text = f"Claude 繪製新造型中 {int(elapsed) // 60}:{int(elapsed) % 60:02d}"
        fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 14
        r = QRectF((W - w) / 2, H - 17, w, 15)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(62, 99, 221, 220))
        p.drawRoundedRect(r, 7, 7)
        p.setPen(QColor("white"))
        p.drawText(r, Qt.AlignCenter, text)

    def _draw_image(
        self, p: QPainter, pix: QPixmap, s: str, cx: float, foot: float, sx: float, sy: float
    ) -> QPointF:
        size = pix.size().scaled(QSize(IMG_W, IMG_H), Qt.KeepAspectRatio)
        w, h = size.width(), size.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 45))
        p.drawEllipse(QPointF(W / 2, GROUND + 2), w * 0.35 * (1 - (GROUND - foot) / 50), 4)
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.translate(cx, foot)
        p.scale(sx, sy)
        if s in ("sleeping", "offline"):
            p.setOpacity(0.5 if s == "offline" else 0.75)
        p.drawPixmap(QRectF(-w / 2, -h, w, h), pix, QRectF(pix.rect()))
        p.restore()
        return QPointF(cx + w / 2 - 6, foot - h * sy + 6)

    def _draw_body(
        self, p: QPainter, s: str, t: float, color: QColor,
        cx: float, foot: float, sx: float, sy: float, dy: float,
    ) -> None:
        # 影子
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 45))
        shadow = 46 * (1 + dy / 50)
        p.drawEllipse(QPointF(W / 2, GROUND + 2), shadow, 5)

        # 腳
        p.setBrush(color.darker(115))
        for i, lx in enumerate((-38, -18, 18, 38)):
            lift = max(0.0, math.sin(t * 14 + i * math.pi)) * 4 if s == "working" else 0.0
            p.drawRoundedRect(QRectF(cx + lx - 5, foot - 16 - lift, 10, 16 - lift * 0.5), 3, 3)

        # 身體（以底部中心為原點縮放，壓扁時腳不會離地）
        p.save()
        p.translate(cx, foot - 10)
        p.scale(sx, sy)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(-55, -78, 110, 78), 22, 22)
        self._arms(p, s, t, color)
        self._face(p, s, t)
        p.restore()

    def _arms(self, p: QPainter, s: str, t: float, color: QColor) -> None:
        p.setBrush(color)
        left_y = right_y = -42.0
        if s == "waiting":
            right_y = -70 + 4 * math.sin(t * 10)  # 舉手
        elif s == "done":
            left_y = right_y = -66.0
        elif s == "working":
            left_y = -42 + 3 * math.sin(t * 14)
            right_y = -42 - 3 * math.sin(t * 14)
        p.drawRoundedRect(QRectF(-69, left_y, 16, 22), 7, 7)
        p.drawRoundedRect(QRectF(53, right_y, 16, 22), 7, 7)

    def _face(self, p: QPainter, s: str, t: float) -> None:
        if s in ("sleeping", "offline"):
            p.setPen(_pen(INK, 3))
            for x in (-22, 22):
                p.drawLine(QPointF(x - 7, -44), QPointF(x + 7, -44))
            return
        if s == "done":
            p.setPen(_pen(INK, 3.5))
            p.setBrush(Qt.NoBrush)
            for x in (-22, 22):
                path = QPainterPath(QPointF(x - 8, -42))
                path.quadTo(QPointF(x, -58), QPointF(x + 8, -42))
                p.drawPath(path)
            mouth = QPainterPath(QPointF(-9, -28))
            mouth.quadTo(QPointF(0, -18), QPointF(9, -28))
            p.drawPath(mouth)
            return

        p.setPen(Qt.NoPen)
        p.setBrush(INK)
        w, h, ox, oy = 10.0, 16.0, 0.0, 0.0
        if s == "thinking":
            ox, oy = 5 * math.sin(t * 0.8), -6
        elif s == "working":
            h, oy = 7, 2
        elif s == "waiting":
            w, h = 13, 20
        elif s == "compacting":
            h = 4
        if s in ("idle", "thinking") and (t % 4.0) < 0.13:
            h = 2  # 眨眼
        for x in (-22, 22):
            p.drawRoundedRect(QRectF(x + ox - w / 2, -48 + oy - h / 2, w, h), 3, 3)
        if s == "waiting":
            p.drawEllipse(QPointF(0, -24), 4, 5)

    def _zzz(self, p: QPainter, t: float) -> None:
        f = QFont()
        f.setBold(True)
        for i in range(3):
            ph = (t * 0.4 + i / 3) % 1
            f.setPixelSize(int(12 + ph * 10))
            p.setFont(f)
            p.setPen(QColor(90, 90, 120, int(230 * (1 - ph))))
            p.drawText(QPointF(W / 2 + 35 + ph * 30, GROUND - 95 - ph * 45), "z")

    def _badge(self, p: QPainter, n: int, center: QPointF) -> None:
        p.setPen(_pen(QColor("white"), 2))
        p.setBrush(QColor("#3E63DD"))
        p.drawEllipse(center, 10, 10)
        f = QFont()
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(center.x() - 10, center.y() - 10, 20, 20), Qt.AlignCenter, str(n))

    def _bubble(self, p: QPainter, s: str, t: float) -> None:
        label = LABELS.get(s)
        if not label:
            return
        shown = label + "." * (int(t * 3) % 4) if s == "thinking" else label
        detail = self.view.detail
        if not detail and self.view.count > 1 and self.view.project:
            detail = self.view.project
        elif detail and self.view.count > 1 and self.view.project:
            detail = f"[{self.view.project}] {detail}"

        f1 = QFont()
        f1.setPixelSize(13)
        f1.setBold(True)
        f2 = QFont()
        f2.setPixelSize(11)
        fm1, fm2 = QFontMetrics(f1), QFontMetrics(f2)
        maxw = W - 12
        detail = fm2.elidedText(detail, Qt.ElideRight, maxw - 16) if detail else ""
        tw = max(fm1.horizontalAdvance(label + "..."), fm2.horizontalAdvance(detail) if detail else 0)
        bw = min(maxw, tw + 24)
        bh = 26 + (16 if detail else 0)
        x, y = (W - bw) / 2, 4.0

        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, bw, bh), 10, 10)
        tail = QPainterPath(QPointF(W / 2 - 7, y + bh - 1))
        tail.lineTo(W / 2, y + bh + 8)
        tail.lineTo(W / 2 + 7, y + bh - 1)
        tail.closeSubpath()
        path = path.united(tail)

        if s == "waiting":
            border = QColor(ALERT)
            border.setAlpha(int(150 + 105 * (0.5 + 0.5 * math.sin(t * 6))))
            p.setPen(_pen(border, 2.5))
        else:
            p.setPen(_pen(QColor(0, 0, 0, 60), 1))
        p.setBrush(QColor(255, 255, 255, 240))
        p.drawPath(path)

        p.setFont(f1)
        p.setPen(ALERT if s == "waiting" else INK)
        # 用不含點點的寬度置中，避免文字左右跳動
        lw = fm1.horizontalAdvance(label)
        p.drawText(QPointF(W / 2 - lw / 2, y + 5 + fm1.ascent()), shown)
        if detail:
            p.setFont(f2)
            p.setPen(QColor(85, 85, 85))
            p.drawText(QRectF(x + 8, y + 23, bw - 16, 16), Qt.AlignCenter, detail)


def run() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    st.home().mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(st.home() / "pet.lock"))
    if not lock.tryLock(100):
        print("ai-pet 已經在執行了。", file=sys.stderr)
        sys.exit(1)
    pet = Pet()
    pet.show()
    QTimer.singleShot(800, pet._offer_hooks)
    # 不論用哪種方式結束（右鍵選單、Ctrl+C、kill），都在關閉前存下位置
    app.aboutToQuit.connect(pet._save_pos)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QApplication.quit())
    # Qt 事件迴圈執行中 Python 收不到 signal，定時讓直譯器醒來處理
    wake = QTimer(interval=200)
    wake.timeout.connect(lambda: None)
    wake.start()
    code = app.exec()
    lock.unlock()
    sys.exit(code)
