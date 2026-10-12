"""Live2D 模型支援（Cubism 3 以上，含 VTube Studio 模型資料夾）。

使用者把模型資料夾放進 ~/.ai-pet/Live2DModels/<名稱>/，裡面要有 *.model3.json。
live2d-py 是可選相依（pip install "ai-pet[live2d]"）；沒安裝或載入失敗時 available() 為 False，
App 照常運作，只是沒有 Live2D 選項。

為了省資源：
- 只在使用者選了 Live2D 模型時才建立 OpenGL 與載入函式庫。
- 貼圖縮到 MAX_TEXTURE 後快取在 ~/.ai-pet/cache/live2d/，畫面上只有約 150 px，2048 的貼圖用不到。
- 在螢幕外的 framebuffer 渲染成 QImage，再由一般的 QPainter 視窗貼上，泡泡、拖曳等功能不用改。
- 待命、睡覺時降低更新頻率。

VTube Studio 模型通常沒有動作檔，是靠臉部追蹤驅動參數；這裡改由各狀態的姿勢設定標準參數
（頭部角度、身體角度、眼睛、眉毛、嘴巴），再由模型自己的物理效果帶動頭髮、耳朵等。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage

from . import live2d_motion
from . import state as st

MAX_TEXTURE = 1024
MEASURE_SIZE = 256  # 量測模型實際範圍時用的渲染大小
MAX_FBO = 1024
FIT_MARGIN = 0.12  # 量出的範圍往外留的空間，讓舉手、轉頭不會被切掉
# 動作慢的狀態降低更新頻率（秒/幀）；其他狀態跟著視窗每秒約 30 幀
FRAME_INTERVAL = {"idle": 1 / 15, "sleeping": 1 / 8, "offline": 1 / 8}

_live2d = None
_import_error: str | None = None


class Live2DError(Exception):
    pass


@contextlib.contextmanager
def _keep_std_fds():
    """保護 stdout / stderr 的檔案描述元。

    PyInstaller 打包的無主控台版本裡，live2d 的 C 擴充會把 fd 1 / 2 換成無效的目標，之後的 print
    全部失敗（OSError: Bad file descriptor）。先複製一份，結束後一律還原回去。
    """
    saved = {}
    for fd in (1, 2):
        try:
            saved[fd] = os.dup(fd)
        except OSError:
            pass
    try:
        yield
    finally:
        for fd, copy in saved.items():
            try:
                os.dup2(copy, fd)
            except OSError:
                pass
            os.close(copy)


def available() -> bool:
    """live2d-py 是否可用（第一次呼叫時才 import）。"""
    global _live2d, _import_error
    if _live2d is None and _import_error is None:
        try:
            with _keep_std_fds(), contextlib.redirect_stdout(io.StringIO()):  # import 時會印一段橫幅
                import live2d
            live2d.enableLog(False)
            _live2d = live2d
        except Exception as e:  # ImportError，或平台不支援時的 OSError
            _import_error = f"{type(e).__name__}: {e}"
    return _live2d is not None


def import_error() -> str | None:
    available()
    return _import_error


def models_dir() -> Path:
    return st.home() / "Live2DModels"


# ---------- 找模型 ----------


@dataclass
class ModelInfo:
    model_json: Path
    name: str
    icon: Path | None


def _model_info(model_json: Path) -> ModelInfo:
    name, icon = model_json.parent.name, None
    for vt in model_json.parent.glob("*.vtube.json"):  # VTube Studio 的設定檔
        try:
            data = json.loads(vt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = str(data.get("Name") or name)
        icon_ref = (data.get("FileReferences") or {}).get("Icon")
        if icon_ref and (model_json.parent / icon_ref).is_file():
            icon = model_json.parent / icon_ref
        break
    return ModelInfo(model_json, name, icon)


def resolve_model(arg: str | Path) -> Path:
    """接受 model3.json 路徑、模型資料夾，或 Live2DModels 底下的資料夾名稱，回傳 model3.json。"""
    p = Path(arg).expanduser()
    if not p.exists() and (models_dir() / str(arg)).exists():
        p = models_dir() / str(arg)
    if p.is_file() and p.name.endswith(".model3.json"):
        return p.resolve()
    if p.is_dir():
        found = sorted(p.glob("*.model3.json")) or sorted(p.glob("*/*.model3.json"))
        if found:
            return found[0].resolve()
    raise Live2DError(f"找不到 Live2D 模型（*.model3.json）：{arg}")


def find_models() -> list[ModelInfo]:
    """Live2DModels 底下每個資料夾（或下一層資料夾）裡的 *.model3.json。"""
    base = models_dir()
    if not base.is_dir():
        return []
    found = []
    for folder in sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name.lower()):
        candidates = sorted(folder.glob("*.model3.json")) or sorted(folder.glob("*/*.model3.json"))
        if candidates:
            found.append(_model_info(candidates[0]))
    return found


# ---------- 貼圖快取 ----------


def _referenced_files(refs: dict) -> list[str]:
    files = [refs[k] for k in ("Moc", "Physics", "Pose", "DisplayInfo", "UserData") if refs.get(k)]
    for exp in refs.get("Expressions") or []:
        if exp.get("File"):
            files.append(exp["File"])
    for motions in (refs.get("Motions") or {}).values():
        for m in motions:
            files += [m[k] for k in ("File", "Sound") if m.get(k)]
    return files


def prepare(model_json: Path) -> Path:
    """把模型用到的檔案複製到快取資料夾，貼圖縮小到 MAX_TEXTURE；回傳快取裡的 model3.json。"""
    try:
        data = json.loads(model_json.read_text(encoding="utf-8"))
        refs = data["FileReferences"]
        textures = list(refs.get("Textures") or [])
        files = _referenced_files(refs)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Live2DError(f"無法讀取 {model_json.name}：{e}") from e
    if not refs.get("Moc"):
        raise Live2DError(f"{model_json.name} 沒有指定 .moc3 檔")

    src = model_json.parent
    h = hashlib.sha1(str(model_json.resolve()).encode())
    for rel in [model_json.name, *textures, *files]:
        try:
            s = (src / rel).stat()
        except OSError as e:
            raise Live2DError(f"模型缺少檔案：{rel}") from e
        h.update(f"{rel}:{s.st_size}:{s.st_mtime_ns}".encode())
    h.update(str(MAX_TEXTURE).encode())
    cache = st.home() / "cache" / "live2d" / h.hexdigest()[:16]
    target = cache / model_json.name
    if (cache / ".ready").exists():
        return target

    shutil.rmtree(cache, ignore_errors=True)
    for rel in files:
        dst = cache / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dst)
    for rel in textures:
        img = QImage(str(src / rel))
        if img.isNull():
            raise Live2DError(f"無法讀取貼圖：{rel}")
        if max(img.width(), img.height()) > MAX_TEXTURE:
            img = img.scaled(MAX_TEXTURE, MAX_TEXTURE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        dst = cache / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not img.save(str(dst), "PNG"):
            raise Live2DError(f"無法寫入快取：{dst}")
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (cache / ".ready").write_text("", encoding="utf-8")
    return target


# ---------- 各狀態的姿勢 ----------

# 語意名稱 -> 可能的參數 id（Cubism 3 標準名稱優先，其次是常見的 VTube Studio / Cubism 2 名稱）
PARAM_ALIASES = {
    "angle_x": ("ParamAngleX", "PARAM_ANGLE_X"),
    "angle_y": ("ParamAngleY", "PARAM_ANGLE_Y"),
    "angle_z": ("ParamAngleZ", "PARAM_ANGLE_Z"),
    "body_x": ("ParamBodyAngleX", "PARAM_BODY_ANGLE_X"),
    "body_y": ("ParamBodyAngleY", "PARAM_BODY_ANGLE_Y"),
    "body_z": ("ParamBodyAngleZ", "PARAM_BODY_ANGLE_Z"),
    "eye_l": ("ParamEyeLOpen", "PARAM_EYE_L_OPEN"),
    "eye_r": ("ParamEyeROpen", "PARAM_EYE_R_OPEN"),
    "smile_l": ("ParamEyeLSmile", "PARAM_EYE_L_SMILE"),
    "smile_r": ("ParamEyeRSmile", "PARAM_EYE_R_SMILE"),
    "ball_x": ("ParamEyeBallX", "PARAM_EYE_BALL_X"),
    "ball_y": ("ParamEyeBallY", "PARAM_EYE_BALL_Y"),
    "brow_l": ("ParamBrowLY", "PARAM_BROW_L_Y"),
    "brow_r": ("ParamBrowRY", "PARAM_BROW_R_Y"),
    "mouth_open": ("ParamMouthOpenY", "PARAM_MOUTH_OPEN_Y"),
    "mouth_form": ("ParamMouthForm", "PARAM_MOUTH_FORM"),
    "hand_r_on": ("ParamRightFound", "HandRightFound", "ParamHandRightFound"),
    "hand_r_x": ("HandRightPositionX", "ParamHandRightPositionX"),
    "hand_r_y": ("HandRightPositionY", "ParamHandRightPositionY"),
    "hand_l_on": ("HandLeftFound", "ParamLeftFound", "ParamHandLeftFound"),
    "hand_l_x": ("HandLeftPositionX", "ParamHandLeftPositionX"),
    "hand_l_y": ("HandLeftPositionY", "ParamHandLeftPositionY"),
}
EYES = ("eye_l", "eye_r")


def _blink(t: float, period: float = 4.2) -> float:
    """1 = 睜眼；每個 period 開頭約 0.15 秒閉一次。"""
    u = t % period
    return 1.0 if u > 0.15 else abs(u - 0.075) / 0.075


def pose(state: str, t: float, ts: float) -> dict[str, float]:
    """各狀態要設定的參數（語意名稱 -> 值）。角度單位是度，其餘是 Cubism 慣用的 -1..1 / 0..1。
    沒列出的參數會慢慢回到模型預設值。"""
    s = math.sin
    open_ = _blink(t)
    if state == "idle":
        return {"angle_x": 8 * s(0.6 * t), "angle_y": 3 * s(0.4 * t), "angle_z": 4 * s(0.5 * t + 1),
                "body_x": 3 * s(0.6 * t), "ball_x": 0.3 * s(0.6 * t), "eye_l": open_, "eye_r": open_}
    if state == "thinking":
        return {"angle_x": 12 * s(0.9 * t), "angle_y": 15, "angle_z": -6, "ball_x": 0.5 * s(0.9 * t),
                "ball_y": 0.8, "brow_l": 0.5, "brow_r": 0.5, "eye_l": 0.9 * open_, "eye_r": 0.9 * open_}
    if state == "working":
        return {"angle_x": 5 * s(3 * t), "angle_y": -8, "body_x": 5 * s(6 * t), "body_z": 3 * s(6 * t),
                "ball_y": -0.6, "eye_l": 0.8 * open_, "eye_r": 0.8 * open_,
                "hand_l_on": 1, "hand_r_on": 1,  # 雙手輪流敲鍵盤
                "hand_l_y": -2 + 3 * s(12 * t), "hand_r_y": -2 + 3 * s(12 * t + math.pi)}
    if state == "waiting":
        return {"angle_x": 3 * s(10 * t), "angle_y": 10, "eye_l": 1, "eye_r": 1, "brow_l": 1, "brow_r": 1,
                "mouth_open": 0.6, "hand_r_on": 1, "hand_r_y": 10, "hand_r_x": 4 * s(8 * t)}  # 舉手揮動
    if state == "done":
        k = max(0.0, 1 - ts / 2.5)  # 開心晃幾下後停住
        return {"angle_z": 10 * s(5 * ts) * k, "body_z": 6 * s(5 * ts) * k, "angle_y": 5,
                "smile_l": 1, "smile_r": 1, "eye_l": 0.6, "eye_r": 0.6, "mouth_form": 1,
                "hand_l_on": k, "hand_r_on": k, "hand_l_y": 8 * k, "hand_r_y": 8 * k}
    if state == "compacting":
        return {"angle_y": -12, "body_y": -8, "angle_x": 6 * s(4 * t), "eye_l": 0.3, "eye_r": 0.3}
    # sleeping / offline
    return {"angle_y": -15, "angle_z": 8, "body_z": 4, "eye_l": 0, "eye_r": 0}




# ---------- 渲染 ----------

MOTION_CHECK_INTERVAL = 1.0  # 每秒檢查一次動作檔有沒有被修改


def display_names(model_json: Path) -> dict[str, tuple[str, str]]:
    """從 .cdi3.json 讀出模型作者取的參數名稱：參數 ID -> (名稱, 群組名稱)。"""
    try:
        refs = json.loads(model_json.read_text(encoding="utf-8"))["FileReferences"]
        cdi = json.loads((model_json.parent / refs["DisplayInfo"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    groups = {g.get("Id"): g.get("Name", "") for g in cdi.get("ParameterGroups") or []}
    return {
        p["Id"]: (p.get("Name", ""), groups.get(p.get("GroupId"), ""))
        for p in cdi.get("Parameters") or []
        if p.get("Id")
    }


_gl = None  # 所有渲染器共用的 (OpenGL context, offscreen surface)


def _gl_context():
    """建立（第一次）並回傳共用的 OpenGL context。

    live2d-py 的 shader 是全域的，建在呼叫 glInit 時的 context 裡；另開 context 的模型會畫不出來。
    渲染都在主執行緒依序進行，所有模型共用一個 context、各自用自己的 framebuffer 即可。
    """
    global _gl
    if _gl is not None:
        return _gl
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat

    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        raise Live2DError("無法建立 OpenGL（遠端桌面或部分虛擬機不支援）")
    surface = QOffscreenSurface()
    surface.setFormat(ctx.format())
    surface.create()
    if not ctx.makeCurrent(surface):
        raise Live2DError("無法使用 OpenGL")
    try:
        with _keep_std_fds():
            _live2d.init()
            _live2d.glInit()
    finally:
        ctx.doneCurrent()
    _gl = (ctx, surface)
    return _gl


class Live2DRenderer:
    def __init__(self, model_json: Path):
        if not available():
            raise Live2DError(f"Live2D 函式庫無法載入（{_import_error}）")
        cached = prepare(model_json)
        self.source = model_json
        self.ctx, self.surface = _gl_context()
        if not self.ctx.makeCurrent(self.surface):
            raise Live2DError("無法使用 OpenGL")
        try:
            with _keep_std_fds():
                self.model = _live2d.Model()
                self.model.LoadModelJson(str(cached))
            self.model.SetAutoBlink(False)  # 眨眼自己控制，各狀態的眼睛開合才不會被蓋掉
            self.model.SetAutoBreath(True)
            m = self.model
            ids = m.GetParamIds()
            if not ids:
                raise Live2DError("模型沒有任何參數，可能不是 Cubism 3 以上的模型")
            # 參數 ID -> (最小, 最大, 預設)
            self.param_info = {
                pid: (m.GetParamMinById(pid), m.GetParamMaxById(pid), m.GetParamDefaultById(pid)) for pid in ids
            }
            # 內建動作用的語意名稱 -> 這個模型實際的參數 ID
            self.alias = {
                key: pid
                for key, aliases in PARAM_ALIASES.items()
                if (pid := next((a for a in aliases if a in self.param_info), None))
            }
            self.eye_ids = {self.alias[k] for k in EYES if k in self.alias}
            self.values = {pid: self.param_info[pid][2] for pid in self.alias.values()}
            self.motion: live2d_motion.Motion | None = None
            self.motion_error = ""
            self._motion_mtime: float | None = None
            self._motion_checked = 0.0
            self._check_motion(force=True)
            self.fbo = None
            self.fbo_size = 0
            self._make_fbo(MEASURE_SIZE)
            self.fit = self._measure()  # 模型在畫布中實際佔的範圍（0..1）
        finally:
            self.ctx.doneCurrent()
        self.last_image: QImage | None = None
        self.last_render = 0.0
        self.last_t: float | None = None

    # ----- 動作 -----
    def _check_motion(self, force: bool = False) -> None:
        """動作檔有變動就重新載入（Claude 或使用者改完後不必重開）。格式錯誤時改用內建動作。"""
        now = time.monotonic()
        if not force and now - self._motion_checked < MOTION_CHECK_INTERVAL:
            return
        self._motion_checked = now
        path = live2d_motion.motion_path(self.source)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._motion_mtime and not force:
            return
        self._motion_mtime = mtime
        self.motion, self.motion_error = None, ""
        if mtime is None:
            return
        try:
            self.motion = live2d_motion.load(self.source, self.param_info)
        except live2d_motion.MotionError as e:
            self.motion_error = "\n".join(e.errors)
            print(f"ai-pet：{path} 有錯，改用內建動作：\n{self.motion_error}", file=sys.stderr)
            return
        for a in (a for sm in self.motion.states.values() for a in sm.params):
            self.values.setdefault(a.part, self.param_info[a.part][2])

    def targets(self, state: str, t: float, ts: float) -> dict[str, float]:
        """這一幀各參數的目標值（參數 ID -> 值）；動作檔有定義就用它，否則用內建動作。"""
        state = "sleeping" if state == "offline" else state
        sm = self.motion.states.get(state) if self.motion else None
        if sm is None:
            return {self.alias[k]: v for k, v in pose(state, t, ts).items() if k in self.alias}
        defaults = {pid: info[2] for pid, info in self.param_info.items()}
        out = sm.param_values(t, ts, defaults)
        if sm.blink:
            b = _blink(t)
            for pid in self.eye_ids:
                out[pid] = out.get(pid, self.param_info[pid][1]) * b  # 沒指定時以全開為準
        return out

    def body(self, state: str, t: float, ts: float) -> dict[str, float] | None:
        """動作檔定義的整體變形；None 表示沿用 App 內建的整體動作。"""
        state = "sleeping" if state == "offline" else state
        sm = self.motion.states.get(state) if self.motion else None
        return sm.body_values(t, ts) if sm is not None and sm.body else None

    # ----- 繪製 -----
    def _make_fbo(self, size: int) -> None:
        from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat

        if self.fbo is not None and self.fbo_size == size:
            return
        ff = QOpenGLFramebufferObjectFormat()
        ff.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
        self.fbo = QOpenGLFramebufferObject(size, size, ff)
        self.fbo_size = size
        self.model.Resize(size, size)

    def _draw(self, delta: float = -1.0) -> QImage:
        self.fbo.bind()
        self.ctx.functions().glViewport(0, 0, self.fbo_size, self.fbo_size)
        _live2d.clearBuffer(0, 0, 0, 0)
        self.model.Update(delta)
        self.model.Draw()
        img = self.fbo.toImage()
        self.fbo.release()
        return img

    def _set(self, target: dict[str, float], dt: float | None) -> None:
        """寫入參數。dt 為 None 時直接設成目標值；否則往目標值平滑靠近，眼睛快一點才看得出眨眼。"""
        for pid in set(self.values) | set(target):
            lo, hi, default = self.param_info[pid]
            goal = min(hi, max(lo, target.get(pid, default)))
            cur = self.values.get(pid, default)
            if dt is None:
                cur = goal
            else:
                rate = 30.0 if pid in self.eye_ids else 6.0
                cur += (goal - cur) * (1 - math.exp(-rate * dt))
            self.values[pid] = cur
            self.model.SetSaveParamById(pid, cur)

    def _measure(self) -> QRectF:
        """以待命姿勢畫一次，找出不透明像素的範圍，之後只把這塊貼到視窗上。"""
        self._set(self.targets("idle", 1.0, 1.0), None)
        img = self._draw().scaled(64, 64)
        xs, ys = [], []
        for y in range(64):
            for x in range(64):
                if img.pixelColor(x, y).alpha() > 10:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return QRectF(0, 0, 1, 1)
        r = QRectF(min(xs) / 64, min(ys) / 64, (max(xs) + 1 - min(xs)) / 64, (max(ys) + 1 - min(ys)) / 64)
        # 上、左、右多留空間給舉手和轉頭；下方只留一點，腳底才會貼在地面上
        m = FIT_MARGIN
        left = max(0.0, r.left() - r.width() * m)
        right = min(1.0, r.right() + r.width() * m)
        top = max(0.0, r.top() - r.height() * m)
        bottom = min(1.0, r.bottom() + r.height() * 0.02)
        return QRectF(left, top, right - left, bottom - top)

    def render(self, state: str, t: float, ts: float, height_px: float) -> tuple[QImage, QRectF] | None:
        """畫一幀；回傳 (影像, 影像中要貼到視窗的範圍)。height_px 是視窗上要顯示的實際像素高度。"""
        self._check_motion()
        now = time.monotonic()
        if self.last_image is not None and now - self.last_render < FRAME_INTERVAL.get(state, 0):
            return self.last_image, self.source_rect()
        dt = 1 / 30 if self.last_t is None else max(0.0, min(0.2, t - self.last_t))
        self.last_t = t
        if not self.ctx.makeCurrent(self.surface):
            return None
        try:
            # 解析度剛好對應螢幕上的大小，不浪費 GPU 與讀回的成本
            self._make_fbo(int(min(MAX_FBO, max(64, math.ceil(height_px / self.fit.height())))))
            self._set(self.targets(state, t, ts), dt)
            img = self._draw()
        finally:
            self.ctx.doneCurrent()
        self.last_image, self.last_render = img, now
        return img, self.source_rect()

    def snapshot(self, target: dict[str, float], size: int) -> QImage:
        """直接以指定參數畫一張（不做平滑），先多跑幾次更新讓物理效果穩定。給參數對照圖與預覽圖用。"""
        if not self.ctx.makeCurrent(self.surface):
            raise Live2DError("無法使用 OpenGL")
        try:
            self._make_fbo(size)
            self._set(target, None)
            for _ in range(20):
                self._draw(1 / 30)
            img = self._draw(1 / 30)
        finally:
            self.ctx.doneCurrent()
        self.last_image = None  # 下一幀重新畫
        return img

    def source_rect(self) -> QRectF:
        s = self.fbo_size
        f = self.fit
        return QRectF(f.x() * s, f.y() * s, f.width() * s, f.height() * s)

    def info(self) -> dict:
        """給 Claude 看的模型資訊：所有參數（含作者取的名稱）、動作群組、表情。"""
        names = display_names(self.source)
        params = []
        for pid, (lo, hi, default) in self.param_info.items():
            name, group = names.get(pid, ("", ""))
            params.append({"id": pid, "name": name, "group": group,
                           "min": round(lo, 4), "max": round(hi, 4), "default": round(default, 4)})
        return {
            "model": self.source.parent.name,
            "parameters": params,
            "motion_groups": self.model.GetMotionGroups(),
            "expressions": self.model.GetExpressions(),
        }

    def close(self) -> None:
        if self.ctx is None:
            return
        # context 是共用的，不銷毀；只在它是 current 時釋放這個模型的貼圖與 framebuffer
        if self.ctx.makeCurrent(self.surface):
            self.model = None
            self.fbo = None
            self.ctx.doneCurrent()
        self.ctx = None
        self.last_image = None


# ---------- 給 Claude 看的圖 ----------


def _crop(r: Live2DRenderer, img: QImage) -> QImage:
    return img.copy(r.source_rect().toRect())


def render_portrait(r: Live2DRenderer, size: int) -> QImage:
    """待命姿勢的模型圖（裁掉空白），給對話框預覽用。"""
    img = _crop(r, r.snapshot(r.targets("idle", 1.0, 1.0), int(size / r.fit.height())))
    return img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _is_toggle(pid: str, name: str) -> bool:
    return "found" in pid.lower() or "found" in name.lower()


def render_sheet(r: Live2DRenderer, out: Path, cell: int = 130) -> None:
    """參數對照圖：每個參數在最小值、最大值時的樣子（其他參數維持預設）。"""
    from PySide6.QtGui import QColor, QFont, QPainter

    size = int(cell / r.fit.height())
    defaults = {pid: info[2] for pid, info in r.param_info.items()}
    names = display_names(r.source)
    pids = list(r.param_info)
    cols = 2  # 每列放兩個參數
    label_w = 190
    block_w = label_w + cell * 2
    rows = (len(pids) + cols - 1) // cols
    top = cell + 40
    sheet = QImage(block_w * cols, top + rows * (cell + 8), QImage.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#eef1f5"))
    p = QPainter(sheet)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    f = QFont()
    f.setPixelSize(12)
    p.setFont(f)
    p.setPen(QColor("#222"))
    p.drawText(QRectF(8, 0, label_w - 8, cell), Qt.AlignVCenter, "所有參數為預設值")
    p.drawImage(QRectF(label_w, 4, cell, cell), _crop(r, r.snapshot(defaults, size)).scaled(cell, cell, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    for i, pid in enumerate(pids):
        x = (i % cols) * block_w
        y = top + (i // cols) * (cell + 8)
        if (i // cols) % 2 == 0:
            p.fillRect(QRectF(x, y, block_w, cell + 8), QColor("#e2e7ee"))
        lo, hi, _ = r.param_info[pid]
        name, group = names.get(pid, ("", ""))
        # VTube Studio 的手部參數要「偵測到手」（…Found = 1）才看得到，同群組的開關一起打開
        toggles = {} if _is_toggle(pid, name) else {
            q: r.param_info[q][1] for q in pids
            if q != pid and _is_toggle(q, names.get(q, ("", ""))[0]) and group and names.get(q, ("", ""))[1] == group
        }
        label = f"{pid}\n{name}" + (f"（{group}）" if group else "") + f"\n左：{lo:g}　右：{hi:g}"
        if toggles:
            label += "\n（同時打開 " + "、".join(toggles) + "）"
        p.setPen(QColor("#222"))
        p.drawText(QRectF(x + 8, y, label_w - 12, cell), Qt.AlignVCenter | Qt.TextWordWrap, label)
        for j, v in enumerate((lo, hi)):
            img = _crop(r, r.snapshot({**defaults, **toggles, pid: v}, size))
            p.drawImage(QRectF(x + label_w + j * cell, y + 4, cell, cell), img.scaled(cell, cell, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    p.end()
    if not sheet.save(str(out)):
        raise OSError(f"無法寫入 {out}")


PREVIEW_TIMES = (0.0, 0.3, 0.6, 1.0, 1.5, 2.5)


def render_preview(r: Live2DRenderer, out: Path, cell: int = 150) -> None:
    """各狀態在幾個時間點的樣子（含動作檔的整體變形），檢查動作是否符合預期。"""
    from PySide6.QtGui import QColor, QFont, QPainter

    size = int(cell / r.fit.height())
    label_w = 110
    pad = 30
    img = QImage(label_w + cell * len(PREVIEW_TIMES), 24 + (cell + pad) * len(st.STATES), QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor("#e9edf2"))
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    f = QFont()
    f.setPixelSize(13)
    p.setFont(f)
    p.setPen(QColor("#333"))
    for j, t in enumerate(PREVIEW_TIMES):
        p.drawText(QRectF(label_w + j * cell, 0, cell, 24), Qt.AlignCenter, f"t = {t:g}s")
    for i, state in enumerate(st.STATES):
        top = 24 + i * (cell + pad)
        if i % 2:
            p.fillRect(QRectF(0, top, img.width(), cell + pad), QColor("#dfe4ea"))
        defined = r.motion is not None and state in r.motion.states
        p.setPen(QColor("#333"))
        p.drawText(QRectF(8, top, label_w - 8, cell + pad), Qt.AlignVCenter, state if defined else f"{state}\n(內建動作)")
        for j, t in enumerate(PREVIEW_TIMES):
            frame = _crop(r, r.snapshot(r.targets(state, t, t), size))
            k = min(cell / frame.width(), cell / frame.height())
            w, h = frame.width() * k, frame.height() * k
            foot_x, foot_y = label_w + j * cell + cell / 2, top + pad + cell
            p.save()
            body = r.body(state, t, t)
            p.translate(foot_x, foot_y)
            if body:
                s = cell / 140  # 動作檔的位移以視窗上約 140 px 高的模型為準
                p.translate(body["x"] * s, body["y"] * s)
                p.rotate(body["rotate"])
                p.scale(body["scale_x"], body["scale_y"])
                p.setOpacity(max(0.0, min(1.0, body["opacity"])))
            p.drawImage(QRectF(-w / 2, -h, w, h), frame)
            p.restore()
    p.end()
    if not img.save(str(out)):
        raise OSError(f"無法寫入 {out}")
