"""置頂的桌面吉祥物視窗（PySide6，Windows / macOS / Linux）。"""

from __future__ import annotations

import math
import signal
import sys
import time
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl
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

W, H = 220, 220  # 腳底下留空間放 session 名稱與繪製進度
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


OFFLINE = ""  # 沒有任何 session 時那隻睡覺吉祥物的 key（session id 不會是空字串）
SINGLE = "*"  # 只顯示一隻時那隻的 key（session id 經過 _safe_id，不會有 *）


class Manager(QObject):
    """每個 Claude Code session 一隻吉祥物；造型、繪製、設定檔由所有吉祥物共用。

    設定 multi 為 false 時只顯示一隻，代表最需要注意的 session（等待確認 > 工作 > 思考 > …）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.t0 = time.monotonic()
        self.pets: dict[str, Pet] = {}
        self.image: QPixmap | None = None
        self.movie: QMovie | None = None
        self.rig: Rig | None = None
        self.generator: Generator | None = None
        self._thumbs: dict[tuple[str, float], QIcon] = {}
        self._look: tuple = (None, None)  # 目前套用的 (mascot, image) 設定
        self._cfg_mtime = 0.0
        cfg = self.load_config()
        self.multi = cfg.get("multi", True) is not False
        self._apply_look(cfg)

        self._poll = QTimer(self, interval=300)
        self._poll.timeout.connect(self.refresh)
        self._anim = QTimer(self, interval=33)
        self._anim.timeout.connect(self._repaint)

    def start(self) -> None:
        self.refresh()
        self._poll.start()
        self._anim.start()

    def _repaint(self) -> None:
        for pet in self.pets.values():
            pet.update()

    # ---------- session ----------
    def refresh(self) -> None:
        self._watch_config()
        sessions = sorted(st.load_sessions(), key=lambda s: s.ts)
        if not sessions:
            wanted: dict[str, st.Session | None] = {OFFLINE: None}
        elif self.multi:
            wanted = {s.id: s for s in sessions}
        else:
            wanted = {SINGLE: max(sessions, key=lambda s: (st.PRIORITY[s.state], s.ts))}
        # 先建立新的再關掉舊的，畫面上不會有一瞬間沒有吉祥物；要關掉的那些讓出位置
        gone = [k for k in self.pets if k not in wanted]
        used = {p.slot for k, p in self.pets.items() if k not in gone}
        # 名稱優先用 session 名稱，還沒有名稱時用專案資料夾名
        bases = {s.id: s.title or s.project or s.id[:8] for s in sessions}
        names = Counter(bases.values())
        labels = {
            s.id: bases[s.id] + (f" · {s.id[:4]}" if names[bases[s.id]] > 1 else "") for s in sessions
        }
        for key, s in wanted.items():
            pet = self.pets.get(key)
            if pet is None:
                slot = 0
                while slot in used:
                    slot += 1
                used.add(slot)
                pet = self.pets[key] = Pet(self, slot)
                pet.show()
            others = [(labels[o.id], o) for o in reversed(sessions)] if key == SINGLE else []
            pet.set_session(s, labels[s.id] if s is not None else "", others)
        for key in gone:
            pet = self.pets.pop(key)
            pet.close()
            pet.deleteLater()

    def set_multi(self, multi: bool) -> None:
        self.multi = multi
        self.update_config(multi=None if multi else False)
        self.refresh()

    def primary(self) -> Pet | None:
        """位置最前面的那隻，負責顯示繪製進度與跳出全域對話框。"""
        return min(self.pets.values(), key=lambda p: p.slot, default=None)

    def restart_states(self) -> None:
        now = time.monotonic()
        for pet in self.pets.values():
            pet.state_t0 = now

    # ---------- 位置 ----------
    # 設定裡的 x, y 是第 0 隻的位置，其他吉祥物依序往左排，排不下就往上一列

    def anchor(self) -> QPoint:
        try:
            cfg = self.load_config()
            pos = QPoint(int(cfg["x"]), int(cfg["y"]))
            if QGuiApplication.screenAt(pos + QPoint(W // 2, H // 2)) is not None:
                return pos
        except (ValueError, KeyError, TypeError):
            pass
        return self.default_pos()

    @staticmethod
    def default_pos() -> QPoint:
        geo = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(geo.right() - W - 20, geo.bottom() - H - 10)

    def slot_pos(self, slot: int) -> QPoint:
        a = self.anchor()
        screen = QGuiApplication.screenAt(a + QPoint(W // 2, H // 2)) or QGuiApplication.primaryScreen()
        per_row = max(1, (a.x() + W - screen.availableGeometry().left()) // W)
        row, col = divmod(slot, per_row)
        return QPoint(a.x() - col * W, a.y() - row * H)

    def reset_positions(self) -> None:
        pos = self.default_pos()
        self.update_config(x=pos.x(), y=pos.y())
        for pet in self.pets.values():
            pet.place(self.slot_pos(pet.slot))

    def save_anchor(self) -> None:
        first = next((p for p in self.pets.values() if p.slot == 0), None)
        if first is not None:
            self.update_config(x=first.x(), y=first.y())

    # ---------- 設定檔 ----------
    def load_config(self) -> dict:
        """讀設定給畫面顯示用；讀不到時回傳空設定（不會拿來寫回）。"""
        try:
            return st.load_config()
        except st.ConfigError:
            return {}

    def update_config(self, **values) -> None:
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
        self.multi = cfg.get("multi", True) is not False

    def _apply_look(self, cfg: dict) -> None:
        """套用設定裡記錄的造型；找不到時暫時顯示預設吉祥物，但保留紀錄。"""
        look = (cfg.get("mascot"), cfg.get("image"))
        if look == self._look:
            return
        self._look = look
        if look[0]:
            if self.set_rig(look[0]):
                return
            print(f"ai-pet：找不到或無法載入造型 {look[0]}，改用預設吉祥物", file=sys.stderr)
        elif look[1]:
            if self.set_image(look[1]):
                return
            print(f"ai-pet：找不到或無法載入圖片 {look[1]}，改用預設吉祥物", file=sys.stderr)
        self.clear_image()

    def _watch_config(self) -> None:
        """設定檔被外部修改（例如 ai-pet use）時套用新造型。"""
        try:
            mtime = st.config_path().stat().st_mtime
            cfg = st.load_config()
        except (OSError, st.ConfigError):
            return  # 下一輪再試，不要因為暫時讀不到就換回預設
        if mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            self.multi = cfg.get("multi", True) is not False
            self._apply_look(cfg)

    # ---------- 造型 ----------
    def set_image(self, path: str) -> bool:
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
            self.clear_image()
            self.movie = movie
            return True
        pix = QPixmap(path)
        if pix.isNull():
            return False
        self.clear_image()
        # 預先縮成顯示大小的兩倍，高 DPI 螢幕也清楚，每格繪製不必縮放原圖
        self.image = pix.scaled(QSize(IMG_W, IMG_H) * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return True

    def clear_image(self) -> None:
        if self.movie is not None:
            self.movie.stop()
            self.movie.deleteLater()
        self.movie = None
        self.image = None
        self.rig = None

    def set_rig(self, folder: str) -> bool:
        try:
            rig = Rig(Path(folder))
        except RigError:
            return False
        self.clear_image()
        self.rig = rig
        return True

    def reset_image(self) -> None:
        self.clear_image()
        self.update_config(image=None, mascot=None)

    def switch_mascot(self, folder: Path, parent: QWidget) -> None:
        if self.set_rig(str(folder)):
            self.update_config(mascot=str(folder), image=None)
            self.restart_states()
        else:
            QMessageBox.warning(parent, "AI Pet", f"無法載入這個吉祥物：\n{folder}")

    def mascot_folders(self) -> list[tuple[Path, Rig]]:
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

    def thumbnail(self, folder: Path, rig: Rig) -> QIcon:
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

    # ---------- Claude 繪製 ----------
    def generate(self, image: Path, actions: str) -> None:
        if self.generator is not None:
            return
        gen = Generator(image, actions, self)
        gen.finished.connect(self._on_generated)
        self.generator = gen
        gen.start()

    def cancel_generate(self) -> None:
        if self.generator is not None:
            self.generator.cancel()
            self.generator.deleteLater()
            self.generator = None

    def _on_generated(self, ok: bool, result: str) -> None:
        if self.generator is not None:
            self.generator.deleteLater()
        self.generator = None
        if ok and self.set_rig(result):
            self.update_config(mascot=result, image=None)
            self.restart_states()
        else:
            box = QMessageBox(QMessageBox.Warning, "AI Pet", "Claude 繪製吉祥物失敗。", parent=self.primary())
            box.setInformativeText(result if not ok else "產生的檔案無法載入。")
            box.setWindowFlag(Qt.WindowStaysOnTopHint)
            box.exec()


class Pet(QWidget):
    """一個 session 的吉祥物視窗（session 為 None 時是沒有 session 時的睡覺吉祥物）。"""

    def __init__(self, mgr: Manager, slot: int) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setFixedSize(W, H)
        self.setWindowTitle("AI Pet")

        self.mgr = mgr
        self.slot = slot
        self.t0 = mgr.t0 - slot * 1.7  # 錯開動畫相位，多隻吉祥物不會同步呼吸、眨眼
        self.state_t0 = time.monotonic()
        self.session: st.Session | None = None
        self.state = "offline"
        self.label = ""
        self.others: list[tuple[str, st.Session]] = []  # 只顯示一隻時：全部 session（新的在前）
        self._drag: QPoint | None = None
        self._placing = False

        self._save_timer = QTimer(self, singleShot=True, interval=500)
        self._save_timer.timeout.connect(self._save_pos)
        self.place(mgr.slot_pos(slot))
        self.setToolTip("沒有 Claude Code session")

    def set_session(
        self, session: st.Session | None, label: str, others: list[tuple[str, st.Session]] | None = None
    ) -> None:
        state = session.state if session is not None else "offline"
        if state != self.state:
            self.state_t0 = time.monotonic()
        self.state = state
        self.session = session
        self.label = label
        self.others = others or []
        if len(self.others) > 1:
            self.setToolTip("\n".join(
                f"{name}：{STATE_NAMES.get(o.state, o.state)}" + (f"  {o.detail[:60]}" if o.detail else "")
                for name, o in self.others
            ))
        elif session is not None:
            tip = f"{label}：{STATE_NAMES.get(state, state)}"
            if session.title and session.project:
                tip += f"\n專案：{session.project}"
            if session.detail:
                tip += f"\n{session.detail[:120]}"
            if session.cwd:
                tip += f"\n{session.cwd}"
            self.setToolTip(tip)
        else:
            self.setToolTip("沒有 Claude Code session")

    # ---------- 自訂圖片 ----------
    def _choose_image(self) -> None:
        formats = sorted({bytes(f).decode() for f in QImageReader.supportedImageFormats()})
        patterns = " ".join(f"*.{f}" for f in formats)
        current = self.mgr.load_config().get("image") or str(Path.home())
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
            self.mgr.generate(Path(path), dlg.actions())
        elif self.mgr.set_image(path):
            self.mgr.update_config(image=str(Path(path).resolve()), mascot=None)

    def _redraw(self) -> None:
        """用同一張原圖重新產生，可以修改動作描述。"""
        folder = Path(self.mgr.load_config().get("mascot", ""))
        sources = sorted(folder.glob("source.*")) if folder.is_dir() else []
        if not sources:
            QMessageBox.warning(self, "AI Pet", "找不到原始圖片，請重新選擇圖片。")
            return
        actions_file = folder / "actions.txt"
        actions = actions_file.read_text(encoding="utf-8") if actions_file.is_file() else ""
        dlg = MascotDialog(sources[0], actions, parent=self, allow_plain=False)
        if dlg.exec() and dlg.choice == "claude":
            self.mgr.generate(sources[0], dlg.actions())

    def _add_switch_menu(self, menu: QMenu) -> None:
        mgr = self.mgr
        menu.setToolTipsVisible(True)
        global _BIG_ICONS
        if _BIG_ICONS is None:  # 要在 QApplication 建立之後才能產生
            _BIG_ICONS = _BigIconStyle()
        menu.setStyle(_BIG_ICONS)
        current = mgr.load_config().get("mascot")
        current = Path(current).resolve() if current and mgr.rig is not None else None

        default = menu.addAction("預設吉祥物", mgr.reset_image)
        default.setCheckable(True)
        default.setChecked(mgr.rig is None and mgr.image is None and mgr.movie is None)
        if mgr.image is not None or mgr.movie is not None:
            img = menu.addAction(f"圖片：{Path(mgr.load_config().get('image', '')).name}")
            img.setCheckable(True)
            img.setChecked(True)
            img.setEnabled(False)

        entries = mgr.mascot_folders()
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
            act = menu.addAction(mgr.thumbnail(folder, rig), label)
            act.setToolTip(f"{folder}\n\n{actions or '（預設動作）'}")
            act.setCheckable(True)
            act.setChecked(current is not None and folder.resolve() == current)
            act.triggered.connect(lambda _=False, f=folder: mgr.switch_mascot(f, self))
        if not entries:
            hint = menu.addAction("（還沒有 Claude 畫過的吉祥物）")
            hint.setEnabled(False)
        menu.addSeparator()
        menu.addAction("開啟吉祥物資料夾", self._open_mascots_dir)

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
        if not force and self.mgr.load_config().get("hooks_prompted"):
            return
        self.mgr.update_config(hooks_prompted=True)
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

    # ---------- 位置 ----------
    def place(self, pos: QPoint) -> None:
        """程式安排的移動，不當成使用者拖曳，不寫入設定。"""
        self._placing = True
        self.move(pos)
        self._placing = False

    def _save_pos(self) -> None:
        if self.slot == 0:
            self.mgr.update_config(x=self.x(), y=self.y())

    def moveEvent(self, e) -> None:
        # 只記住第 0 隻的位置，其他隻依它排列
        if not self._placing and self.slot == 0:
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
        mgr = self.mgr
        menu = QMenu(self)
        s = self.session
        if len(self.others) > 1:
            for name, o in self.others:
                menu.addAction(f"{name}：{STATE_NAMES.get(o.state, o.state)}").setEnabled(False)
        elif s is not None:
            menu.addAction(f"{self.label}：{STATE_NAMES.get(self.state, self.state)}").setEnabled(False)
            if s.cwd:
                menu.addAction(s.cwd).setEnabled(False)
        else:
            menu.addAction("沒有 Claude Code session").setEnabled(False)
        menu.addSeparator()
        settings = menu.addMenu("設定")
        if mgr.generator is not None:
            settings.addAction("取消 Claude 繪製", mgr.cancel_generate)
        else:
            settings.addAction("選擇吉祥物圖片…", self._choose_image)
            if mgr.rig is not None:
                settings.addAction("修改動作並重新繪製…", self._redraw)
        self._add_switch_menu(settings.addMenu("切換吉祥物"))
        settings.addSeparator()
        multi = settings.addAction("每個 session 一隻吉祥物")
        multi.setCheckable(True)
        multi.setChecked(mgr.multi)
        multi.toggled.connect(mgr.set_multi)
        settings.addAction("全部回到預設位置" if mgr.multi else "回到預設位置", mgr.reset_positions)
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
        if s is not None and not self.others:
            # 例如 Claude Code 被強制關閉、沒有觸發 SessionEnd，留下來的吉祥物
            menu.addAction("移除這隻（session 紀錄）", lambda: st.remove_session(s.id))
        menu.addAction("清除全部 session 紀錄", st.clear_sessions)
        menu.addAction("結束", QApplication.quit)
        menu.exec(e.globalPos())

    # ---------- 繪圖 ----------
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = time.monotonic()
        t = now - self.t0
        ts = now - self.state_t0
        s = self.state
        mgr = self.mgr

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

        pix = mgr.movie.currentPixmap() if mgr.movie is not None else mgr.image
        if mgr.rig is not None:
            self._draw_rig(p, s, t, ts, dx, dy, sx, sy)
        elif pix is not None and not pix.isNull():
            self._draw_image(p, pix, s, cx, foot, sx, sy)
        else:
            self._draw_body(p, s, t, color, cx, foot, sx, sy, dy)

        if s in ("sleeping", "offline"):
            self._zzz(p, t)
        self._name_tag(p)
        self._bubble(p, s, t)
        if mgr.generator is not None and mgr.primary() is self:
            self._generating_label(p, now - mgr.generator.started_at)

    def _draw_rig(
        self, p: QPainter, s: str, t: float, ts: float, dx: float, dy: float, sx: float, sy: float
    ) -> None:
        rig = self.mgr.rig
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

    def _name_tag(self, p: QPainter) -> None:
        """腳底下的 session 名稱（專案資料夾名），分辨哪隻是哪個 session。"""
        if not self.label:
            return
        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)
        fm = QFontMetrics(f)
        more = f"  +{len(self.others) - 1}" if len(self.others) > 1 else ""  # 只顯示一隻時，其他 session 數
        text = fm.elidedText(self.label, Qt.ElideMiddle, W - 30 - fm.horizontalAdvance(more)) + more
        w = fm.horizontalAdvance(text) + 14
        r = QRectF((W - w) / 2, GROUND + 6, w, 15)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(43, 33, 24, 180))
        p.drawRoundedRect(r, 7, 7)
        p.setPen(QColor("white"))
        p.drawText(r, Qt.AlignCenter, text)

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
    ) -> None:
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

    def _bubble(self, p: QPainter, s: str, t: float) -> None:
        label = LABELS.get(s)
        if not label:
            return
        shown = label + "." * (int(t * 3) % 4) if s == "thinking" else label
        detail = self.session.detail if self.session is not None else ""

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
    # session 結束時會關掉它的吉祥物視窗，不能因此結束程式
    app.setQuitOnLastWindowClosed(False)
    st.home().mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(st.home() / "pet.lock"))
    if not lock.tryLock(100):
        print("ai-pet 已經在執行了。", file=sys.stderr)
        sys.exit(1)
    mgr = Manager()
    mgr.start()
    QTimer.singleShot(800, lambda: (pet := mgr.primary()) and pet._offer_hooks())
    # 不論用哪種方式結束（右鍵選單、Ctrl+C、kill），都在關閉前存下位置
    app.aboutToQuit.connect(mgr.save_anchor)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QApplication.quit())
    # Qt 事件迴圈執行中 Python 收不到 signal，定時讓直譯器醒來處理
    wake = QTimer(interval=200)
    wake.timeout.connect(lambda: None)
    wake.start()
    code = app.exec()
    lock.unlock()
    sys.exit(code)
