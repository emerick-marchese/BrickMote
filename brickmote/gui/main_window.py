"""Main window: this computer's ID on the left, connect to a remote on the right."""

from __future__ import annotations

import os
import socket
import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import BrickMoteCore
from ..util import format_id
from .bridge import CoreThread, PendingRequest, QtCoreUI
from .dialogs import ChatDialog, HostSessionPanel, IncomingDialog, SettingsDialog
from .style import OK, WARN, app_icon, draw_logo
from .viewer_window import ViewerWindow


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(objectName="Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(8)
    t = QLabel(title, objectName="CardTitle")
    lay.addWidget(t)
    return frame, lay


def local_ips() -> list[str]:
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class MainWindow(QMainWindow):
    def __init__(self, core: BrickMoteCore, core_thread: CoreThread, ui: QtCoreUI):
        super().__init__()
        self.core = core
        self.core_thread = core_thread
        self.ui = ui
        self._dialogs: dict[int, QWidget] = {}
        self._panels: dict[int, HostSessionPanel] = {}
        self._chats: dict[int, ChatDialog] = {}
        self._viewers: list[ViewerWindow] = []
        self._last_remote_clip: Optional[str] = None
        self._quitting = False
        self.setWindowTitle(f"BrickMote {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(940, 640)
        self._build()
        self._wire()
        self._setup_tray()
        self.refresh_identity()
        self.refresh_recent()
        self._on_relay_status(*ui.last_relay_status)
        self._on_lan_peers(ui.last_lan_peers)
        QTimer.singleShot(1500, self.refresh_identity)

    # ------------------------------------------------------------ layout
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame(objectName="Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(18, 10, 18, 10)
        logo = QLabel()
        logo.setPixmap(draw_logo(34))
        hl.addWidget(logo)
        hl.addWidget(QLabel("BrickMote", objectName="HeaderTitle"))
        hl.addStretch(1)
        self.btn_settings = QPushButton("Paramètres")
        hl.addWidget(self.btn_settings)
        outer.addWidget(header)

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(18, 18, 18, 8)
        bl.setSpacing(18)
        outer.addWidget(body, 1)

        # ---- this computer
        left, ll = _card("Ce poste")
        ll.addWidget(QLabel("Votre adresse BrickMote", objectName="Muted"))
        id_row = QHBoxLayout()
        self.lbl_id = QLabel("", objectName="BigId")
        self.lbl_id.setTextInteractionFlags(Qt.TextSelectableByMouse)
        id_row.addWidget(self.lbl_id, 1)
        copy = QPushButton("Copier")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.core.identity.device_id))
        id_row.addWidget(copy)
        ll.addLayout(id_row)
        self.lbl_name = QLabel("", objectName="Muted")
        ll.addWidget(self.lbl_name)
        self.lbl_fp = QLabel("", objectName="Small")
        self.lbl_fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_fp.setWordWrap(True)
        ll.addWidget(self.lbl_fp)
        ll.addSpacing(6)
        self.lbl_relay = QLabel("", objectName="Small")
        self.lbl_relay.setWordWrap(True)
        ll.addWidget(self.lbl_relay)
        self.lbl_direct = QLabel("", objectName="Small")
        self.lbl_direct.setWordWrap(True)
        self.lbl_direct.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ll.addWidget(self.lbl_direct)
        self.lbl_access = QLabel("", objectName="Small")
        self.lbl_access.setWordWrap(True)
        ll.addWidget(self.lbl_access)
        self.lbl_warning = QLabel("", objectName="Small")
        self.lbl_warning.setWordWrap(True)
        self.lbl_warning.setStyleSheet(f"color: {WARN};")
        ll.addWidget(self.lbl_warning)
        ll.addStretch(1)
        ll.addWidget(QLabel("Sessions actives sur ce poste", objectName="CardTitle"))
        self.list_sessions = QListWidget()
        self.list_sessions.setMaximumHeight(110)
        ll.addWidget(self.list_sessions)
        bl.addWidget(left, 1)

        # ---- remote computer
        right, rl = _card("Poste distant")
        rl.addWidget(QLabel("ID BrickMote ou adresse IP du poste distant", objectName="Muted"))
        row = QHBoxLayout()
        self.target = QLineEdit(objectName="TargetInput", placeholderText="123 456 789  ou  192.168.1.20")
        self.btn_connect = QPushButton("Se connecter", objectName="Primary")
        self.btn_connect.setMinimumHeight(40)
        row.addWidget(self.target, 1)
        row.addWidget(self.btn_connect)
        rl.addLayout(row)
        self.chk_unattended = QCheckBox("Accès sans surveillance (mot de passe du poste distant)")
        rl.addWidget(self.chk_unattended)
        self.password = QLineEdit(placeholderText="Mot de passe", echoMode=QLineEdit.Password)
        self.password.setVisible(False)
        rl.addWidget(self.password)
        rl.addSpacing(6)
        rl.addWidget(QLabel("Postes détectés sur le réseau local", objectName="CardTitle"))
        self.list_lan = QListWidget()
        rl.addWidget(self.list_lan, 1)
        rl.addWidget(QLabel("Postes récents", objectName="CardTitle"))
        self.list_recent = QListWidget()
        rl.addWidget(self.list_recent, 1)
        bl.addWidget(right, 1)

        footer = QLabel(
            "Chiffrement de bout en bout : X25519 · Ed25519 · AES-256-GCM · SPAKE2 — "
            "le serveur relais ne voit jamais le contenu des sessions.",
            objectName="Footer",
        )
        footer.setContentsMargins(18, 0, 18, 10)
        outer.addWidget(footer)

    def _wire(self):
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_connect.clicked.connect(self.connect_clicked)
        self.target.returnPressed.connect(self.connect_clicked)
        self.password.returnPressed.connect(self.connect_clicked)
        self.chk_unattended.toggled.connect(self.password.setVisible)
        self.list_lan.itemDoubleClicked.connect(lambda it: self._connect_to(it.data(Qt.UserRole)))
        self.list_recent.itemDoubleClicked.connect(lambda it: self._connect_to(it.data(Qt.UserRole)))
        s = self.ui.sig
        s.request.connect(self._on_request)
        s.request_closed.connect(self._on_request_closed)
        s.relay_status.connect(self._on_relay_status)
        s.host_session_started.connect(self._on_host_session)
        s.lan_peers.connect(self._on_lan_peers)
        h = self.ui.host_events.sig
        h.closed.connect(self._on_host_closed)
        h.chat.connect(self._on_host_chat)
        h.clipboard.connect(self._on_host_clipboard)
        h.permissions.connect(lambda sess, perms: self._panels.get(id(sess)) and self._panels[id(sess)].set_permissions(perms))
        h.file_progress.connect(self._on_host_file_progress)
        h.file_done.connect(self._on_host_file_done)
        QGuiApplication.clipboard().dataChanged.connect(self._on_local_clipboard)

    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction(QAction("Afficher BrickMote", self, triggered=self._show))
        menu.addSeparator()
        menu.addAction(QAction("Quitter", self, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip(f"BrickMote - {self.core.identity.display_id}")
        self.tray.show()

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ----------------------------------------------------------- status
    def refresh_identity(self):
        s = self.core.settings
        self.lbl_id.setText(self.core.identity.display_id)
        self.lbl_name.setText(f"Nom : {s.device_name}")
        self.lbl_fp.setText(f"Empreinte de la clé : {self.core.identity.fingerprint}")
        port = self.core.direct_port
        if port:
            ips = ", ".join(local_ips()) or "?"
            self.lbl_direct.setText(f"Connexion directe : TCP {port} ({ips})")
        elif s.direct_enabled:
            self.lbl_direct.setText(f"Connexion directe : port TCP {s.direct_port} indisponible")
        else:
            self.lbl_direct.setText("Connexion directe : désactivée")
        modes = []
        if s.interactive_enabled:
            modes.append("sur acceptation")
        if s.unattended_enabled:
            modes.append("sans surveillance" + (" + 2FA" if s.totp_secret else ""))
        self.lbl_access.setText("Accès entrant : " + (", ".join(modes) if modes else "aucun (tout est refusé)"))
        warnings = []
        if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
            warnings.append(
                "Session Wayland détectée : la capture d'écran et le contrôle exigent une session X11 "
                "(choisissez « Xorg » à l'écran de connexion)."
            )
        if sys.platform == "darwin":
            warnings.append(
                "macOS : autorisez BrickMote dans Réglages > Confidentialité > Enregistrement de l'écran et Accessibilité."
            )
        self.lbl_warning.setText("\n".join(warnings))
        self.lbl_warning.setVisible(bool(warnings))

    def _on_relay_status(self, state: str, detail: str):
        text = {
            "online": f"<span style='color:{OK}'>●</span> Relais : en ligne ({detail})",
            "connecting": f"<span style='color:{WARN}'>●</span> Relais : connexion à {detail}…",
            "offline": f"<span style='color:#C8423B'>●</span> Relais : hors ligne — {detail}",
            "disabled": "○ Relais : non configuré (réseau local et IP directe uniquement)",
        }.get(state, state)
        self.lbl_relay.setText(text)

    def _on_lan_peers(self, peers: dict):
        self.list_lan.clear()
        for dev, entry in sorted(peers.items()):
            known = self.core.known_hosts.entries().get(dev, {})
            name = known.get("name", "") if isinstance(known, dict) else ""
            item = QListWidgetItem(f"{format_id(dev)}   {name}   ({entry['ip']})")
            item.setData(Qt.UserRole, dev)
            self.list_lan.addItem(item)

    def refresh_recent(self):
        self.list_recent.clear()
        entries = sorted(
            self.core.known_hosts.entries().items(), key=lambda kv: -int(kv[1].get("last_seen", 0) or 0)
        )
        for dev, entry in entries[:20]:
            item = QListWidgetItem(f"{format_id(dev)}   {entry.get('name', '')}")
            item.setData(Qt.UserRole, dev)
            self.list_recent.addItem(item)

    # -------------------------------------------------------- settings
    def open_settings(self):
        dlg = SettingsDialog(
            self.core.settings, self.core.identity.device_id, self.core.known_hosts, self.core.audit, self
        )
        if dlg.exec():
            new = dlg.result_settings()

            async def apply():
                self.core.settings = new
                self.core.save_settings()
                await self.core.apply_settings()

            fut = self.core_thread.submit(apply())
            fut.add_done_callback(lambda f: QTimer.singleShot(0, self.refresh_identity))
            QTimer.singleShot(800, self.refresh_identity)
        self.refresh_recent()

    # ----------------------------------------------------- viewer side
    def connect_clicked(self):
        self._connect_to(self.target.text())

    def _connect_to(self, target: str):
        target = (target or "").strip()
        if not target:
            self.target.setFocus()
            return
        password = self.password.text() if self.chk_unattended.isChecked() else None
        if self.chk_unattended.isChecked() and not password:
            QMessageBox.warning(self, "BrickMote", "Saisissez le mot de passe d'accès sans surveillance.")
            return
        win = ViewerWindow(self.core_thread, format_id(target) if target.replace(" ", "").isdigit() else target)
        win.setWindowIcon(app_icon())
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(lambda *_: self._viewers.remove(win) if win in self._viewers else None)
        win.destroyed.connect(lambda *_: QTimer.singleShot(0, self.refresh_recent))
        self._viewers.append(win)
        win.show()
        win.connect_with(self.core, password)
        self.password.clear()

    # ------------------------------------------------- user questions
    def _on_request(self, req: PendingRequest):
        if req.kind == "incoming":
            dlg = IncomingDialog(req, self.core.settings.interactive_permissions)
            dlg.setWindowIcon(app_icon())
            dlg.decided.connect(self._on_incoming_decided)
            self._dialogs[id(req)] = dlg
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            QApplication_alert(dlg)
            if self.tray:
                self.tray.showMessage("BrickMote", f"{req.peer.name} demande à se connecter à ce poste")
        elif req.kind == "totp":
            dlg = QInputDialog()
            dlg.setWindowTitle("BrickMote - Double authentification")
            dlg.setLabelText(
                f"{req.peer.name} ({format_id(req.peer.device_id)}) exige un code 2FA.\nCode à 6 chiffres :"
            )
            dlg.setWindowFlag(Qt.WindowStaysOnTopHint)
            dlg.finished.connect(
                lambda result, d=dlg: req.answer(d.textValue().strip() if result else None)
            )
            self._show_request_dialog(req, dlg)
        elif req.kind == "trust":
            peer = req.peer
            dlg = QMessageBox(
                QMessageBox.Question,
                "BrickMote - Nouveau poste",
                f"Première connexion à ce poste :\n\n"
                f"Nom : {peer.name}\nID : {format_id(peer.device_id)}\nEmpreinte : {peer.fingerprint}\n\n"
                "Vérifiez que cet ID est bien celui affiché sur le poste distant avant de continuer.",
                QMessageBox.Yes | QMessageBox.No,
            )
            dlg.setDefaultButton(QMessageBox.No)
            dlg.setWindowFlag(Qt.WindowStaysOnTopHint)
            dlg.finished.connect(lambda result, d=dlg: req.answer(d.clickedButton() == d.button(QMessageBox.Yes)))
            self._show_request_dialog(req, dlg)

    def _show_request_dialog(self, req: PendingRequest, dlg: QWidget):
        dlg.setWindowIcon(app_icon())
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        self._dialogs[id(req)] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_request_closed(self, req: PendingRequest):
        dlg = self._dialogs.pop(id(req), None)
        if dlg is not None:
            try:
                dlg.close()
            except RuntimeError:  # already deleted
                pass

    def _on_incoming_decided(self, req: PendingRequest, perms, block: bool):
        self._dialogs.pop(id(req), None)
        if block:
            self._block(req.peer.device_id)
        req.answer(perms)

    def _block(self, device_id: str):
        s = self.core.settings
        if device_id not in s.blocked:
            s.blocked.append(device_id)
            self.core_thread.submit(_call(self.core.save_settings))

    # ------------------------------------------------------- host side
    def _on_host_session(self, session):
        panel = HostSessionPanel(session)
        panel.setWindowIcon(app_icon())
        panel.permissions_changed.connect(lambda s, p: self.core_thread.submit(s.set_permissions(p)))
        panel.disconnect_requested.connect(self._disconnect_host_session)
        panel.chat_requested.connect(self._open_host_chat)
        panel.send_file_requested.connect(self._host_send_file)
        self._panels[id(session)] = panel
        panel.show()
        item = QListWidgetItem(f"● {session.peer.name} ({format_id(session.peer.device_id)}) — {session.peer.address}")
        item.setData(Qt.UserRole, id(session))
        self.list_sessions.addItem(item)
        if self.tray:
            self.tray.showMessage("BrickMote", f"{session.peer.name} est connecté à ce poste")

    def _disconnect_host_session(self, session, block: bool):
        if block:
            self._block(session.peer.device_id)
        self.core_thread.submit(session.close("l'utilisateur de l'hôte a mis fin à la session"))

    def _on_host_closed(self, session, reason: str):
        panel = self._panels.pop(id(session), None)
        if panel is not None:
            panel.close()
            panel.deleteLater()
        chat = self._chats.pop(id(session), None)
        if chat is not None:
            chat.close()
        for i in range(self.list_sessions.count()):
            if self.list_sessions.item(i).data(Qt.UserRole) == id(session):
                self.list_sessions.takeItem(i)
                break
        if self.tray:
            self.tray.showMessage("BrickMote", f"Session avec {session.peer.name} terminée : {reason}")

    def _open_host_chat(self, session) -> ChatDialog:
        chat = self._chats.get(id(session))
        if chat is None:
            chat = ChatDialog(
                f"Chat - {session.peer.name}", lambda t, s=session: self.core_thread.submit(s.send_chat(t))
            )
            chat.setWindowFlag(Qt.WindowStaysOnTopHint)
            self._chats[id(session)] = chat
        chat.show()
        chat.raise_()
        return chat

    def _on_host_chat(self, session, text: str):
        self._open_host_chat(session).append(session.peer.name, text)

    def _host_send_file(self, session):
        path, _ = QFileDialog.getOpenFileName(None, "Envoyer un fichier au poste client")
        if path:
            self.core_thread.submit(session.send_file(path))

    def _on_host_clipboard(self, session, text: str):
        self._last_remote_clip = text
        QGuiApplication.clipboard().setText(text)

    def _on_local_clipboard(self):
        text = QGuiApplication.clipboard().text()
        if not text or text == self._last_remote_clip:
            return
        self._last_remote_clip = text
        for session in list(self.core.host_sessions):
            if session.permissions.get("clipboard"):
                self.core_thread.submit(session.send_clipboard(text))

    def _on_host_file_progress(self, session, tid, name, done, total, incoming):
        panel = self._panels.get(id(session))
        if panel:
            pct = int(done * 100 / total) if total else 100
            panel.status.setText(f"{'Réception' if incoming else 'Envoi'} de {name} : {pct} %")

    def _on_host_file_done(self, session, tid, name, path, error, incoming):
        panel = self._panels.get(id(session))
        if panel:
            panel.status.setText(f"{name} : {error}" if error else (f"Reçu : {path}" if incoming else f"Envoyé : {name}"))

    # --------------------------------------------------------- closing
    def closeEvent(self, event):
        if self._quitting or self.tray is None:
            self._quitting = True
            event.accept()
            QGuiApplication.quit()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "BrickMote", "BrickMote reste actif dans la zone de notification pour recevoir les connexions."
        )

    def quit(self):
        self._quitting = True
        for win in list(self._viewers):
            win.close()
        self.close()


async def _call(fn, *args):
    return fn(*args)


def QApplication_alert(widget):
    from PySide6.QtWidgets import QApplication

    QApplication.alert(widget, 0)
