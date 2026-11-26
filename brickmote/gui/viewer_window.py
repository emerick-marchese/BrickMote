"""Remote screen window (viewer side)."""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QGuiApplication, QImage, QPainter, QRegion
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..host.capture import PROFILES, get_profile
from ..host.inputs import COORD_MAX
from ..util import format_id
from .bridge import CoreThread, QtSessionEvents
from .dialogs import ChatDialog
from .keymap import translate
from .pointers import PointerSet, bounds, color_of, draw_pointer, label_font, pointer_cursor
from .icons import icon, pixmap
from .style import BRICK, logo as brand_logo

_BUTTONS = {Qt.LeftButton: "left", Qt.RightButton: "right", Qt.MiddleButton: "middle"}


class RemoteScreen(QWidget):
    def __init__(self, window: "ViewerWindow"):
        super().__init__(window)
        self.win = window
        self.image: Optional[QImage] = None
        self.cursor_pos = None
        self.overlay: Optional[tuple[str, str]] = None  # (title, explanation) shown over the image
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setCursor(Qt.CrossCursor)
        self._pending_move: Optional[tuple[int, int]] = None
        self._move_timer = QTimer(self, interval=15, timeout=self._flush_move)
        self._move_timer.start()
        self._pressed: dict[int, tuple] = {}
        self._wheel_acc = [0.0, 0.0]
        # live pointers (the others' and the computer's own cursor), in image fractions
        self.pointers = PointerSet()
        self.live_cursors = False  # the host sends pointers separately (newer hosts)
        self.local_priority = False  # the person at the computer is using its mouse
        self._label_font = label_font(self.font())
        self._pointer_area = QRegion()  # where pointers were last drawn
        self._glide_timer = QTimer(self, interval=8, timeout=self._glide_tick)
        self._glide_timer.setTimerType(Qt.PreciseTimer)

    # ---------------------------------------------------------- painting
    def apply_frame(self, width: int, height: int, cursor, images) -> None:
        resized = self.image is None or self.image.width() != width or self.image.height() != height
        if resized:
            self.image = QImage(width, height, QImage.Format_RGB32)
            self.image.fill(QColor("black"))
        p = QPainter(self.image)
        dirty = QRect()
        for x, y, img in images:
            p.drawImage(x, y, img)
            dirty = dirty.united(QRect(x, y, img.width(), img.height()))
        p.end()
        moved = cursor != self.cursor_pos and not self.live_cursors
        self.cursor_pos = cursor
        if resized or moved:
            self.update()  # full repaint (new size, or the remote cursor moved: older hosts)
        elif not dirty.isEmpty():
            # repaint only the changed area, mapped to widget coordinates
            r = self.target_rect()
            sx, sy = r.width() / width, r.height() / height
            self.update(
                QRect(
                    int(r.x() + dirty.x() * sx) - 2,
                    int(r.y() + dirty.y() * sy) - 2,
                    int(dirty.width() * sx) + 5,
                    int(dirty.height() * sy) + 5,
                )
            )

    def target_rect(self) -> QRectF:
        if self.image is None:
            return QRectF()
        w, h = self.width(), self.height()
        iw, ih = self.image.width(), self.image.height()
        scale = min(w / iw, h / ih)
        if not self.win.fit_to_window:
            scale = min(scale, 1.0)
        tw, th = iw * scale, ih * scale
        return QRectF((w - tw) / 2, (h - th) / 2, tw, th)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#111317"))
        if self.image is None:
            if self.overlay:
                self._paint_overlay(p)
            p.end()
            return
        rect = self.target_rect()
        p.setRenderHint(QPainter.SmoothPixmapTransform, rect.width() < self.image.width())
        p.drawImage(rect, self.image)
        if self.live_cursors:
            area = QRegion()
            for x, y, color, name in self._pointer_positions():
                draw_pointer(p, x, y, None if color is None else color_of(color), name, self._label_font)
                area += bounds(x, y, name, self._label_font).toAlignedRect()
            self._pointer_area = area
        elif self.cursor_pos and self._show_system_cursor():
            sx = rect.width() / self.image.width()
            draw_pointer(p, rect.x() + self.cursor_pos[0] * sx, rect.y() + self.cursor_pos[1] * sx, None, None, self._label_font)
        if self.local_priority:
            self._paint_local_banner(p)
        if self.overlay:
            self._paint_overlay(p)
        p.end()

    LOCAL_TEXT = "La personne devant l'ordinateur utilise la souris : votre contrôle reprend dès qu'elle s'arrête."

    def _local_banner_rect(self) -> QRectF:
        w = min(640.0, self.width() - 24.0)
        return QRectF((self.width() - w) / 2, 12, w, 34)

    def _paint_local_banner(self, p: QPainter):
        box = self._local_banner_rect()
        p.save()
        p.fillRect(box, QColor(17, 19, 23, 235))
        p.fillRect(QRectF(box.x(), box.y(), 4, box.height()), QColor("#F59E0B"))
        f = p.font()
        f.setPointSizeF(9.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("white"))
        text = p.fontMetrics().elidedText(self.LOCAL_TEXT, Qt.ElideRight, int(box.width() - 26))
        p.drawText(box.adjusted(16, 0, -10, 0), Qt.AlignVCenter | Qt.AlignLeft | Qt.TextSingleLine, text)
        p.restore()

    def set_local_priority(self, active: bool) -> None:
        if active != self.local_priority:
            self.local_priority = active
            self.update(self._local_banner_rect().toAlignedRect().adjusted(-2, -2, 2, 2))

    # ------------------------------------------------------------ pointers
    def _show_system_cursor(self) -> bool:
        # when we control the computer, our own mouse cursor is the real one
        return self.win.view_only or not self.win.can_control()

    def set_cursors(self, system, pointers) -> None:
        """Positions from the host (0..COORD_MAX on the monitor we watch)."""
        self.live_cursors = True
        targets = {}
        if system is not None:
            targets["sys"] = (system[0] / COORD_MAX, system[1] / COORD_MAX, None, None)
        for color, x, y, name in pointers:
            targets[color] = (x / COORD_MAX, y / COORD_MAX, color, name)
        self.pointers.update(targets)
        self._glide_tick()
        if self.pointers.animating() and not self._glide_timer.isActive():
            self._glide_timer.start()

    def _pointer_positions(self) -> list:
        rect = self.target_rect()
        if rect.isEmpty():
            return []
        show_system = self._show_system_cursor()
        out = []
        for key, fx, fy, color, name in self.pointers.drawn():
            if key == "sys" and not show_system:
                continue
            out.append((rect.x() + fx * rect.width(), rect.y() + fy * rect.height(), color, name))
        return out

    def _glide_tick(self) -> None:
        """Repaint only around the pointers (where they were + where they are)."""
        area = QRegion(self._pointer_area)
        for x, y, color, name in self._pointer_positions():
            area += bounds(x, y, name, self._label_font).toAlignedRect()
        if not area.isEmpty():
            self.update(area)
        if not self.pointers.animating():
            self._glide_timer.stop()

    def _paint_overlay(self, p: QPainter):
        title, text = self.overlay
        p.fillRect(self.rect(), QColor(10, 12, 15, 200))
        bw = min(520.0, self.width() - 24.0)
        box = QRectF((self.width() - bw) / 2, self.height() / 2 - 70, bw, 140)
        p.fillRect(box, QColor("#181B20"))
        p.setPen(QColor(BRICK))
        p.drawRect(box)
        f = p.font()
        f.setBold(True)
        f.setPointSizeF(12)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(box.adjusted(20, 16, -20, -80), Qt.AlignHCenter | Qt.AlignTop, title)
        f.setBold(False)
        f.setPointSizeF(9.5)
        p.setFont(f)
        p.setPen(QColor("#C3C8D0"))
        p.drawText(box.adjusted(20, 52, -20, -12), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, text)

    # ------------------------------------------------------------- input
    def _norm(self, pos: QPointF) -> Optional[tuple[int, int]]:
        rect = self.target_rect()
        if rect.isEmpty():
            return None
        nx = (pos.x() - rect.x()) / rect.width()
        ny = (pos.y() - rect.y()) / rect.height()
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            return None
        return int(nx * COORD_MAX), int(ny * COORD_MAX)

    def mouseMoveEvent(self, e):
        pos = self._norm(e.position())
        if pos:
            self._pending_move = pos

    def leaveEvent(self, e):
        self._pending_move = None
        self.win.send(lambda s: s.pointer_out())
        super().leaveEvent(e)

    def _flush_move(self):
        if self._pending_move is not None:
            x, y = self._pending_move
            self._pending_move = None
            self.win.send(lambda s: s.mouse("move", x, y))

    def mousePressEvent(self, e):
        self.setFocus()
        self._button(e, "down")

    def mouseReleaseEvent(self, e):
        self._button(e, "up")

    def mouseDoubleClickEvent(self, e):
        self._button(e, "down")

    def _button(self, e, action: str):
        btn = _BUTTONS.get(e.button())
        pos = self._norm(e.position())
        if btn and pos:
            self._pending_move = None
            self.win.send(lambda s: s.mouse(action, pos[0], pos[1], btn))

    def wheelEvent(self, e):
        delta = e.angleDelta()
        self._wheel_acc[0] += delta.x() / 120
        self._wheel_acc[1] += delta.y() / 120
        dx, dy = int(self._wheel_acc[0]), int(self._wheel_acc[1])
        if dx or dy:
            self._wheel_acc[0] -= dx
            self._wheel_acc[1] -= dy
            self.win.send(lambda s: s.wheel(dx, dy))

    def event(self, e):
        # Tab / Shift+Tab must go to the remote computer, not move the focus.
        if e.type() == QEvent.KeyPress and e.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self.keyPressEvent(e)
            return True
        if e.type() == QEvent.KeyRelease and e.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self.keyReleaseEvent(e)
            return True
        if e.type() == QEvent.ShortcutOverride:
            e.accept()  # local shortcuts must not swallow keys meant for the remote
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        if e.isAutoRepeat():
            # injected keys are not repeated by the remote OS: forward the
            # local repeats (held Backspace, arrows...) as extra presses
            held = self._pressed.get(e.key())
            if held is not None:
                key, char = held
                self.win.send(lambda s: s.key(True, key=key, char=char))
            return
        mapped = translate(e.key(), e.text(), e.modifiers())
        if mapped is None:
            return
        self._pressed[e.key()] = mapped
        key, char = mapped
        self.win.send(lambda s: s.key(True, key=key, char=char))

    def keyReleaseEvent(self, e):
        if e.isAutoRepeat():
            return
        mapped = self._pressed.pop(e.key(), None)
        if mapped is None:
            return
        key, char = mapped
        self.win.send(lambda s: s.key(False, key=key, char=char))

    def release_all(self):
        for key, char in list(self._pressed.values()):
            self.win.send(lambda s, k=key, c=char: s.key(False, key=k, char=c))
        self._pressed.clear()

    def focusOutEvent(self, e):
        self.release_all()
        super().focusOutEvent(e)


class ViewerWindow(QMainWindow):
    def __init__(self, core_thread: CoreThread, target: str, parent=None, on_thumbnail=None, profile="balanced"):
        super().__init__(parent)
        self.profile = get_profile(profile)
        self._viewport_sent = None
        self.on_thumbnail = on_thumbnail  # callable(device_id, QImage): keeps a preview for "Récents"
        self.on_wol_learned = None  # callable(device_id, {"mac", "broadcast"})
        self.wake_action = None  # callable(target) -> callable | None (offer Wake-on-LAN on failure)
        self._snapshot_scheduled = False
        self.core_thread = core_thread
        self.target = target
        self.session = None
        self.events = QtSessionEvents()
        self.view_only = False
        self.fit_to_window = True
        self.clip_sync = True
        self._last_remote_clip: Optional[str] = None
        self._chat: Optional[ChatDialog] = None
        self._frames = 0
        self._fps_t = time.monotonic()
        self._last_bytes = 0
        self._closing = False
        self._connect_future = None
        self.setWindowTitle(f"BrickMote - {target}")
        self.resize(1280, 800)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.wait_page = self._make_wait_page()
        self.screen = RemoteScreen(self)
        self.stack.addWidget(self.wait_page)
        self.stack.addWidget(self.screen)
        self._make_toolbar()
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)
        self._stats = QTimer(self, interval=1000, timeout=self._update_stats)
        self._viewport_timer = QTimer(self, singleShot=True, interval=400, timeout=self._send_viewport)

        sig = self.events.sig
        sig.waiting.connect(self._on_waiting)
        sig.connect_result.connect(self._on_connect_result)
        sig.frame.connect(self._on_frame)
        sig.status.connect(self._on_remote_status)
        sig.cursors.connect(self._on_cursors)
        sig.info.connect(self._on_info)
        sig.permissions.connect(self._on_permissions)
        sig.closed.connect(self._on_closed)
        sig.chat.connect(self._on_chat)
        sig.clipboard.connect(self._on_remote_clipboard)
        sig.file_progress.connect(self._on_file_progress)
        sig.file_done.connect(self._on_file_done)
        QGuiApplication.clipboard().dataChanged.connect(self._on_local_clipboard)

    # -------------------------------------------------------------- UI
    def _make_wait_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch(1)
        mark = QLabel()
        mark.setPixmap(brand_logo(72))
        mark.setAlignment(Qt.AlignCenter)
        lay.addWidget(mark)
        self.wait_title = QLabel(f"Connexion à {self.target}…")
        self.wait_title.setObjectName("CardTitle")
        self.wait_title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.wait_title)
        self.wait_hint = QLabel("")
        self.wait_hint.setObjectName("Muted")
        self.wait_hint.setWordWrap(True)
        self.wait_hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.wait_hint)
        lay.addStretch(2)
        return w

    def _make_toolbar(self):
        tb = QToolBar("Session", objectName="SessionBar")
        tb.setMovable(False)
        tb.setIconSize(QSize(19, 19))
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.addToolBar(tb)
        tint, on = "#D1D5DB", "#FFFFFF"

        def action(name, tip, slot=None, checkable=False, checked=False):
            act = QAction(icon(name, tint, 19, active=on), tip, self)
            act.setToolTip(tip)
            act.setCheckable(checkable)
            act.setChecked(checked)
            if slot is not None:
                (act.toggled if checkable else act.triggered).connect(slot)
            tb.addAction(act)
            return act

        stop = QToolButton(objectName="Disconnect")
        stop.setText(" Déconnecter")
        stop.setIcon(icon("power", "#FFFFFF", 17))
        stop.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        stop.setCursor(Qt.PointingHandCursor)
        stop.clicked.connect(self.close)
        tb.addWidget(stop)
        tb.addSeparator()

        mon = QLabel()
        mon.setPixmap(pixmap("devices", "#9AA1AD", 17))
        mon.setToolTip("Écran distant affiché")
        tb.addWidget(mon)
        self.monitor_box = QComboBox()
        self.monitor_box.setMinimumWidth(130)
        self.monitor_box.setToolTip("Écran distant affiché")
        self.monitor_box.activated.connect(
            lambda i: self.send(lambda s, idx=self.monitor_box.itemData(i): s.set_monitor(idx))
        )
        tb.addWidget(self.monitor_box)
        self.quality_box = QComboBox()
        self.quality_box.setToolTip(
            "Profil de performance : plus il est élevé, plus l'image est fluide et nette,\n"
            "mais plus il consomme de débit et de processeur (des deux côtés).\n"
            "La qualité s'adapte aussi automatiquement à la connexion."
        )
        for profile in PROFILES.values():
            self.quality_box.addItem(profile.label, profile.name)
        self.quality_box.setCurrentIndex(max(0, self.quality_box.findData(self.profile.name)))
        self.quality_box.activated.connect(lambda i: self._set_profile(self.quality_box.itemData(i)))
        tb.addWidget(self.quality_box)
        tb.addSeparator()
        self.act_view_only = action("eye", "Lecture seule (ne pas envoyer la souris ni le clavier)", self._toggle_view_only, True)
        self.act_clip = action("clipboard", "Synchroniser le presse-papiers", lambda on: setattr(self, "clip_sync", on), True, True)
        self.act_fit = action("fit", "Ajuster l'image à la fenêtre", self._toggle_fit, True, True)
        tb.addSeparator()
        self.act_file = action("file", "Envoyer un fichier au poste distant", self._send_file)
        action("chat", "Ouvrir le chat", self._open_chat)
        action("keyboard", "Envoyer Ctrl+Alt+Suppr", self._send_cad)
        action("refresh", "Rafraîchir l'image", lambda: self.send(lambda s: s.refresh()))
        action("fullscreen", "Plein écran", self._toggle_fullscreen)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        lock = QLabel()
        lock.setPixmap(pixmap("lock", "#9AA1AD", 15))
        tb.addWidget(lock)
        self.sas_label = QLabel(" Chiffré de bout en bout ")
        self.sas_label.setToolTip("Connexion chiffrée de bout en bout, clé du poste distant vérifiée")
        tb.addWidget(self.sas_label)

    # ------------------------------------------------------- networking
    def send(self, fn) -> None:
        """Run ``fn(session)`` (a coroutine factory) in the network thread."""
        if self.session is not None and not self.session.closed:
            self.core_thread.submit(fn(self.session))

    def can_control(self) -> bool:
        return bool(self.session and self.session.permissions.get("control"))

    def connect_with(self, core, password: Optional[str], remember: bool = False) -> None:
        def waiting(peer, sas):
            self.events.sig.waiting.emit(peer, sas)

        self._connect_future = self.core_thread.submit(
            core.connect(self.target, password, self.events, on_waiting=waiting, remember=remember)
        )
        self._connect_future.add_done_callback(self.events.sig.connect_result.emit)

    def _on_waiting(self, peer, sas: str):
        self.setWindowTitle(f"BrickMote - {peer.name} ({format_id(peer.device_id)})")
        self.wait_title.setText(f"Connexion chiffrée établie avec {peer.name}")
        self.wait_hint.setText("En attente de l'acceptation par l'utilisateur distant.")

    def _on_connect_result(self, fut):
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            if not self._closing:
                self._closing = True
                wake = self.wake_action(self.target) if self.wake_action else None
                if wake is None:
                    QMessageBox.warning(self, "BrickMote", str(exc))
                else:
                    box = QMessageBox(QMessageBox.Warning, "BrickMote", str(exc), parent=self)
                    box.setInformativeText("Le poste est peut-être éteint ou en veille.")
                    wake_btn = box.addButton("Réveiller ce PC", QMessageBox.AcceptRole)
                    box.addButton("Fermer", QMessageBox.RejectRole)
                    box.exec()
                    if box.clickedButton() is wake_btn:
                        wake()
                self.close()
            return
        session = fut.result()
        if self._closing:  # window closed while we were connecting
            self.core_thread.submit(session.close("connexion annulée par le poste client"))
            return
        self.session = session
        peer = session.peer
        self.setWindowTitle(f"BrickMote - {peer.name} ({format_id(peer.device_id)})")
        self.stack.setCurrentWidget(self.screen)
        self.screen.setFocus()
        self._set_profile(self.profile.name)
        self._send_viewport()
        self._stats.start()
        self._on_permissions(session, session.permissions)

    def _on_info(self, session, info: dict):
        if info.get("wol") and self.on_wol_learned is not None:
            try:
                self.on_wol_learned(session.peer.device_id, info["wol"])
            except RuntimeError:
                pass
        self.monitor_box.clear()
        for m in info.get("monitors", []):
            self.monitor_box.addItem(f"{m['index']} ({m['width']}×{m['height']})", m["index"])
        idx = self.monitor_box.findData(info.get("monitor"))
        if idx >= 0:
            self.monitor_box.setCurrentIndex(idx)
        # the performance profile is chosen here, not by the host

    STATUS_TEXT = {
        "locked": (
            "L'ordinateur distant est verrouillé",
            "Windows protège l'écran de verrouillage : BrickMote ne peut ni l'afficher ni le contrôler "
            "pour l'instant. L'image reviendra dès que la session sera déverrouillée sur place.",
        ),
        "unavailable": (
            "Écran distant indisponible",
            "La capture d'écran a échoué sur l'ordinateur distant (écran éteint, session fermée "
            "ou session Wayland sous Linux).",
        ),
    }

    def _on_remote_status(self, session, status: str):
        self.screen.overlay = self.STATUS_TEXT.get(status)
        self.screen.update()

    def _on_frame(self, session, width, height, cursor, images):
        self._frames += 1
        self.screen.apply_frame(width, height, cursor, images)
        if not self._snapshot_scheduled:
            self._snapshot_scheduled = True
            QTimer.singleShot(4000, self._snapshot)

    def _snapshot(self):
        if self.on_thumbnail is not None and self.session is not None and self.screen.image is not None:
            try:
                self.on_thumbnail(self.session.peer.device_id, self.screen.image.copy())
            except RuntimeError:  # main window already gone
                pass

    def _on_permissions(self, session, perms: dict):
        self.act_file.setEnabled(bool(perms.get("files")))
        self.act_clip.setEnabled(bool(perms.get("clipboard")))
        self._apply_cursor_shape()
        self.screen.update()  # the computer's cursor is drawn only when we do not control it
        self._update_stats()

    def _apply_cursor_shape(self):
        color = self.session.info.get("color") if self.session else None
        if color is not None:
            # our pointer, in the color the other participants see it
            self.screen.setCursor(pointer_cursor(color, self.devicePixelRatioF()))
        elif self.can_control() and not self.view_only:
            self.screen.setCursor(Qt.CrossCursor)
        else:
            self.screen.setCursor(Qt.ForbiddenCursor)

    def _on_cursors(self, session, system, pointers, local=False):
        if session is self.session:
            self.screen.set_cursors(system, pointers)
            self.screen.set_local_priority(bool(local))

    def _update_stats(self):
        if self.session is None:
            return
        now = time.monotonic()
        dt = max(0.001, now - self._fps_t)
        fps = self._frames / dt
        received = self.session.channel.bytes_received
        kbps = (received - self._last_bytes) * 8 / 1000 / dt
        self._frames, self._fps_t, self._last_bytes = 0, now, received
        perms = self.session.permissions
        mode = "Lecture seule" if (self.view_only or not perms.get("control")) else "Contrôle"
        self.status_label.setText(
            f"{self.session.peer.name} · {mode} · "
            f"{fps:.0f} img/s · {kbps:,.0f} kbit/s".replace(",", " ")
        )

    def _on_closed(self, session, reason: str):
        self._stats.stop()
        self._snapshot()
        if not self._closing:
            self._closing = True
            QMessageBox.information(self, "BrickMote", f"Session terminée : {reason}")
            self.close()

    # ---------------------------------------------------------- actions
    def _set_profile(self, name: str):
        self.profile = get_profile(name)
        self.screen._move_timer.setInterval(self.profile.mouse_ms)
        self.send(lambda s, n=self.profile.name: s.set_quality(n))

    def _send_viewport(self):
        """Tell the host how wide the image is displayed, so that it never
        sends more pixels than useful (less bandwidth, less latency)."""
        if self.session is None:
            return
        if self.fit_to_window:
            width = int(self.screen.width() * self.screen.devicePixelRatioF())
            width = max(320, (width + 63) // 64 * 64)
        else:
            width = 16384  # 1:1 display: full resolution
        if width != self._viewport_sent:
            self._viewport_sent = width
            self.send(lambda s, w=width: s.set_viewport(w))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_viewport_timer"):
            self._viewport_timer.start()

    def _toggle_view_only(self, on: bool):
        self.view_only = on
        if self.session:
            self.screen.release_all()
            self.session.view_only = on
            self._on_permissions(self.session, self.session.permissions)

    def _toggle_fit(self, on: bool):
        self.fit_to_window = on
        self.screen.update()
        self._send_viewport()

    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _send_cad(self):
        for key in ("ctrl", "alt", "delete"):
            self.send(lambda s, k=key: s.key(True, key=k))
        for key in ("delete", "alt", "ctrl"):
            self.send(lambda s, k=key: s.key(False, key=k))

    def _send_file(self):
        if not self.session:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Envoyer un fichier au poste distant")
        if path:
            self.send(lambda s: s.send_file(path))

    def _open_chat(self):
        if self.session is None:
            return
        if self._chat is None:
            self._chat = ChatDialog(f"Chat - {self.session.peer.name}", lambda t: self.send(lambda s: s.send_chat(t)), self)
        self._chat.show()
        self._chat.raise_()

    def _on_chat(self, session, text: str):
        self._open_chat()
        self._chat.append(session.peer.name, text)

    def _on_remote_clipboard(self, session, text: str):
        if self.clip_sync:
            self._last_remote_clip = text
            QGuiApplication.clipboard().setText(text)

    def _on_local_clipboard(self):
        if not self.clip_sync or self.session is None or not self.session.permissions.get("clipboard"):
            return
        text = QGuiApplication.clipboard().text()
        if text and text != self._last_remote_clip:
            self._last_remote_clip = text
            self.send(lambda s: s.send_clipboard(text))

    def _on_file_progress(self, session, tid, name, done, total, incoming):
        verb = "Réception" if incoming else "Envoi"
        pct = int(done * 100 / total) if total else 100
        self.statusBar().showMessage(f"{verb} de {name} : {pct} %", 3000)

    def _on_file_done(self, session, tid, name, path, error, incoming):
        if error:
            self.statusBar().showMessage(f"Transfert de {name} échoué : {error}", 8000)
        else:
            where = f" → {path}" if incoming else ""
            self.statusBar().showMessage(f"Transfert de {name} terminé{where}", 8000)

    def closeEvent(self, event):
        self._closing = True
        if self._connect_future is not None and not self._connect_future.done():
            self._connect_future.cancel()
        try:
            QGuiApplication.clipboard().dataChanged.disconnect(self._on_local_clipboard)
        except (RuntimeError, TypeError):
            pass
        if self.session is not None and not self.session.closed:
            self._snapshot()
            self.screen.release_all()
            self.core_thread.submit(self.session.close("le poste client a mis fin à la session"))
        super().closeEvent(event)
