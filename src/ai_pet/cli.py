from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import state as st


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    # hook 走最短路徑：不經過 argparse，也不載入 Qt，讓每次觸發都盡快結束
    if argv[:1] == ["ai-pet-hook"]:
        from .hook import main as hook_main

        hook_main()
    ap = argparse.ArgumentParser(prog="ai-pet", description="Claude Code 狀態吉祥物")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="啟動吉祥物（預設）")
    for name, text in (("install", "把 hooks 寫入 Claude Code 設定"), ("uninstall", "移除 hooks")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--settings", type=Path, help="settings.json 路徑（預設 ~/.claude/settings.json）")
    p = sub.add_parser("set", help="手動設定狀態（測試用）")
    p.add_argument("state", choices=st.STATES)
    p.add_argument("detail", nargs="?", default="")
    p.add_argument("--session", default="manual")
    p.add_argument("--project", default="demo")
    sub.add_parser("clear", help="清除所有 session 狀態")
    sub.add_parser("spec", help="印出吉祥物格式規格（mascot.svg + rig.json）")
    p = sub.add_parser("new", help="建立新的吉祥物資料夾並複製原圖，印出資料夾路徑")
    p.add_argument("image", type=Path)
    p.add_argument("--actions", default="", help="各狀態的動作描述")
    p = sub.add_parser("validate", help="檢查吉祥物資料夾")
    p.add_argument("folder", type=Path)
    p = sub.add_parser("preview", help="把各狀態畫成一張預覽圖")
    p.add_argument("folder", type=Path)
    p.add_argument("--out", type=Path, help="輸出 PNG（預設為資料夾內的 preview.png）")
    p = sub.add_parser("use", help="讓吉祥物改用這個資料夾的造型")
    p.add_argument("folder", type=Path)
    sub.add_parser("status", help="印出目前狀態")
    args = ap.parse_args(argv)

    if args.cmd in (None, "run"):
        from .app import run

        run()
    elif args.cmd == "install":
        from .install import install

        print(install(args.settings))
    elif args.cmd == "uninstall":
        from .install import uninstall

        print(uninstall(args.settings))
    elif args.cmd == "set":
        st.write_session(args.session, args.state, args.detail, args.project)
    elif args.cmd == "clear":
        st.clear_sessions()
    elif args.cmd == "spec":
        from .generate import spec_text

        sys.stdout.reconfigure(encoding="utf-8")
        print(spec_text())
    elif args.cmd == "new":
        from .generate import new_folder

        if not args.image.is_file():
            sys.exit(f"找不到圖片：{args.image}")
        sys.stdout.reconfigure(encoding="utf-8")
        print(new_folder(args.image.resolve(), args.actions))
    elif args.cmd in ("validate", "preview", "use"):
        _folder_command(args)
    elif args.cmd == "status":
        view = st.aggregate(st.load_sessions())
        print(f"{view.state}  {view.project}  {view.detail}  ({view.count} sessions)")
        for s in view.sessions:
            print(f"  - {s.project or s.id}: {s.state} {s.detail}")


def _folder_command(args: argparse.Namespace) -> None:
    # 這些指令要用到 Qt 的 SVG 與繪圖，但不會開視窗。
    # offscreen 平台在 Windows / macOS 上讀不到系統字型，只在沒有顯示器的 Linux 使用。
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    from . import rig

    _app = QGuiApplication.instance() or QGuiApplication([])
    sys.stdout.reconfigure(encoding="utf-8")
    folder = args.folder.resolve()
    errors = rig.validate(folder)
    if errors:
        print("驗證失敗：")
        for e in errors:
            print(f"- {e}")
        sys.exit(1)
    if args.cmd == "validate":
        print("OK")
    elif args.cmd == "preview":
        out = (args.out or folder / "preview.png").resolve()
        rig.render_preview(folder, out)
        print(out)
    else:  # use
        try:
            st.update_config(mascot=str(folder), image=None)
        except st.ConfigError as e:
            sys.exit(f"無法寫入設定：{e}")
        print(f"已切換造型：{folder}（執行中的吉祥物會自動套用）")


if __name__ == "__main__":
    main()
