from __future__ import annotations

import argparse
import json
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
    l2d = sub.add_parser("live2d", help="Live2D 模型的動作檔（aipet.json）工具").add_subparsers(dest="l2d_cmd", required=True)
    l2d.add_parser("spec", help="印出 aipet.json 格式規格")
    l2d.add_parser("list", help="列出 Live2DModels 裡的模型")
    for name, text in (
        ("info", "印出模型參數（JSON）"),
        ("sheet", "把每個參數的最小 / 最大值畫成對照圖"),
        ("preview", "把各狀態的動作畫成預覽圖"),
        ("validate", "檢查模型資料夾裡的 aipet.json"),
        ("use", "讓吉祥物改用這個 Live2D 模型"),
    ):
        p = l2d.add_parser(name, help=text)
        p.add_argument("model", help="model3.json、模型資料夾，或 Live2DModels 底下的資料夾名稱")
        if name in ("sheet", "preview"):
            p.add_argument("--out", type=Path, help="輸出 PNG（預設放在模型資料夾外的暫存位置）")
        if name == "info":
            p.add_argument("--out", type=Path, help="寫入檔案而不是印出")
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
    elif args.cmd == "live2d":
        _live2d_command(args)
    elif args.cmd == "status":
        view = st.aggregate(st.load_sessions())
        print(f"{view.state}  {view.project}  {view.detail}  ({view.count} sessions)")
        for s in view.sessions:
            print(f"  - {s.project or s.id}: {s.state} {s.detail}")


def _qt_app():
    # 要用到 Qt 繪圖與 OpenGL，但不開視窗。offscreen 平台在 Windows / macOS 讀不到系統字型，
    # 只在沒有顯示器的 Linux 使用。
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


def _live2d_command(args: argparse.Namespace) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from . import live2d_model as l2d
    from . import live2d_motion as mo

    if args.l2d_cmd == "spec":
        print(mo.spec_text())
        return
    if args.l2d_cmd == "list":
        for info in l2d.find_models():
            print(f"{info.name}\t{info.model_json}")
        return
    try:
        model_json = l2d.resolve_model(args.model)
    except l2d.Live2DError as e:
        sys.exit(str(e))
    if args.l2d_cmd == "use":
        try:
            st.update_config(live2d=str(model_json), mascot=None, image=None)
        except st.ConfigError as e:
            sys.exit(f"無法寫入設定：{e}")
        print(f"已切換到 Live2D 模型：{model_json}（執行中的吉祥物會自動套用）")
        return

    _app = _qt_app()
    try:
        r = l2d.Live2DRenderer(model_json)
    except l2d.Live2DError as e:
        sys.exit(str(e))
    try:
        if args.l2d_cmd == "info":
            text = json.dumps(r.info(), ensure_ascii=False, indent=2)
            if args.out:
                args.out.write_text(text, encoding="utf-8")
                print(args.out.resolve())
            else:
                print(text)
        elif args.l2d_cmd == "validate":
            path = mo.motion_path(model_json)
            if not path.is_file():
                sys.exit(f"沒有動作檔：{path}（會使用內建動作）")
            errors = mo.validate(path, r.param_info)
            if errors:
                print("驗證失敗：")
                for e in errors:
                    print(f"- {e}")
                sys.exit(1)
            print("OK")
        else:  # sheet / preview
            if args.l2d_cmd == "preview" and r.motion_error:
                print(f"警告：aipet.json 有錯，預覽的是內建動作：\n{r.motion_error}")
            # 預設輸出到 ~/.ai-pet/cache，不在使用者的模型資料夾裡多放檔案
            out = args.out or st.home() / "cache" / "live2d-out" / f"{model_json.parent.name}-{args.l2d_cmd}.png"
            out = out.resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            (l2d.render_sheet if args.l2d_cmd == "sheet" else l2d.render_preview)(r, out)
            print(out)
    finally:
        r.close()


def _folder_command(args: argparse.Namespace) -> None:
    from . import rig

    _app = _qt_app()
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
            st.update_config(mascot=str(folder), image=None, live2d=None)
        except st.ConfigError as e:
            sys.exit(f"無法寫入設定：{e}")
        print(f"已切換造型：{folder}（執行中的吉祥物會自動套用）")


if __name__ == "__main__":
    main()
