"""Claude 產生的吉祥物：mascot.svg（分部件的向量圖）+ rig.json（各狀態的動畫）。

App 只讀資料、不執行產生出來的程式碼。

rig.json 格式：
{
  "version": 1,
  "parts": ["body", "arm_left", ...],   # mascot.svg 中最上層 <g id> 的繪製順序（後面的畫在上面）
  "hidden": ["eyes_closed"],            # 預設隱藏的部件
  "states": {
    "idle": {
      "show": [...], "hide": [...],     # 這個狀態額外顯示 / 隱藏的部件
      "anims": [
        {"part": "all" | "<id>", "prop": "x|y|rotate|scale|scale_x|scale_y|opacity",
         "wave": "const|sin|bounce|blink|shake|spin", "amp": 0, "base": 0,
         "period": 1.0, "phase": 0.0, "origin": [x, y], "once": 2.0}
      ]
    }
  }
}
值 = base + amp * wave((時間 / period) + phase)；once 表示從進入狀態起只播放幾秒，之後停在 base。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from . import state as st

SVG_NAME = "mascot.svg"
RIG_NAME = "rig.json"

PROPS = {"x": 0.0, "y": 0.0, "rotate": 0.0, "scale": 1.0, "scale_x": 1.0, "scale_y": 1.0, "opacity": 1.0}


def _frac(u: float) -> float:
    return u - math.floor(u)


WAVES = {
    "const": lambda u: 0.0,
    "sin": lambda u: math.sin(2 * math.pi * u),
    "bounce": lambda u: abs(math.sin(math.pi * u)),  # 0→1→0，每個 period 一次
    "blink": lambda u: 1.0 if _frac(u) < 0.05 else 0.0,  # 每個 period 開頭短暫觸發
    "shake": lambda u: math.sin(2 * math.pi * 8 * u) if _frac(u) < 0.25 else 0.0,  # 一陣一陣抖
    "spin": _frac,  # 0→1 線性，配 rotate amp=360 就是轉一圈
}


class RigError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class Anim:
    part: str
    prop: str
    wave: str
    amp: float
    base: float
    period: float
    phase: float
    origin: QPointF | None
    once: float | None

    def value(self, t: float, ts: float) -> float:
        if self.once:
            if ts >= self.once:
                return self.base
            t = ts
        return self.base + self.amp * WAVES[self.wave](t / self.period + self.phase)


@dataclass
class StateSpec:
    show: set[str] = field(default_factory=set)
    hide: set[str] = field(default_factory=set)
    anims: list[Anim] = field(default_factory=list)


def _num(v, name: str, errors: list[str], default: float | None = None) -> float:
    if v is None and default is not None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        errors.append(f"{name} 必須是數字（目前是 {v!r}）")
        return default if default is not None else 0.0
    return float(v)


class Rig:
    def __init__(self, folder: Path):
        errors: list[str] = []
        svg = folder / SVG_NAME
        rig_file = folder / RIG_NAME
        if not svg.is_file():
            errors.append(f"缺少 {SVG_NAME}")
        if not rig_file.is_file():
            errors.append(f"缺少 {RIG_NAME}")
        if errors:
            raise RigError(errors)

        self.renderer = QSvgRenderer(str(svg))
        if not self.renderer.isValid():
            raise RigError([f"{SVG_NAME} 無法被 QtSvg 解析（只支援 SVG Tiny 1.2，避免 CSS <style>、filter、mask、<text>）"])
        try:
            data = json.loads(rig_file.read_text(encoding="utf-8"))
        except ValueError as e:
            raise RigError([f"{RIG_NAME} 不是合法 JSON：{e}"]) from e
        if not isinstance(data, dict):
            raise RigError([f"{RIG_NAME} 最外層必須是物件"])

        parts = data.get("parts")
        if not isinstance(parts, list) or not parts or not all(isinstance(x, str) for x in parts):
            raise RigError(["parts 必須是非空的字串陣列"])
        if len(set(parts)) != len(parts):
            errors.append("parts 有重複的 id")
        for pid in parts:
            if not self.renderer.elementExists(pid):
                errors.append(f"parts 裡的 '{pid}' 在 {SVG_NAME} 找不到對應的 id")
        self.parts: list[str] = parts
        known = set(parts)

        def ids(v, where: str) -> set[str]:
            if v is None:
                return set()
            if not isinstance(v, list):
                errors.append(f"{where} 必須是陣列")
                return set()
            bad = [x for x in v if x not in known]
            if bad:
                errors.append(f"{where} 有不在 parts 裡的 id：{bad}")
            return {x for x in v if x in known}

        self.hidden = ids(data.get("hidden"), "hidden")
        self.states: dict[str, StateSpec] = {}
        states = data.get("states")
        if not isinstance(states, dict):
            errors.append("states 必須是物件")
            states = {}
        for name, spec in states.items():
            where = f"states.{name}"
            if name not in st.STATES:
                errors.append(f"{where}：未知狀態，只能是 {list(st.STATES)}")
                continue
            if not isinstance(spec, dict):
                errors.append(f"{where} 必須是物件")
                continue
            anims = []
            raw_anims = spec.get("anims", [])
            if not isinstance(raw_anims, list):
                errors.append(f"{where}.anims 必須是陣列")
                raw_anims = []
            for i, a in enumerate(raw_anims):
                aw = f"{where}.anims[{i}]"
                if not isinstance(a, dict):
                    errors.append(f"{aw} 必須是物件")
                    continue
                part = a.get("part", "all")
                if part != "all" and part not in known:
                    errors.append(f"{aw}.part '{part}' 不在 parts 裡")
                    continue
                prop = a.get("prop")
                if prop not in PROPS:
                    errors.append(f"{aw}.prop 必須是 {list(PROPS)} 之一")
                    continue
                wave = a.get("wave", "sin")
                if wave not in WAVES:
                    errors.append(f"{aw}.wave 必須是 {list(WAVES)} 之一")
                    continue
                origin = a.get("origin")
                if origin is not None:
                    if isinstance(origin, list) and len(origin) == 2:
                        origin = QPointF(_num(origin[0], f"{aw}.origin[0]", errors), _num(origin[1], f"{aw}.origin[1]", errors))
                    else:
                        errors.append(f"{aw}.origin 必須是 [x, y]")
                        origin = None
                once = a.get("once")
                anims.append(
                    Anim(
                        part=part,
                        prop=prop,
                        wave=wave,
                        amp=_num(a.get("amp"), f"{aw}.amp", errors, 0.0),
                        base=_num(a.get("base"), f"{aw}.base", errors, PROPS[prop]),
                        period=max(0.05, _num(a.get("period"), f"{aw}.period", errors, 1.0)),
                        phase=_num(a.get("phase"), f"{aw}.phase", errors, 0.0),
                        origin=origin,
                        once=max(0.05, _num(once, f"{aw}.once", errors)) if once is not None else None,
                    )
                )
            self.states[name] = StateSpec(
                show=ids(spec.get("show"), f"{where}.show"),
                hide=ids(spec.get("hide"), f"{where}.hide"),
                anims=anims,
            )
        if errors:
            raise RigError(errors)

        self.viewbox = self.renderer.viewBoxF()
        if self.viewbox.isEmpty():
            raise RigError([f"{SVG_NAME} 需要設定 viewBox"])
        self.bounds = {
            pid: self.renderer.transformForElement(pid).mapRect(self.renderer.boundsOnElement(pid)) for pid in parts
        }
        self._cache: dict[str, QImage] = {}
        self._cache_scale = 0.0

    def _sprites(self, scale: float) -> dict[str, QImage]:
        """把每個部件預先渲染成點陣圖（含反鋸齒）。

        直接在視窗上呼叫 QSvgRenderer.render 會失去反鋸齒，邊緣出現鋸齒；
        先畫到 QImage 再貼上就沒有這個問題，也省下每格重新解析向量的成本。
        """
        if abs(scale - self._cache_scale) < 1e-6:
            return self._cache
        self._cache = {}
        for pid, b in self.bounds.items():
            pad = 2.0
            img = QImage(
                max(1, math.ceil((b.width() + pad * 2) * scale)),
                max(1, math.ceil((b.height() + pad * 2) * scale)),
                QImage.Format_ARGB32_Premultiplied,
            )
            img.fill(Qt.transparent)
            ip = QPainter(img)
            ip.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
            ip.scale(scale, scale)
            ip.translate(pad, pad)
            self.renderer.render(ip, pid, QRectF(0, 0, b.width(), b.height()))
            ip.end()
            self._cache[pid] = img
        self._cache_scale = scale
        return self._cache

    def has_state(self, state: str) -> bool:
        return state in self.states

    def target_rect(self, box: QRectF) -> QRectF:
        """viewBox 等比例縮進 box、底部置中後實際佔的範圍。"""
        vb = self.viewbox
        k = min(box.width() / vb.width(), box.height() / vb.height())
        w, h = vb.width() * k, vb.height() * k
        return QRectF(box.center().x() - w / 2, box.bottom() - h, w, h)

    def paint(self, p: QPainter, state: str, t: float, ts: float, box: QRectF) -> None:
        spec = self.states.get(state) or StateSpec()
        vb = self.viewbox
        rect = self.target_rect(box)
        k = rect.width() / vb.width()
        # 快取解析度 = 螢幕上的大小 × 裝置像素比 × 2，放大縮小的動畫也保持清楚
        sprites = self._sprites(k * p.device().devicePixelRatioF() * 2)
        p.save()
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        p.translate(rect.topLeft())
        p.scale(k, k)
        p.translate(-vb.left(), -vb.top())
        self._apply(p, [a for a in spec.anims if a.part == "all"], t, ts, QPointF(vb.center().x(), vb.bottom()))
        visible = (set(self.parts) - self.hidden - spec.hide) | spec.show
        for pid in self.parts:
            if pid not in visible:
                continue
            p.save()
            bounds = self.bounds[pid]
            self._apply(p, [a for a in spec.anims if a.part == pid], t, ts, bounds.center())
            p.drawImage(bounds.adjusted(-2, -2, 2, 2), sprites[pid])
            p.restore()
        p.restore()

    @staticmethod
    def _apply(p: QPainter, anims: list[Anim], t: float, ts: float, default_origin: QPointF) -> None:
        # 每個動畫各自繞自己的支點套用；位移先做，讓旋轉、縮放跟著部件移動
        order = {"x": 0, "y": 0, "rotate": 1, "scale": 1, "scale_x": 1, "scale_y": 1, "opacity": 2}
        for a in sorted(anims, key=lambda a: order[a.prop]):
            v = a.value(t, ts)
            o = a.origin if a.origin is not None else default_origin
            if a.prop == "x":
                p.translate(v, 0)
            elif a.prop == "y":
                p.translate(0, v)
            elif a.prop == "opacity":
                p.setOpacity(p.opacity() * max(0.0, min(1.0, v)))
            else:
                p.translate(o)
                if a.prop == "rotate":
                    p.rotate(v)
                elif a.prop == "scale":
                    p.scale(v, v)
                elif a.prop == "scale_x":
                    p.scale(v, 1)
                else:
                    p.scale(1, v)
                p.translate(-o)


def validate(folder: Path) -> list[str]:
    """嚴格檢查：格式錯誤之外，七個狀態都必須定義（App 本身容許缺漏，會套預設動作）。"""
    try:
        r = Rig(folder)
    except RigError as e:
        return e.errors
    missing = [s for s in st.STATES if s not in r.states]
    return [f"states 缺少：{missing}"] if missing else []


PREVIEW_TIMES = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5)


def render_preview(folder: Path, out: Path, cell: int = 150) -> None:
    """把每個狀態在幾個時間點的樣子畫成一張表格圖，方便檢查畫面與動畫。"""
    from PySide6.QtGui import QColor, QFont

    r = Rig(folder)
    label_w = 110
    pad = 30  # 上方留空間給跳躍、旋轉超出的部分
    img = QImage(label_w + cell * len(PREVIEW_TIMES), 24 + (cell + pad) * len(st.STATES), QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor("#e9edf2"))
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
    font = QFont()
    font.setPixelSize(13)
    p.setFont(font)
    p.setPen(QColor("#333"))
    for j, t in enumerate(PREVIEW_TIMES):
        p.drawText(QRectF(label_w + j * cell, 0, cell, 24), Qt.AlignCenter, f"t = {t:g}s")
    for i, state in enumerate(st.STATES):
        top = 24 + i * (cell + pad)
        if i % 2:
            p.fillRect(QRectF(0, top, img.width(), cell + pad), QColor("#dfe4ea"))
        p.setPen(QColor("#333"))
        name = state if state in r.states else f"{state}\n(未定義)"
        p.drawText(QRectF(8, top, label_w - 8, cell + pad), Qt.AlignVCenter | Qt.AlignLeft, name)
        for j, t in enumerate(PREVIEW_TIMES):
            box = QRectF(label_w + j * cell + 8, top + pad, cell - 16, cell - 8)
            r.paint(p, state, t, t, box)
    p.end()
    if not img.save(str(out)):
        raise OSError(f"無法寫入 {out}")
