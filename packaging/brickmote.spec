# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for BrickMote.
#   python packaging/make_icon.py
#   pyinstaller --noconfirm packaging/brickmote.spec
# Output: dist/BrickMote.exe (Windows), dist/BrickMote.app (macOS), dist/BrickMote (Linux)
# With BRICKMOTE_ONEDIR=1: a folder dist/BrickMote/ (used by the Windows installer
# and the Linux package: faster start-up than a single self-extracting file).
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
PKG = ROOT / "packaging"

if sys.platform == "win32":
    icon = PKG / "brickmote.ico"
elif sys.platform == "darwin":
    icon = PKG / "brickmote.icns"
else:
    icon = PKG / "brickmote.png"
icon = str(icon) if icon.exists() else None

# pynput picks its backend at runtime: list them explicitly (importing pynput
# at build time fails on a headless build machine).
if sys.platform == "win32":
    backend = ["keyboard._win32", "mouse._win32", "_util.win32", "_util.win32_vks"]
elif sys.platform == "darwin":
    backend = ["keyboard._darwin", "mouse._darwin", "_util.darwin", "_util.darwin_vks"]
else:
    backend = ["keyboard._xorg", "mouse._xorg", "_util.xorg", "_util.xorg_keysyms", "keyboard._uinput", "_util.uinput"]
hidden = [f"pynput.{m}" for m in backend] + ["pynput.keyboard._base", "pynput.mouse._base", "pynput._info"]
hidden += collect_submodules("spake2")

a = Analysis(
    [str(PKG / "run_brickmote.py")],
    pathex=[str(ROOT)],
    hiddenimports=hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BrickMote", console=False, icon=icon)
    coll = COLLECT(exe, a.binaries, a.datas, name="BrickMote")
    app = BUNDLE(
        coll,
        name="BrickMote.app",
        icon=icon,
        bundle_identifier="app.brickmote.desktop",
        info_plist={
            "NSHighResolutionCapable": True,
            "NSScreenCaptureUsageDescription": "BrickMote partage cet écran avec le poste que vous autorisez.",
            "NSAppleEventsUsageDescription": "BrickMote contrôle la souris et le clavier lorsque vous l'autorisez.",
        },
    )
elif os.environ.get("BRICKMOTE_ONEDIR") == "1":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BrickMote", console=False, icon=icon, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="BrickMote", upx=False)
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name="BrickMote",
        console=False,
        icon=icon,
        upx=False,
    )
