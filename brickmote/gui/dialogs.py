"""Dialogs: incoming request, host session panel, chat, settings, security."""

from __future__ import annotations

import copy
import io
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import AuditLog, KnownHosts, Settings
from ..constants import INTERACTIVE_ACCEPT_TIMEOUT
from ..crypto.password import (
    PasswordVerifier,
    TotpVerifier,
    generate_totp_secret,
    password_problems,
    totp_uri,
)
from ..util import format_id, normalize_id
from .bridge import PendingRequest
from .style import logo

PERM_LABELS = {
    "control": "Contrôler la souris et le clavier",
    "clipboard": "Synchroniser le presse-papiers",
    "files": "Transférer des fichiers",
}


def _label(text: str, name: Optional[str] = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


def _button(text: str, name: Optional[str] = None) -> QPushButton:
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    return b


# --------------------------------------------------------------------------
# Incoming connection request (host side)
# --------------------------------------------------------------------------


class IncomingDialog(QDialog):
    decided = Signal(object, object, bool)  # request, permissions|None, block

    def __init__(self, req: PendingRequest, defaults: dict, parent=None):
        super().__init__(parent)
        self.req = req
        self.setWindowTitle("BrickMote - Demande de connexion")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowTitleHint)
        self.setModal(False)
        self.setMinimumWidth(460)
        peer = req.peer
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(logo(56))
        top.addWidget(mark)
        head = QVBoxLayout()
        head.addWidget(_label(f"<b>{_esc(peer.name)}</b> souhaite se connecter à ce poste", "CardTitle", True))
        head.addWidget(_label(f"ID {format_id(peer.device_id)} · {_esc(peer.address or '')}", "Muted"))
        top.addLayout(head, 1)
        lay.addLayout(top)
        lay.addWidget(_label(f"Empreinte : {peer.fingerprint}", "Small", True))

        sas_box = QGroupBox("Code de sécurité de la session")
        sl = QVBoxLayout(sas_box)
        sl.addWidget(_label(req.sas, "Sas"))
        sl.addWidget(
            _label(
                "Si votre correspondant vous a contacté, vérifiez qu'il voit exactement le même code. "
                "N'acceptez jamais une demande que vous n'attendiez pas.",
                "Small",
                True,
            )
        )
        lay.addWidget(sas_box)

        perm_box = QGroupBox("Autorisations accordées")
        pl = QVBoxLayout(perm_box)
        pl.addWidget(_label("Affichage de l'écran (toujours)", "Muted"))
        self.checks = {}
        for key, text in PERM_LABELS.items():
            cb = QCheckBox(text)
            cb.setChecked(bool(defaults.get(key)))
            pl.addWidget(cb)
            self.checks[key] = cb
        lay.addWidget(perm_box)

        self.countdown = _label("", "Small")
        lay.addWidget(self.countdown)
        buttons = QHBoxLayout()
        block = _button("Refuser et bloquer", "Danger")
        refuse = _button("Refuser")
        accept = _button("Accepter", "Primary")
        buttons.addWidget(block)
        buttons.addStretch(1)
        buttons.addWidget(refuse)
        buttons.addWidget(accept)
        lay.addLayout(buttons)
        accept.clicked.connect(self._accept)
        refuse.clicked.connect(lambda: self._finish(None, False))
        block.clicked.connect(lambda: self._finish(None, True))
        refuse.setFocus()  # Enter must never accept by accident

        self._deadline = time.monotonic() + INTERACTIVE_ACCEPT_TIMEOUT - 2
        self._timer = QTimer(self, interval=500, timeout=self._tick)
        self._timer.start()
        self._tick()

    def _tick(self):
        left = int(self._deadline - time.monotonic())
        if left <= 0:
            self._finish(None, False)
        else:
            self.countdown.setText(f"Refus automatique dans {left} s")

    def _accept(self):
        self._finish({k: cb.isChecked() for k, cb in self.checks.items()}, False)

    def _finish(self, perms, block):
        if self._timer.isActive():
            self._timer.stop()
            self.decided.emit(self.req, perms, block)
        self.close()

    def closeEvent(self, event):
        if self._timer.isActive():
            self._timer.stop()
            self.decided.emit(self.req, None, False)
        super().closeEvent(event)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


class ChatDialog(QDialog):
    def __init__(self, title: str, send: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(380, 420)
        self._send = send
        lay = QVBoxLayout(self)
        self.log = QTextEdit(readOnly=True)
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.input = QLineEdit(placeholderText="Votre message…")
        self.input.setMaxLength(4000)
        btn = _button("Envoyer", "Primary")
        row.addWidget(self.input, 1)
        row.addWidget(btn)
        lay.addLayout(row)
        btn.clicked.connect(self._on_send)
        self.input.returnPressed.connect(self._on_send)

    def _on_send(self):
        text = self.input.text().strip()
        if text:
            self._send(text)
            self.append("Moi", text)
            self.input.clear()

    def append(self, who: str, text: str):
        stamp = time.strftime("%H:%M")
        self.log.append(f"<b>{_esc(who)}</b> <span style='color:#6B7280'>{stamp}</span><br>{_esc(text)}")


# --------------------------------------------------------------------------
# Host session panel (always visible while someone is connected)
# --------------------------------------------------------------------------


class HostSessionPanel(QWidget):
    permissions_changed = Signal(object, object)
    disconnect_requested = Signal(object, bool)  # session, block
    chat_requested = Signal(object)
    send_file_requested = Signal(object)

    def __init__(self, session, parent=None):
        super().__init__(parent, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.session = session
        peer = session.peer
        self.setWindowTitle("BrickMote - Session active")
        self.setStyleSheet("QWidget#Panel { background: white; border: 2px solid #C8423B; border-radius: 8px; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QWidget(objectName="Panel")
        outer.addWidget(panel)
        lay = QVBoxLayout(panel)
        lay.addWidget(_label("<span style='color:#C8423B'>●</span> <b>Session à distance en cours</b>"))
        lay.addWidget(_label(f"{_esc(peer.name)} · ID {format_id(peer.device_id)}", "Muted"))
        method = "mot de passe (sans surveillance)" if session.method == "password" else "acceptée manuellement"
        lay.addWidget(_label(f"Accès : {method}", "Small"))
        lay.addWidget(_label(f"Code de sécurité : <b>{session.sas}</b>", "Small"))
        self.checks = {}
        for key, text in PERM_LABELS.items():
            cb = QCheckBox(text)
            cb.setChecked(bool(session.permissions.get(key)))
            cb.toggled.connect(self._perm_changed)
            lay.addWidget(cb)
            self.checks[key] = cb
        self.status = _label("", "Small", True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        chat = _button("Chat")
        send = _button("Envoyer un fichier")
        row.addWidget(chat)
        row.addWidget(send)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        stop = _button("Déconnecter", "Primary")
        block = _button("Déconnecter et bloquer", "Danger")
        row2.addWidget(stop)
        row2.addWidget(block)
        lay.addLayout(row2)
        chat.clicked.connect(lambda: self.chat_requested.emit(self.session))
        send.clicked.connect(lambda: self.send_file_requested.emit(self.session))
        stop.clicked.connect(lambda: self.disconnect_requested.emit(self.session, False))
        block.clicked.connect(lambda: self.disconnect_requested.emit(self.session, True))
        self.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 20, geo.top() + 20)

    def _perm_changed(self):
        self.permissions_changed.emit(self.session, {k: cb.isChecked() for k, cb in self.checks.items()})

    def set_permissions(self, perms: dict):
        for key, cb in self.checks.items():
            cb.blockSignals(True)
            cb.setChecked(bool(perms.get(key)))
            cb.blockSignals(False)

    def closeEvent(self, event):
        # The panel cannot be dismissed while the session runs.
        if not self.session.closed:
            event.ignore()
        else:
            super().closeEvent(event)


# --------------------------------------------------------------------------
# Security dialogs
# --------------------------------------------------------------------------


class PasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Mot de passe d'accès sans surveillance")
        self.verifier: Optional[PasswordVerifier] = None
        lay = QVBoxLayout(self)
        lay.addWidget(
            _label(
                "Ce mot de passe permet de se connecter à ce poste sans que personne n'accepte la demande. "
                "Il n'est jamais stocké en clair ni transmis sur le réseau (protocole SPAKE2).",
                "Muted",
                True,
            )
        )
        form = QFormLayout()
        self.pw1 = QLineEdit(echoMode=QLineEdit.Password)
        self.pw2 = QLineEdit(echoMode=QLineEdit.Password)
        form.addRow("Mot de passe", self.pw1)
        form.addRow("Confirmation", self.pw2)
        lay.addLayout(form)
        self.feedback = _label("", "Small", True)
        lay.addWidget(self.feedback)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setEnabled(False)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.pw1.textChanged.connect(self._check)
        self.pw2.textChanged.connect(self._check)

    def _check(self):
        problems = password_problems(self.pw1.text())
        if self.pw1.text() and self.pw2.text() and self.pw1.text() != self.pw2.text():
            problems.append("les deux saisies diffèrent")
        self.feedback.setText(("Requis : " + " ; ".join(problems)) if problems else "Mot de passe robuste ✓")
        self.ok.setEnabled(not problems and bool(self.pw2.text()))

    def _accept(self):
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.verifier = PasswordVerifier.create(self.pw1.text())
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.pw1.clear()
        self.pw2.clear()
        self.accept()


class TotpSetupDialog(QDialog):
    def __init__(self, account: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Double authentification (TOTP)")
        self.secret = generate_totp_secret()
        self.confirmed = False
        uri = totp_uri(self.secret, account)
        lay = QVBoxLayout(self)
        lay.addWidget(
            _label(
                "Scannez ce QR code avec une application d'authentification (Google Authenticator, "
                "Aegis, 2FAS, Microsoft Authenticator…), puis saisissez le code affiché.",
                "Muted",
                True,
            )
        )
        qr = _qr_pixmap(uri)
        if qr is not None:
            lab = QLabel()
            lab.setPixmap(qr)
            lab.setAlignment(Qt.AlignCenter)
            lay.addWidget(lab)
        lay.addWidget(_label(f"Clé secrète : <b>{' '.join(self.secret[i:i+4] for i in range(0, len(self.secret), 4))}</b>", None, True))
        self.code = QLineEdit(placeholderText="Code à 6 chiffres")
        self.code.setMaxLength(6)
        lay.addWidget(self.code)
        self.feedback = _label("", "Small")
        lay.addWidget(self.feedback)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._verify)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _verify(self):
        if TotpVerifier(self.secret).verify(self.code.text()):
            self.confirmed = True
            self.accept()
        else:
            self.feedback.setText("Code incorrect. Vérifiez l'heure de votre téléphone.")


def _qr_pixmap(text: str) -> Optional[QPixmap]:
    try:
        import qrcode
    except ImportError:
        return None
    img = qrcode.make(text, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qimg = QImage.fromData(buf.getvalue(), "PNG")
    return QPixmap.fromImage(qimg)


class AuditDialog(QDialog):
    LABELS = {
        "request": "Demande de connexion",
        "accepted": "Connexion acceptée",
        "refused": "Connexion refusée",
        "auth_failure": "ÉCHEC d'authentification",
        "session_start": "Début de session",
        "session_end": "Fin de session",
        "start": "Démarrage de BrickMote",
        "stop": "Arrêt de BrickMote",
    }

    def __init__(self, audit: AuditLog, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Journal de sécurité")
        self.resize(720, 460)
        lay = QVBoxLayout(self)
        text = QPlainTextEdit(readOnly=True)
        lines = []
        for e in reversed(audit.tail(500)):
            label = self.LABELS.get(e.get("event"), e.get("event"))
            details = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "event") and v)
            lines.append(f"{e.get('ts', '')}  {label}  {details}")
        text.setPlainText("\n".join(lines) or "Aucun évènement.")
        lay.addWidget(text)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


class IdListEditor(QWidget):
    def __init__(self, ids: list, placeholder: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        for entry in ids:
            dev = entry.get("id") if isinstance(entry, dict) else entry
            note = entry.get("note", "") if isinstance(entry, dict) else ""
            self._add(dev, note)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        self.input = QLineEdit(placeholderText=placeholder)
        add = _button("Ajouter")
        rem = _button("Retirer")
        row.addWidget(self.input, 1)
        row.addWidget(add)
        row.addWidget(rem)
        lay.addLayout(row)
        add.clicked.connect(self._on_add)
        rem.clicked.connect(lambda: [self.list.takeItem(self.list.row(i)) for i in self.list.selectedItems()])

    def _add(self, dev: str, note: str = ""):
        item = QListWidgetItem(format_id(dev) + (f"  ({note})" if note else ""))
        item.setData(Qt.UserRole, (dev, note))
        self.list.addItem(item)

    def _on_add(self):
        dev = normalize_id(self.input.text())
        if not dev:
            QMessageBox.warning(self, "BrickMote", "Saisissez un ID BrickMote à 9 chiffres.")
            return
        if dev not in self.ids():
            self._add(dev)
        self.input.clear()

    def ids(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole)[0] for i in range(self.list.count())]

    def entries(self) -> list[dict]:
        out = []
        for i in range(self.list.count()):
            dev, note = self.list.item(i).data(Qt.UserRole)
            out.append({"id": dev, "note": note})
        return out


def _perm_group(title: str, perms: dict) -> tuple[QGroupBox, dict]:
    box = QGroupBox(title)
    lay = QVBoxLayout(box)
    checks = {}
    for key, text in PERM_LABELS.items():
        cb = QCheckBox(text)
        cb.setChecked(bool(perms.get(key)))
        lay.addWidget(cb)
        checks[key] = cb
    return box, checks


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, device_id: str, known: KnownHosts, audit: AuditLog, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BrickMote - Paramètres")
        self.resize(640, 600)
        self.s = copy.deepcopy(settings)
        self.device_id = device_id
        self.known = known
        self.audit = audit
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)
        tabs.addTab(self._general_tab(), "Général")
        tabs.addTab(self._security_tab(), "Sécurité")
        tabs.addTab(self._access_tab(), "Contrôle d'accès")
        tabs.addTab(self._network_tab(), "Réseau")
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save, cancel = bb.button(QDialogButtonBox.Save), bb.button(QDialogButtonBox.Cancel)
        save.setText("Enregistrer")
        save.setObjectName("Primary")
        cancel.setText("Annuler")
        for b in (save, cancel):
            b.setIcon(QIcon())
            b.setMinimumWidth(110)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ---- tabs
    def _general_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.name = QLineEdit(self.s.device_name)
        self.name.setMaxLength(64)
        form.addRow("Nom de ce poste", self.name)
        self.quality = QComboBox()
        for key, text in (("low", "Faible bande passante"), ("balanced", "Équilibrée"), ("high", "Haute qualité")):
            self.quality.addItem(text, key)
        self.quality.setCurrentIndex(max(0, self.quality.findData(self.s.quality)))
        form.addRow("Qualité d'image envoyée", self.quality)
        self.fps = QSpinBox(minimum=1, maximum=60, value=self.s.fps, suffix=" images/s")
        form.addRow("Fréquence maximale", self.fps)
        return w

    def _security_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        inter = QGroupBox("Accès interactif (une personne accepte la demande)")
        il = QVBoxLayout(inter)
        self.interactive = QCheckBox("Autoriser les demandes de connexion")
        self.interactive.setChecked(self.s.interactive_enabled)
        il.addWidget(self.interactive)
        box, self.inter_perms = _perm_group("Autorisations proposées par défaut", self.s.interactive_permissions)
        il.addWidget(box)
        lay.addWidget(inter)

        un = QGroupBox("Accès sans surveillance (mot de passe)")
        ul = QVBoxLayout(un)
        self.unattended = QCheckBox("Activer l'accès sans surveillance")
        self.unattended.setChecked(self.s.unattended_enabled)
        ul.addWidget(self.unattended)
        row = QHBoxLayout()
        self.pw_state = _label("", "Small")
        set_pw = _button("Définir le mot de passe…")
        row.addWidget(self.pw_state, 1)
        row.addWidget(set_pw)
        ul.addLayout(row)
        row2 = QHBoxLayout()
        self.totp_state = _label("", "Small")
        self.totp_btn = _button("")
        row2.addWidget(self.totp_state, 1)
        row2.addWidget(self.totp_btn)
        ul.addLayout(row2)
        box2, self.un_perms = _perm_group("Autorisations en accès sans surveillance", self.s.unattended_permissions)
        ul.addWidget(box2)
        lay.addWidget(un)
        set_pw.clicked.connect(self._set_password)
        self.totp_btn.clicked.connect(self._toggle_totp)
        self.unattended.toggled.connect(self._unattended_toggled)
        audit_btn = _button("Journal de sécurité…")
        audit_btn.clicked.connect(lambda: AuditDialog(self.audit, self).exec())
        lay.addWidget(audit_btn, alignment=Qt.AlignLeft)
        lay.addStretch(1)
        self._refresh_security()
        return w

    def _access_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        acl = QGroupBox("Liste blanche")
        al = QVBoxLayout(acl)
        self.acl_enabled = QCheckBox("N'autoriser que les postes de cette liste")
        self.acl_enabled.setChecked(self.s.acl_enabled)
        al.addWidget(self.acl_enabled)
        self.acl = IdListEditor(self.s.acl, "ID autorisé (ex. 123 456 789)")
        al.addWidget(self.acl)
        lay.addWidget(acl)
        blk = QGroupBox("Postes bloqués")
        bl = QVBoxLayout(blk)
        self.blocked = IdListEditor(self.s.blocked, "ID à bloquer")
        bl.addWidget(self.blocked)
        lay.addWidget(blk)
        kh = QGroupBox("Postes distants connus (clés épinglées)")
        kl = QVBoxLayout(kh)
        self.known_list = QListWidget()
        for dev, entry in sorted(self.known.entries().items()):
            item = QListWidgetItem(f"{format_id(dev)}  {entry.get('name', '')}")
            item.setData(Qt.UserRole, dev)
            self.known_list.addItem(item)
        kl.addWidget(self.known_list)
        forget = _button("Oublier la clé sélectionnée")
        forget.clicked.connect(self._forget)
        kl.addWidget(forget, alignment=Qt.AlignLeft)
        lay.addWidget(kh)
        return w

    def _network_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        relay = QGroupBox("Serveur relais BrickMote (auto-hébergé)")
        form = QFormLayout(relay)
        self.relay_host = QLineEdit(self.s.relay_host, placeholderText="relais.mondomaine.fr (vide = réseau local uniquement)")
        self.relay_ports = QLineEdit(",".join(map(str, self.s.relay_ports)))
        self.relay_fp = QLineEdit(self.s.relay_fingerprint, placeholderText="empreinte SHA-256 affichée par le serveur")
        form.addRow("Adresse", self.relay_host)
        form.addRow("Ports TCP", self.relay_ports)
        form.addRow("Empreinte TLS", self.relay_fp)
        form.addRow(_label(
            "Avec une empreinte, le certificat du relais est épinglé (protection contre l'interception). "
            "Sans empreinte, il doit être signé par une autorité reconnue.",
            "Small",
            True,
        ))
        lay.addWidget(relay)
        direct = QGroupBox("Connexions directes")
        dl = QFormLayout(direct)
        self.direct = QCheckBox("Accepter les connexions directes")
        self.direct.setChecked(self.s.direct_enabled)
        self.direct_port = QSpinBox(minimum=1, maximum=65535, value=self.s.direct_port)
        self.discovery = QCheckBox("Découverte sur le réseau local (UDP 50001)")
        self.discovery.setChecked(self.s.discovery_enabled)
        dl.addRow(self.direct)
        dl.addRow("Port TCP", self.direct_port)
        dl.addRow(self.discovery)
        lay.addWidget(direct)
        lay.addStretch(1)
        return w

    # ---- actions
    def _refresh_security(self):
        has_pw = self.s.password_verifier() is not None
        self.pw_state.setText("Mot de passe défini ✓" if has_pw else "Aucun mot de passe défini")
        has_totp = bool(self.s.totp_secret)
        self.totp_state.setText("Double authentification (2FA) : activée ✓" if has_totp else "Double authentification (2FA) : désactivée")
        self.totp_btn.setText("Désactiver la 2FA" if has_totp else "Activer la 2FA…")

    def _unattended_toggled(self, on: bool):
        if on and self.s.password_verifier() is None:
            self._set_password()
            if self.s.password_verifier() is None:
                self.unattended.setChecked(False)

    def _set_password(self):
        dlg = PasswordDialog(self)
        if dlg.exec() and dlg.verifier:
            self.s.password = dlg.verifier.to_dict()
            self._refresh_security()

    def _toggle_totp(self):
        if self.s.totp_secret:
            if QMessageBox.question(self, "BrickMote", "Désactiver la double authentification ?") == QMessageBox.Yes:
                self.s.totp_secret = None
        else:
            dlg = TotpSetupDialog(f"{self.name.text() or 'BrickMote'} ({format_id(self.device_id)})", self)
            if dlg.exec() and dlg.confirmed:
                self.s.totp_secret = dlg.secret
        self._refresh_security()

    def _forget(self):
        for item in self.known_list.selectedItems():
            self.known.forget(item.data(Qt.UserRole))
            self.known_list.takeItem(self.known_list.row(item))

    def _save(self):
        s = self.s
        s.device_name = self.name.text().strip()[:64] or s.device_name
        s.quality = self.quality.currentData()
        s.fps = self.fps.value()
        s.interactive_enabled = self.interactive.isChecked()
        s.interactive_permissions = {k: cb.isChecked() for k, cb in self.inter_perms.items()}
        s.unattended_enabled = self.unattended.isChecked() and s.password_verifier() is not None
        s.unattended_permissions = {k: cb.isChecked() for k, cb in self.un_perms.items()}
        s.acl_enabled = self.acl_enabled.isChecked()
        s.acl = self.acl.entries()
        s.blocked = self.blocked.ids()
        s.relay_host = self.relay_host.text().strip()
        try:
            ports = [int(p) for p in self.relay_ports.text().replace(" ", "").split(",") if p]
        except ValueError:
            ports = []
        s.relay_ports = [p for p in ports if 0 < p < 65536] or [6568, 443, 80]
        s.relay_fingerprint = self.relay_fp.text().strip()
        s.direct_enabled = self.direct.isChecked()
        s.direct_port = self.direct_port.value()
        s.discovery_enabled = self.discovery.isChecked()
        if s.acl_enabled and not s.acl:
            if QMessageBox.question(
                self, "BrickMote", "La liste blanche est vide : plus aucun poste ne pourra se connecter. Continuer ?"
            ) != QMessageBox.Yes:
                return
        if not s.interactive_enabled and not s.unattended_enabled:
            QMessageBox.information(self, "BrickMote", "Toutes les connexions entrantes seront refusées.")
        self.accept()

    def result_settings(self) -> Settings:
        return self.s
