"""Render the BrickMote logo to packaging/brickmote.{png,ico,icns}.

Run before PyInstaller:  python packaging/make_icon.py
"""

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from brickmote.gui.style import draw_logo  # noqa: E402


def main() -> None:
    app = QGuiApplication([])  # noqa: F841 - required for QPixmap
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    draw_logo(1024).save(buf, "PNG")
    img = Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")
    out = ROOT / "packaging"
    img.resize((512, 512), Image.LANCZOS).save(out / "brickmote.png")
    img.save(out / "brickmote.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(out / "brickmote.icns")
    print("icons written to", out)


if __name__ == "__main__":
    main()
