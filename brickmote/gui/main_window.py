"""Main window: sidebar navigation, this computer's ID, connect bar, devices."""

from __future__ import annotations

import os
import socket
import sys
import time
from typing import Optional

import asyncio

from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
    QScrollArea,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import BrickMoteCore
from ..util import format_id, normalize_id, secure_write
from .bridge import CoreThread, PendingRequest, QtCoreUI
from ..net.wol import WolError, merge_wol, send_wake
from .dialogs import AuditDialog, ChatDialog, HostSessionPanel, IncomingDialog, SettingsDialog, WakeDialog
from .icons import icon, pixmap
from .style import C, app_icon, avatar, logo, shadow
from .viewer_window import ViewerWindow
from .devices import DeviceBrowser
from .widgets import Card, EmptyState, StatusRow, section_header

NAV_COLOR, NAV_ACTIVE = "#8B919C", "#FFFFFF"

AUDIT_LABELS = {
    "request": "Demande de connexion",
    "accepted": "Connexion acceptée",
    "refused": "Connexion refusée",
    "auth_failure": "Échec d'authentification",
    "session_start": "Début de session",
    "session_end": "Fin de session",
    "start": "Démarrage de BrickMote",
    "stop": "Arrêt de BrickMote",
}


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


def _scroll_page(title: str, subtitle: str, header: bool = True) -> tuple[QScrollArea, QVBoxLayout]:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setFrameShape(QFrame.NoFrame)
    page = QWidget(objectName="Page")
    area.setWidget(page)
    lay = QVBoxLayout(page)
    lay.setContentsMargins(34, 28, 34, 28)
    lay.setSpacing(16)
    if header:
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(QLabel(title, objectName="PageTitle"))
        head.addWidget(QLabel(subtitle, objectName="PageSubtitle"))
        lay.addLayout(head)
    return area, lay


class MainWindow(QMainWindow):
    wake_result = Signal(str, bool)  # message, success (emitted from the network thread)

    def __init__(self, core: BrickMoteCore, core_thread: CoreThread, ui: QtCoreUI):
        super().__init__()
        self.core = core
        self.core_thread = core_thread
        self.ui = ui
        self._dialogs: dict[int, QWidget] = {}
        self._panels: dict[int, HostSessionPanel] = {}
        self._chats: dict[int, ChatDialog] = {}
        self._host_sessions: dict[int, object] = {}
        self._session_started: dict[int, float] = {}
        self._viewers: list[ViewerWindow] = []
        self._lan_peers: dict = {}
        self._thumb_cache: dict = {}
        self._last_remote_clip: Optional[str] = None
        self._quitting = False
        self.setWindowTitle("BrickMote")
        self.setWindowIcon(app_icon())
        self.resize(1120, 740)
        self.setMinimumSize(940, 620)
        self._build()
        self._wire()
        self._setup_tray()
        self.refresh_identity()
        self._on_relay_status(*ui.last_relay_status)
        self._on_lan_peers(ui.last_lan_peers)
        self.refresh_sessions()
        QTimer.singleShot(1500, self.refresh_identity)
        self._clock = QTimer(self, interval=30_000, timeout=self._refresh_devices)
        self._clock.start()

    # ================================================================ layout
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_home())
        self.pages.addWidget(self._build_sessions())

    def _nav_button(self, icon_name: str, text: str, checkable: bool = True) -> QPushButton:
        b = QPushButton(f"  {text}", objectName="Nav")
        b.setIcon(icon(icon_name, NAV_COLOR, 20, active=NAV_ACTIVE))
        b.setCheckable(checkable)
        b.setCursor(Qt.PointingHandCursor)
        b.setMinimumHeight(42)
        return b

    def _build_sidebar(self) -> QWidget:
        side = QFrame(objectName="Sidebar")
        side.setFixedWidth(232)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(16, 22, 16, 18)
        lay.setSpacing(4)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(logo(34))
        brand.addWidget(mark)
        brand.addWidget(QLabel("BrickMote", objectName="Brand"))
        brand.addStretch(1)
        lay.addLayout(brand)
        lay.addSpacing(26)
        lay.addWidget(QLabel("NAVIGATION", objectName="SidebarCaption"))
        lay.addSpacing(4)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_home = self._nav_button("home", "Accueil")
        self.nav_sessions = self._nav_button("users", "Sessions")
        badge_lay = QHBoxLayout(self.nav_sessions)
        badge_lay.setContentsMargins(0, 0, 10, 0)
        badge_lay.addStretch(1)
        self.sessions_badge = QLabel("0", objectName="NavBadge")
        self.sessions_badge.setAlignment(Qt.AlignCenter)
        self.sessions_badge.hide()
        badge_lay.addWidget(self.sessions_badge)
        for i, b in enumerate((self.nav_home, self.nav_sessions)):
            self.nav_group.addButton(b, i)
            lay.addWidget(b)
        self.nav_home.setChecked(True)
        lay.addStretch(1)
        lay.addWidget(QLabel("SÉCURITÉ", objectName="SidebarCaption"))
        lay.addSpacing(4)
        self.nav_log = self._nav_button("log", "Journal", checkable=False)
        self.nav_settings = self._nav_button("settings", "Paramètres", checkable=False)
        lay.addWidget(self.nav_log)
        lay.addWidget(self.nav_settings)
        lay.addSpacing(16)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        lock = QLabel()
        lock.setPixmap(pixmap("lock", "#8B919C", 15))
        foot.addWidget(lock, alignment=Qt.AlignTop)
        foot_text = QLabel(f"Chiffré de bout en bout\nVersion {__version__}", objectName="SidebarFoot")
        foot.addWidget(foot_text, 1)
        lay.addLayout(foot)
        return side

    # ---------------------------------------------------------------- home
    def _build_home(self) -> QWidget:
        area, lay = _scroll_page("Accueil", "", header=False)

        # connect bar
        bar = QFrame(objectName="ConnectBar")
        shadow(bar, blur=12, dy=1, alpha=18)
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(18, 12, 12, 12)
        bl.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        search = QLabel()
        search.setPixmap(pixmap("search", C["MUTED"], 22))
        row.addWidget(search)
        self.target = QLineEdit(objectName="TargetInput")
        self.target.setPlaceholderText("Saisissez l'ID (123 456 789) ou l'adresse IP du poste distant")
        row.addWidget(self.target, 1)
        self.btn_connect = QPushButton("Se connecter  ", objectName="ConnectButton")
        self.btn_connect.setIcon(icon("arrow", "#FFFFFF", 18))
        self.btn_connect.setLayoutDirection(Qt.RightToLeft)
        self.btn_connect.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.btn_connect)
        bl.addLayout(row)
        opt = QHBoxLayout()
        opt.setContentsMargins(34, 0, 0, 2)
        self.chk_unattended = QCheckBox("Accès sans surveillance (mot de passe du poste distant)")
        opt.addWidget(self.chk_unattended)
        self.password = QLineEdit(objectName="PasswordInput", echoMode=QLineEdit.Password)
        self.password.setPlaceholderText("Mot de passe")
        self.password.setMaximumWidth(260)
        self.password.setVisible(False)
        opt.addWidget(self.password)
        opt.addStretch(1)
        self.btn_wake = QPushButton("  Réveiller un PC", objectName="WakeLink")
        self.btn_wake.setIcon(icon("power", C["MUTED"], 16))
        self.btn_wake.setToolTip("Envoyer un signal Wake-on-LAN (paquet magique) sur le port UDP de votre choix")
        self.btn_wake.setCursor(Qt.PointingHandCursor)
        opt.addWidget(self.btn_wake)
        bl.addLayout(opt)
        lay.addWidget(bar)

        # hero (this computer) + status
        top = QHBoxLayout()
        top.setSpacing(18)
        hero = QFrame(objectName="Hero")
        shadow(hero, blur=12, dy=2, alpha=40)
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(24, 16, 24, 16)
        hl.setSpacing(4)
        cap = QHBoxLayout()
        cap.addWidget(QLabel("CE POSTE · VOTRE ADRESSE", objectName="HeroCaption"))
        cap.addStretch(1)
        pill = QFrame(objectName="HeroPill")
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(10, 4, 12, 4)
        pl.setSpacing(6)
        lock = QLabel()
        lock.setPixmap(pixmap("lock", "#FFFFFF", 13, 2.2))
        pl.addWidget(lock)
        pl.addWidget(QLabel("Chiffré de bout en bout", objectName="HeroPillText"))
        cap.addWidget(pill)
        hl.addLayout(cap)
        idrow = QHBoxLayout()
        self.lbl_id = QLabel("", objectName="HeroId")
        self.lbl_id.setTextInteractionFlags(Qt.TextSelectableByMouse)
        idrow.addWidget(self.lbl_id, 1)
        self.btn_copy = QPushButton("  Copier", objectName="HeroButton")
        self.btn_copy.setIcon(icon("copy", "#FFFFFF", 16))
        self.btn_copy.setCursor(Qt.PointingHandCursor)
        idrow.addWidget(self.btn_copy, alignment=Qt.AlignVCenter)
        hl.addLayout(idrow)
        self.lbl_name = QLabel("", objectName="HeroMeta")
        hl.addWidget(self.lbl_name)
        hint = QLabel(
            "Communiquez cet ID à la personne qui doit accéder à ce poste. "
            "Chaque demande devra être acceptée ici.",
            objectName="HeroHint",
        )
        hint.setWordWrap(True)
        self.hero_hint = hint
        hl.addWidget(hint)
        hl.addStretch(1)
        self.lbl_fp = QLabel("", objectName="HeroFp")
        self.lbl_fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_fp.setToolTip("Empreinte de la clé d'identité Ed25519 de ce poste")
        hl.addWidget(self.lbl_fp)
        top.addWidget(hero, 5)

        status = Card(margins=18, spacing=0)
        status.body.addWidget(QLabel("État de la connexion", objectName="CardTitle"))
        status.body.addSpacing(2)
        self.row_relay = StatusRow("server", "Serveur relais")
        self.row_direct = StatusRow("link", "Connexion directe")
        self.row_access = StatusRow("shield", "Accès entrant")
        for r in (self.row_relay, self.row_direct, self.row_access):
            status.body.addWidget(r)
        status.body.addStretch(1)
        top.addWidget(status, 4)
        lay.addLayout(top)

        self.lbl_warning = QLabel("", objectName="Warning")
        self.lbl_warning.setWordWrap(True)
        lay.addWidget(self.lbl_warning)

        lay.addSpacing(6)
        self.browser = DeviceBrowser(self.core.settings.device_view)
        lay.addWidget(self.browser)
        lay.addStretch(1)
        return area

    # ------------------------------------------------------------ sessions
    def _build_sessions(self) -> QWidget:
        area, lay = _scroll_page("Sessions", "Les personnes connectées à ce poste et l'activité de sécurité récente.")
        lay.addWidget(section_header("Connexions en cours sur ce poste"))
        self.sessions_box = QVBoxLayout()
        self.sessions_box.setSpacing(12)
        lay.addLayout(self.sessions_box)
        self.sessions_empty = EmptyState("Personne n'est connecté à ce poste.\nLes demandes de connexion s'afficheront ici.")
        lay.addWidget(self.sessions_empty)
        lay.addWidget(section_header("Activité récente", "Journal de sécurité"))
        self.activity = QListWidget()
        self.activity.setMinimumHeight(240)
        self.activity.setFocusPolicy(Qt.NoFocus)
        shadow(self.activity, blur=8, dy=1, alpha=12)
        lay.addWidget(self.activity)
        lay.addStretch(1)
        return area

    # =============================================================== wiring
    def _wire(self):
        self.nav_group.idClicked.connect(self._go)
        self.browser.connect_requested.connect(self._connect_to)
        self.browser.favorite_toggled.connect(self._toggle_favorite)
        self.browser.rename_requested.connect(self._rename)
        self.browser.forget_requested.connect(self._forget)
        self.browser.view_changed.connect(self._set_view)
        self.browser.wake_requested.connect(self._wake_device)
        self.browser.wake_settings_requested.connect(self._wake_settings)
        self.btn_wake.clicked.connect(self._wake_manual)
        self.wake_result.connect(self.show_toast)
        self.nav_settings.clicked.connect(self.open_settings)
        self.nav_log.clicked.connect(lambda: AuditDialog(self.core.audit, self).exec())
        self.btn_connect.clicked.connect(self.connect_clicked)
        self.btn_copy.clicked.connect(self._copy_id)
        self.target.returnPressed.connect(self.connect_clicked)
        self.password.returnPressed.connect(self.connect_clicked)
        self.chk_unattended.toggled.connect(self._toggle_password)
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
        h.permissions.connect(
            lambda sess, perms: self._panels.get(id(sess)) and self._panels[id(sess)].set_permissions(perms)
        )
        h.file_progress.connect(self._on_host_file_progress)
        h.file_done.connect(self._on_host_file_done)
        QGuiApplication.clipboard().dataChanged.connect(self._on_local_clipboard)

    def _go(self, index: int):
        self.pages.setCurrentIndex(index)
        self.nav_group.button(index).setChecked(True)
        if index == 1:
            self.refresh_activity()

    def _toggle_password(self, on: bool):
        self.password.setVisible(on)
        if on:
            self.password.setFocus()

    def _copy_id(self):
        QGuiApplication.clipboard().setText(self.core.identity.device_id)
        self.btn_copy.setText("  Copié")
        self.btn_copy.setIcon(icon("check", "#FFFFFF", 16))

        def reset():
            self.btn_copy.setText("  Copier")
            self.btn_copy.setIcon(icon("copy", "#FFFFFF", 16))

        QTimer.singleShot(1600, reset)

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

    # =============================================================== status
    def refresh_identity(self):
        s = self.core.settings
        self.lbl_id.setText(self.core.identity.display_id)
        self.lbl_name.setText(s.device_name)
        self.lbl_fp.setText(f"Empreinte  {self.core.identity.fingerprint}")
        port = self.core.direct_port
        if port:
            ips = ", ".join(local_ips()) or "adresse inconnue"
            self.row_direct.set("Active", "ok", f"TCP {port} · {ips}")
        elif s.direct_enabled:
            self.row_direct.set("Port occupé", "danger", f"Le port TCP {s.direct_port} est déjà utilisé")
        else:
            self.row_direct.set("Désactivée", "neutral", "Activable dans les paramètres")
        if s.unattended_enabled:
            extras = ["mot de passe"]
            extras.append("2FA activée" if s.totp_secret else "sans 2FA")
            if s.acl_enabled:
                extras.append("liste blanche")
            kind = "ok" if s.totp_secret or s.acl_enabled else "warn"
            label = "Sans surveillance"
            detail = " · ".join(extras).capitalize()
            if s.interactive_enabled:
                detail += " · ou sur acceptation"
        elif s.interactive_enabled:
            label, kind = "Sur acceptation", "ok"
            detail = "Chaque connexion doit être acceptée" + (" · liste blanche" if s.acl_enabled else "")
        else:
            label, kind, detail = "Fermé", "danger", "Toutes les connexions entrantes sont refusées"
        self.row_access.set(label, kind, detail)
        self.hero_hint.setText(
            "Communiquez cet ID à la personne qui doit accéder à ce poste. "
            + (
                "Chaque demande devra être acceptée ici."
                if s.interactive_enabled and not s.unattended_enabled
                else "L'accès sans surveillance par mot de passe est activé."
                if s.unattended_enabled
                else "Les connexions entrantes sont actuellement désactivées."
            )
        )
        warnings = []
        if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
            warnings.append(
                "Session Wayland détectée : pour être contrôlé, ouvrez une session « Xorg » (X11) "
                "depuis l'écran de connexion."
            )
        if sys.platform == "darwin":
            warnings.append(
                "macOS : autorisez BrickMote dans Réglages › Confidentialité › Enregistrement de l'écran et Accessibilité."
            )
        self.lbl_warning.setText("⚠  " + "\n".join(warnings) if warnings else "")
        self.lbl_warning.setVisible(bool(warnings))
        if self.tray:
            self.tray.setToolTip(f"BrickMote - {self.core.identity.display_id}")

    def _on_relay_status(self, state: str, detail: str):
        if state == "online":
            where = "" if self.core.relay_masked else f" ({detail})"
            self.row_relay.set("En ligne", "ok", f"Joignable par ID depuis Internet{where}")
        elif state == "connecting":
            where = "au serveur BrickMote" if self.core.relay_masked else f"à {detail}"
            self.row_relay.set("Connexion…", "warn", f"Connexion {where}")
        elif state == "offline":
            if self.core.relay_masked and detail[:1].islower():
                detail = f"Serveur BrickMote {detail}"
            self.row_relay.set("Hors ligne", "danger", detail or "Serveur injoignable")
        else:
            self.row_relay.set("Non configuré", "neutral", "Réseau local et IP directe uniquement")

    # ============================================================== devices
    def _on_lan_peers(self, peers: dict):
        self._lan_peers = dict(peers)
        self._refresh_devices()

    def refresh_recent(self):
        self._refresh_devices()

    def _refresh_devices(self):
        known = {k: v for k, v in self.core.known_hosts.entries().items() if isinstance(v, dict)}
        self.browser.set_data(known, dict(self._lan_peers), self._thumbnail)

    # ----------------------------------------------------- thumbnails
    def _thumb_path(self, device_id: str):
        if not normalize_id(device_id):
            return None
        return self.core.paths.thumbnails / f"{device_id}.jpg"

    def _thumbnail(self, device_id: str) -> Optional[QPixmap]:
        path = self._thumb_path(device_id)
        if path is None or not self.core.settings.save_thumbnails:
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._thumb_cache.get(device_id)
        if cached and cached[0] == mtime:
            return cached[1]
        pix = QPixmap(str(path))
        if pix.isNull():
            return None
        self._thumb_cache[device_id] = (mtime, pix)
        return pix

    def save_thumbnail(self, device_id: str, image: QImage) -> None:
        """Called by viewer windows: keep a small preview of the remote screen."""
        path = self._thumb_path(device_id)
        if path is None or not self.core.settings.save_thumbnails or image.isNull():
            return
        thumb = image.scaled(480, 270, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        thumb.save(buf, "JPG", 70)
        try:
            secure_write(path, bytes(buf.data()))
        except OSError:
            return
        self._thumb_cache.pop(device_id, None)
        QTimer.singleShot(0, self._refresh_devices)

    def clear_thumbnails(self) -> None:
        folder = self.core.paths.thumbnails
        for f in folder.glob("*.jpg") if folder.exists() else []:
            try:
                f.unlink()
            except OSError:
                pass
        self._thumb_cache.clear()
        self._refresh_devices()

    # -------------------------------------------------- device actions
    def _toggle_favorite(self, dev: str, on: bool):
        self.core.known_hosts.set_favorite(dev, on)
        self._refresh_devices()

    def _rename(self, dev: str):
        entry = self.core.known_hosts.entries().get(dev, {})
        current = entry.get("alias") or entry.get("name") or ""
        text, ok = QInputDialog.getText(
            self, "Renommer le poste", f"Nom affiché pour {format_id(dev)} :", QLineEdit.Normal, current
        )
        if ok:
            self.core.known_hosts.set_alias(dev, text)
            self._refresh_devices()

    def _forget(self, dev: str):
        if (
            QMessageBox.question(
                self,
                "BrickMote",
                f"Oublier le poste {format_id(dev)} ?\n"
                "Sa clé d'identité épinglée et sa miniature seront supprimées.",
            )
            == QMessageBox.Yes
        ):
            self.core.known_hosts.forget(dev)
            path = self._thumb_path(dev)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            self._thumb_cache.pop(dev, None)
            self._refresh_devices()

    # ------------------------------------------------- Wake-on-LAN
    def _wol_of(self, dev: str) -> Optional[dict]:
        entry = self.core.known_hosts.entries().get(dev)
        wol = entry.get("wol") if isinstance(entry, dict) else None
        return wol if isinstance(wol, dict) and wol.get("mac") else None

    def _label_of(self, dev: str) -> str:
        entry = self.core.known_hosts.entries().get(dev) or {}
        name = entry.get("alias") or entry.get("name") or ""
        return f"{name} ({format_id(dev)})" if name else format_id(dev)

    def learn_wol(self, dev: str, info: dict) -> None:
        """A connected host told us its MAC: remember it for one-click wake."""
        if not normalize_id(dev) or not info:
            return
        current = self._wol_of(dev)
        merged = merge_wol(current, info)
        if merged != current:
            self.core.known_hosts.set_wol(dev, merged)
            QTimer.singleShot(0, self._refresh_devices)

    def wake_action_for(self, target: str):
        """For the viewer's 'connection failed' dialog: a wake callback, or None."""
        dev = normalize_id(target or "")
        if dev and self._wol_of(dev):
            return lambda: self._wake_device(dev)
        return None

    def _wake_device(self, dev: str):
        wol = self._wol_of(dev)
        if wol is None:
            self._wake_settings(dev)
            return
        self._send_wake(wol, self._label_of(dev))

    def _wake_settings(self, dev: str):
        dlg = WakeDialog(self, self._wol_of(dev), self._label_of(dev), can_save=True)
        if not dlg.exec():
            return
        if dlg.action == "save" or dlg.remember.isChecked():
            self.core.known_hosts.set_wol(dev, dlg.values)
            self._refresh_devices()
        if dlg.action == "wake":
            self._send_wake(dlg.values, self._label_of(dev))

    def _wake_manual(self):
        dlg = WakeDialog(self)
        if dlg.exec() and dlg.action == "wake":
            self._send_wake(dlg.values, dlg.values["mac"])

    def _send_wake(self, wol: dict, label: str):
        """Send the magic packet to the box (public address) and on the LAN."""

        def done(fut):
            try:
                sent, errors = fut.result()
            except (WolError, OSError, ValueError) as exc:
                self.wake_result.emit(f"Réveil de {label} impossible : {exc}", False)
                return
            box = (wol.get("box") or "").strip()
            via = []
            if box and any(t.startswith(box + ":") for t in sent):
                via.append(f"box {box}:{wol.get('port') or 9}")
            if any(not (box and t.startswith(box + ":")) for t in sent):
                via.append("réseau local")
            text = f"Signal de réveil envoyé à {label} ({' + '.join(via)}).\nLe démarrage peut prendre jusqu'à une minute."
            if errors:
                text += "\nNon envoyé : " + " ; ".join(errors)
            self.wake_result.emit(text, True)

        fut = self.core_thread.submit(asyncio.to_thread(send_wake, dict(wol)))
        fut.add_done_callback(done)

    # ------------------------------------------------------- toast
    def show_toast(self, text: str, ok: bool = True):
        if not hasattr(self, "_toast"):
            self._toast = QLabel(self.centralWidget())
            self._toast.setWordWrap(True)
            self._toast.setMaximumWidth(460)
            self._toast_timer = QTimer(self, singleShot=True, timeout=self._toast.hide)
            shadow(self._toast, blur=16, dy=2, alpha=40)
        self._toast.setObjectName("Toast" if ok else "ToastError")
        self._toast.style().unpolish(self._toast)
        self._toast.style().polish(self._toast)
        self._toast.setText(text)
        self._toast.adjustSize()
        self._place_toast()
        self._toast.show()
        self._toast.raise_()
        self._toast_timer.start(6000 if ok else 9000)

    def _place_toast(self):
        if hasattr(self, "_toast"):
            area = self.centralWidget().rect()
            self._toast.move(area.right() - self._toast.width() - 24, area.bottom() - self._toast.height() - 24)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_toast()

    def _set_view(self, view: str):
        self.core.settings.device_view = view
        self.core_thread.submit(_call(self.core.save_settings))

    # ============================================================= sessions
    def refresh_sessions(self):
        while self.sessions_box.count():
            item = self.sessions_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for sid, session in self._host_sessions.items():
            self.sessions_box.addWidget(self._session_card(session))
        count = len(self._host_sessions)
        self.sessions_empty.setVisible(count == 0)
        self.sessions_badge.setText(str(count))
        self.sessions_badge.setVisible(count > 0)

    def _session_card(self, session) -> QWidget:
        card = Card(margins=18, spacing=0)
        row = QHBoxLayout()
        row.setSpacing(14)
        peer = session.peer
        av = QLabel()
        av.setPixmap(avatar(peer.name, peer.device_id, 48))
        row.addWidget(av)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(QLabel(peer.name, objectName="DeviceName"))
        since = self._session_started.get(id(session), time.time())
        method = "Mot de passe" if session.method == "password" else "Acceptée manuellement"
        text.addWidget(
            QLabel(
                f"ID {format_id(peer.device_id)} · {peer.address or ''} · depuis {time.strftime('%H:%M', time.localtime(since))}",
                objectName="DeviceId",
            )
        )
        text.addWidget(QLabel(f"{method} · code de sécurité {session.sas}", objectName="DeviceTag"))
        row.addLayout(text, 1)
        pill = QLabel("● En cours", objectName="PillOk")
        row.addWidget(pill, alignment=Qt.AlignVCenter)
        chat = QPushButton("  Chat")
        chat.setIcon(icon("chat", C["ICON"], 16))
        chat.clicked.connect(lambda _=False, s=session: self._open_host_chat(s))
        send = QPushButton("  Fichier")
        send.setIcon(icon("file", C["ICON"], 16))
        send.clicked.connect(lambda _=False, s=session: self._host_send_file(s))
        stop = QPushButton("  Déconnecter", objectName="Primary")
        stop.setIcon(icon("power", "#FFFFFF", 16))
        stop.clicked.connect(lambda _=False, s=session: self._disconnect_host_session(s, False))
        for b in (chat, send, stop):
            b.setCursor(Qt.PointingHandCursor)
            row.addWidget(b, alignment=Qt.AlignVCenter)
        card.body.addLayout(row)
        return card

    def refresh_activity(self):
        self.activity.clear()
        for e in reversed(self.core.audit.tail(60)):
            event = e.get("event")
            label = AUDIT_LABELS.get(event, event)
            ts = (e.get("ts") or "")[:16].replace("T", "  ")
            who = e.get("name") or ""
            peer = e.get("peer")
            parts = [ts, label]
            if peer:
                parts.append(f"{format_id(peer)} {who}".strip())
            if e.get("reason"):
                parts.append(str(e["reason"]))
            item = QListWidgetItem("    ".join(parts))
            color = {"auth_failure": C["DANGER"], "refused": C["WARN"], "accepted": C["OK"]}.get(event)
            if color:
                item.setIcon(icon({"auth_failure": "alert", "refused": "x", "accepted": "check"}[event], color, 16))
            else:
                item.setIcon(icon("clock", C["FAINT"], 16))
            self.activity.addItem(item)
        if self.activity.count() == 0:
            self.activity.addItem("Aucune activité enregistrée.")

    # ============================================================= settings
    def open_settings(self):
        dlg = SettingsDialog(
            self.core.settings,
            self.core.identity.device_id,
            self.core.known_hosts,
            self.core.audit,
            self,
            clear_thumbnails=self.clear_thumbnails,
            public_ip=self.core.public_ip or "",
            builtin_relay=self.core.builtin_relay is not None,
        )
        if dlg.exec():
            new = dlg.result_settings()
            if not new.save_thumbnails:
                self.clear_thumbnails()

            async def apply():
                self.core.settings = new
                self.core.save_settings()
                await self.core.apply_settings()

            fut = self.core_thread.submit(apply())
            fut.add_done_callback(lambda f: QTimer.singleShot(0, self.refresh_identity))
            QTimer.singleShot(800, self.refresh_identity)
        self._refresh_devices()

    # ========================================================== viewer side
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
            self.password.setFocus()
            return
        win = ViewerWindow(
            self.core_thread,
            format_id(target) if target.replace(" ", "").isdigit() else target,
            on_thumbnail=self.save_thumbnail,
        )
        win.on_wol_learned = self.learn_wol
        win.wake_action = self.wake_action_for
        win.setWindowIcon(app_icon())
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(lambda *_: self._viewers.remove(win) if win in self._viewers else None)
        win.destroyed.connect(lambda *_: QTimer.singleShot(0, self._refresh_devices))
        self._viewers.append(win)
        win.show()
        win.connect_with(self.core, password)
        self.password.clear()

    # ====================================================== user questions
    def _on_request(self, req: PendingRequest):
        if req.kind == "incoming":
            dlg = IncomingDialog(req, self.core.settings.interactive_permissions)
            dlg.setWindowIcon(app_icon())
            dlg.decided.connect(self._on_incoming_decided)
            self._dialogs[id(req)] = dlg
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            QApplication.alert(dlg, 0)
            if self.tray:
                self.tray.showMessage("BrickMote", f"{req.peer.name} demande à se connecter à ce poste")
        elif req.kind == "totp":
            dlg = QInputDialog()
            dlg.setWindowTitle("BrickMote - Double authentification")
            dlg.setLabelText(
                f"{req.peer.name} ({format_id(req.peer.device_id)}) exige un code 2FA.\nCode à 6 chiffres :"
            )
            dlg.setWindowFlag(Qt.WindowStaysOnTopHint)
            dlg.finished.connect(lambda result, d=dlg: req.answer(d.textValue().strip() if result else None))
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

    # =========================================================== host side
    def _on_host_session(self, session):
        panel = HostSessionPanel(session)
        panel.setWindowIcon(app_icon())
        panel.permissions_changed.connect(lambda s, p: self.core_thread.submit(s.set_permissions(p)))
        panel.disconnect_requested.connect(self._disconnect_host_session)
        panel.chat_requested.connect(self._open_host_chat)
        panel.send_file_requested.connect(self._host_send_file)
        self._panels[id(session)] = panel
        self._host_sessions[id(session)] = session
        self._session_started[id(session)] = time.time()
        panel.show()
        self.refresh_sessions()
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
        self._host_sessions.pop(id(session), None)
        self._session_started.pop(id(session), None)
        self.refresh_sessions()
        if self.pages.currentIndex() == 1:
            self.refresh_activity()
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
        for session in list(self._host_sessions.values()):
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
            panel.status.setText(
                f"{name} : {error}" if error else (f"Reçu : {path}" if incoming else f"Envoyé : {name}")
            )

    # ============================================================= closing
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
