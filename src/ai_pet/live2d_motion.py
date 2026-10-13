"""Live2D 模型的動作檔（模型資料夾裡的 aipet.json）。

格式（波形語法與 rig.json 相同）：
{
  "version": 1,
  "request": "使用者當初描述的動作（重新產生時當作起點）",
  "states": {
    "waiting": {
      "params": [{"param": "HandRightPositionY", "wave": "const", "base": 10}, ...],
      "body":   [{"prop": "x", "wave": "shake", "amp": 3, "period": 1.5}],
      "blink":  false
    }
  }
}
- params：直接用模型的參數 ID；值 = base + amp * wave(時間 / period + phase)，超出模型範圍會被限制。
  同一個參數有多筆時，各筆相對預設值的變化量相加。沒列出的參數回到模型預設值。
- body：整個模型在視窗上的位移（px）、旋轉（度）、縮放、透明度，以腳底中心為原點。
- blink：是否自動眨眼；省略時，只要這個狀態沒有控制眼睛開合就會眨眼。
- 沒定義的狀態使用 App 內建的動作。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import state as st
from .rig import PROPS, WAVES, Anim, _num

MOTION_FILE = "aipet.json"
EYE_PARAMS = ("ParamEyeLOpen", "ParamEyeROpen", "PARAM_EYE_L_OPEN", "PARAM_EYE_R_OPEN")


class MotionError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class StateMotion:
    params: list[Anim] = field(default_factory=list)
    body: list[Anim] = field(default_factory=list)
    blink: bool = True

    def param_values(self, t: float, ts: float, defaults: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for a in self.params:
            d = defaults.get(a.part, 0.0)
            out[a.part] = out.get(a.part, d) + (a.value(t, ts) - d)
        return out

    def body_values(self, t: float, ts: float) -> dict[str, float]:
        out = dict(PROPS)
        for a in self.body:
            v = a.value(t, ts)
            if a.prop in ("x", "y", "rotate"):
                out[a.prop] += v
            elif a.prop == "scale":
                out["scale_x"] *= v
                out["scale_y"] *= v
            else:
                out[a.prop] *= v
        return out


@dataclass
class Motion:
    request: str
    states: dict[str, StateMotion]


def motion_path(model_json: Path) -> Path:
    return model_json.parent / MOTION_FILE


def _anim(a, where: str, errors: list[str], key: str, allowed, default_base) -> Anim | None:
    """key 是 "param"（模型參數）或 "prop"（整體變形）；default_base(target) 給出沒寫 base 時的值。"""
    if not isinstance(a, dict):
        errors.append(f"{where} 必須是物件")
        return None
    target = a.get(key)
    if not isinstance(target, str) or (allowed is not None and target not in allowed):
        hint = "模型的參數 ID" if key == "param" else f"{sorted(PROPS)} 之一"
        errors.append(f"{where}.{key} 必須是{hint}（目前是 {target!r}）")
        return None
    wave = a.get("wave", "sin")
    if wave not in WAVES:
        errors.append(f"{where}.wave 必須是 {list(WAVES)} 之一")
        return None
    once = a.get("once")
    return Anim(
        part=target if key == "param" else "all",
        prop="param" if key == "param" else target,
        wave=wave,
        amp=_num(a.get("amp"), f"{where}.amp", errors, 0.0),
        base=_num(a.get("base"), f"{where}.base", errors, default_base(target)),
        period=max(0.05, _num(a.get("period"), f"{where}.period", errors, 1.0)),
        phase=_num(a.get("phase"), f"{where}.phase", errors, 0.0),
        origin=None,
        once=max(0.05, _num(once, f"{where}.once", errors)) if once is not None else None,
    )


def parse(data, param_info: dict[str, tuple[float, float, float]] | None = None) -> Motion:
    """param_info：參數 ID -> (最小, 最大, 預設)。有給時會檢查參數 ID 是否存在，並用預設值補上 base。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise MotionError([f"{MOTION_FILE} 最外層必須是物件"])
    raw_states = data.get("states")
    if not isinstance(raw_states, dict):
        raise MotionError(["states 必須是物件"])
    allowed = set(param_info) if param_info is not None else None

    def param_default(pid: str) -> float:
        return param_info[pid][2] if param_info and pid in param_info else 0.0

    states: dict[str, StateMotion] = {}
    for name, spec in raw_states.items():
        where = f"states.{name}"
        if name not in st.STATES:
            errors.append(f"{where}：未知狀態，只能是 {list(st.STATES)}")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{where} 必須是物件")
            continue
        params, body = [], []
        groups = (
            ("params", params, "param", allowed, param_default),
            ("body", body, "prop", set(PROPS), PROPS.get),
        )
        for key, out, target_key, allow, default_base in groups:
            items = spec.get(key, [])
            if not isinstance(items, list):
                errors.append(f"{where}.{key} 必須是陣列")
                continue
            for i, a in enumerate(items):
                anim = _anim(a, f"{where}.{key}[{i}]", errors, target_key, allow, default_base)
                if anim is not None:
                    out.append(anim)
        blink = spec.get("blink")
        if blink is not None and not isinstance(blink, bool):
            errors.append(f"{where}.blink 必須是 true 或 false")
            blink = None
        if blink is None:
            blink = not any(a.part in EYE_PARAMS for a in params)
        states[name] = StateMotion(params, body, blink)
    if errors:
        raise MotionError(errors)
    request = data.get("request", "")
    return Motion(request if isinstance(request, str) else "", states)


def load(model_json: Path, param_info=None) -> Motion | None:
    """讀取模型資料夾裡的動作檔；沒有檔案回傳 None，格式錯誤丟出 MotionError。"""
    path = motion_path(model_json)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MotionError([f"{MOTION_FILE} 不是合法 JSON：{e}"]) from e
    return parse(data, param_info)


def validate(path: Path, param_info, strict: bool = True) -> list[str]:
    """檢查動作檔。strict 時七個狀態都要定義（Claude 產生的檔案要完整）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        motion = parse(data, param_info)
    except OSError as e:
        return [f"無法讀取 {path.name}：{e}"]
    except ValueError as e:
        return [f"{path.name} 不是合法 JSON：{e}"]
    except MotionError as e:
        return e.errors
    if strict:
        missing = [s for s in st.STATES if s not in motion.states]
        if missing:
            return [f"states 缺少：{missing}"]
    return []


def spec_text() -> str:
    return (Path(__file__).parent / "live2d_motion_spec.md").read_text(encoding="utf-8")
