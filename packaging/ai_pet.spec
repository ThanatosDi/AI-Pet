# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包設定。在專案根目錄執行：
#   uv run pyinstaller packaging/ai_pet.spec --noconfirm
# Windows 產出 dist/AI Pet/AI Pet.exe；macOS 產出 dist/AI Pet.app。
# PyInstaller 不能跨平台打包，Windows 版要在 Windows 上打，macOS 版要在 Mac 上打。

import sys
import tomllib
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "packaging")]

from make_icon import make_icon  # noqa: E402

NAME = "AI Pet"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
icon = str(make_icon(ROOT / "build" / "icon.png"))

# Live2D 是可選功能：打包環境有裝 live2d-py（uv sync --extra live2d）才一起打包
datas = [
    (str(ROOT / "src" / "ai_pet" / "mascot_spec.md"), "ai_pet"),
    (str(ROOT / "src" / "ai_pet" / "live2d_motion_spec.md"), "ai_pet"),
]
hiddenimports = []
try:
    import live2d  # noqa: F401

    from PyInstaller.utils.hooks import collect_data_files

    datas += collect_data_files("live2d", includes=["FrameworkShaders/*"])
    hiddenimports.append("live2d")
    print("AI Pet：包含 Live2D 支援")
except ImportError:
    print("AI Pet：沒有安裝 live2d-py，不包含 Live2D 支援")

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    # 各子指令是在函式內才 import，明確列出比較保險
    hiddenimports=[
        "ai_pet.app",
        "ai_pet.hook",
        "ai_pet.install",
        "ai_pet.generate",
        "ai_pet.dialogs",
        "ai_pet.rig",
        "ai_pet.live2d_model",
        "ai_pet.live2d_motion",
        "PySide6.QtSvg",
        "PySide6.QtOpenGL",
        *hiddenimports,
    ],
    # live2d-py 宣告了 numpy、PyOpenGL 相依，但我們只用到它的 C 擴充，不需要這兩個
    excludes=["tkinter", "unittest", "pydoc", "PIL", "numpy", "OpenGL", "live2d.v2", "live2d.utils"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir：啟動快，hook 每次觸發才不會拖慢 Claude Code
    name=NAME,
    console=False,
    upx=False,
    icon=icon,
    argv_emulation=False,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=icon,
        bundle_identifier="io.github.ai-pet",
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # 不在 Dock 顯示圖示，只有桌面上的吉祥物
            "NSHighResolutionCapable": True,
        },
    )
