"""Application entry point: ``python -m brickmote``."""

from __future__ import annotations

import argparse
import hashlib
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
    parser.add_argument(
        "--minimized", action="store_true", help="démarrer dans la zone de notification (démarrage de Windows)"
    )
    parser.add_argument("--version", action="store_true", help="afficher la version et quitter")
    args = parser.parse_args(argv)
    if args.version:
        from .. import __version__
        from ..defaults import builtin_relay

        has_default = builtin_relay() is not None
        print(f"BrickMote {__version__} - serveur relais par défaut : {'intégré' if has_default else 'aucun'}")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _windows_dpi_awareness()

    from PySide6.QtCore import QLockFile
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ..config import Paths
    from ..core import BrickMoteCore
    from .bridge import CoreThread, QtCoreUI
    from .main_window import MainWindow
    from ..config import Settings
    from .style import app_icon, apply_font, apply_theme

    app = QApplication(sys.argv if argv is None else ["brickmote", *argv])
    app.setApplicationName("BrickMote")
    app.setOrganizationName("BrickMote")
    app.setStyle("Fusion")
    apply_font(app)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    paths = Paths(Path(args.home) if args.home else None)
    paths.base.mkdir(parents=True, exist_ok=True)
    apply_theme(app, Settings.load(paths.settings).theme)
    # One instance per configuration folder. Launching BrickMote again (Start
    # menu, desktop shortcut) brings the running window to the front instead.
    instance = "BrickMote-" + hashlib.sha256(str(paths.base.resolve()).encode()).hexdigest()[:16]
    lock = QLockFile(str(paths.base / "brickmote.lock"))
    if not lock.tryLock(100):
        sock = QLocalSocket()
        sock.connectToServer(instance)
        if sock.waitForConnected(1500):
            sock.write(b"show")
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
        else:
            QMessageBox.information(None, "BrickMote", "BrickMote est déjà en cours d'exécution.")
        return 0

    ui = QtCoreUI()
    core_thread = CoreThread()
    core_thread.start()
    core = BrickMoteCore(paths, ui)
    core_thread.submit(core.start())

    window = MainWindow(core, core_thread, ui)
    if args.minimized and window.tray is not None:
        window.tray.showMessage("BrickMote", f"BrickMote est actif : ID {core.identity.display_id}")
    elif args.minimized:
        window.showMinimized()
    else:
        window.show()

    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)  # only this user's processes
    QLocalServer.removeServer(instance)
    if server.listen(instance):
        def _activate():
            conn = server.nextPendingConnection()
            if conn is not None:
                conn.disconnectFromServer()
            window._show()

        server.newConnection.connect(_activate)

    code = app.exec()
    server.close()

    try:
        core_thread.submit(core.stop()).result(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    core_thread.stop()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
