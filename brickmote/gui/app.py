"""Application entry point: ``python -m brickmote``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _windows_dpi_awareness() -> None:
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="brickmote", description="BrickMote - bureau à distance sécurisé")
    parser.add_argument("--home", help="dossier de configuration (par défaut : dossier utilisateur)")
    parser.add_argument("--debug", action="store_true", help="journalisation détaillée")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _windows_dpi_awareness()

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ..config import Paths
    from ..core import BrickMoteCore
    from .bridge import CoreThread, QtCoreUI
    from .main_window import MainWindow
    from .style import app_icon, apply_font, stylesheet

    app = QApplication(sys.argv if argv is None else ["brickmote", *argv])
    app.setApplicationName("BrickMote")
    app.setOrganizationName("BrickMote")
    app.setStyle("Fusion")
    apply_font(app)
    app.setStyleSheet(stylesheet())
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    paths = Paths(Path(args.home) if args.home else None)
    paths.base.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(paths.base / "brickmote.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, "BrickMote", "BrickMote est déjà en cours d'exécution.")
        return 0

    ui = QtCoreUI()
    core_thread = CoreThread()
    core_thread.start()
    core = BrickMoteCore(paths, ui)
    core_thread.submit(core.start())

    window = MainWindow(core, core_thread, ui)
    window.show()
    code = app.exec()

    try:
        core_thread.submit(core.stop()).result(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    core_thread.stop()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
