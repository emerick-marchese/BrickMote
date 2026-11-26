"""Device browser: Favourites / Recent / Local network, as large tiles,
small tiles or a list, with a preview of the remote screen."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..util import format_id
from .icons import icon, pixmap
from .style import C, FONT_FAMILIES
from .widgets import ElidedLabel, EmptyState, FlowLayout, relative_time

STAR_COLOR = "#F5B301"
SIZES = {"large": (300, 169), "small": (214, 120)}  # tile width, preview height

ThumbFn = Callable[[str], Optional[QPixmap]]


class Preview(QWidget):
    """Remote screen preview (cover-cropped), or a neutral placeholder."""

    def __init__(self, thumb: Optional[QPixmap], online: bool, compact: bool = False, status: Optional[bool] = None):
        super().__init__()
        self.thumb = thumb
        self.online = online  # seen on the local network
        self.status = True if online else status  # available? (None: unknown)
        self.compact = compact
        self._cache: Optional[QPixmap] = None

    def resizeEvent(self, event):
        self._cache = None
        super().resizeEvent(event)

    def _scaled(self) -> Optional[QPixmap]:
        if self.thumb is None or self.thumb.isNull():
            return None
        if self._cache is None:
            dpr = self.devicePixelRatioF()
            target = QSize(int(self.width() * dpr), int(self.height() * dpr))
            scaled = self.thumb.scaled(target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = (scaled.width() - target.width()) // 2
            y = (scaled.height() - target.height()) // 2
            self._cache = scaled.copy(QRect(x, y, target.width(), target.height()))
            self._cache.setDevicePixelRatio(dpr)
        return self._cache

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect()
        pix = self._scaled()
        if pix is not None:
            p.drawPixmap(0, 0, pix)
        else:
            grad = QLinearGradient(0, 0, 0, r.height())
            grad.setColorAt(0, QColor(C["SIDEBAR_ACTIVE"]))
            grad.setColorAt(1, QColor(C["SIDEBAR"]))
            p.fillRect(r, grad)
            size = 26 if self.compact else 44
            icon_pix = pixmap("devices", "#6B7280", size, 1.5)
            p.drawPixmap((r.width() - size) // 2, (r.height() - size) // 2, icon_pix)
        if self.status is not None:
            color = QColor(STATUS_ON if self.status else STATUS_OFF)
            if self.compact:
                # just the dot, top-left corner
                p.setRenderHint(QPainter.Antialiasing)
                p.setPen(QPen(QColor(0, 0, 0, 170), 2))
                p.setBrush(color)
                p.drawEllipse(QRectF(7, 7, 11, 11))
            else:
                font = QFont()
                font.setFamilies(FONT_FAMILIES)
                font.setPixelSize(11)
                font.setBold(True)
                p.setFont(font)
                label = status_text(self.status, self.online)
                w = p.fontMetrics().horizontalAdvance(label) + 26
                box = QRectF(8, r.height() - 28, w, 20)
                p.fillRect(box, QColor(0, 0, 0, 165))
                p.setPen(color)
                p.drawText(QRectF(box.x() + 7, box.y(), 12, box.height()), Qt.AlignVCenter, "●")
                p.setPen(QColor("white"))
                p.drawText(QRectF(box.x() + 19, box.y(), w - 19, box.height()), Qt.AlignVCenter, label)
        p.end()


STATUS_ON = "#22C55E"
STATUS_OFF = "#EF4444"


def status_text(status: Optional[bool], lan: bool = False) -> str:
    if status is None:
        return ""
    if status:
        return "Disponible · réseau local" if lan else "Disponible"
    return "Indisponible"


class _DeviceItem(QFrame):
    """Common behaviour of tiles and rows."""

    activated = Signal(str)
    wake_requested = Signal(str)
    menu_requested = Signal(str, object)  # device id, global position
    favorite_toggled = Signal(str, bool)

    def __init__(self, device: dict):
        super().__init__()
        self.device = device
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"Se connecter à {device['title']} ({format_id(device['id'])})")

    def _star_button(self) -> Optional[QToolButton]:
        if not self.device["known"]:
            return None
        fav = self.device["favorite"]
        star = QToolButton(objectName="TileStar")
        star.setIcon(icon("star_fill" if fav else "star", STAR_COLOR if fav else "#FFFFFF", 16))
        star.setIconSize(QSize(16, 16))
        star.setToolTip("Retirer des favoris" if fav else "Ajouter aux favoris")
        star.setCursor(Qt.PointingHandCursor)
        star.clicked.connect(lambda: self.favorite_toggled.emit(self.device["id"], not fav))
        return star

    def _wake_button(self) -> Optional[QToolButton]:
        if not self.device.get("wol"):
            return None
        btn = QToolButton(objectName="TileMenu")
        btn.setIcon(icon("power", C["MUTED"], 17))
        btn.setIconSize(QSize(17, 17))
        btn.setToolTip("Réveiller ce PC (Wake-on-LAN)")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self.wake_requested.emit(self.device["id"]))
        return btn

    def _menu_button(self) -> QToolButton:
        btn = QToolButton(objectName="TileMenu")
        btn.setIcon(icon("more_v", C["MUTED"], 18))
        btn.setIconSize(QSize(18, 18))
        btn.setToolTip("Plus d'actions")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self.menu_requested.emit(self.device["id"], btn.mapToGlobal(btn.rect().bottomLeft())))
        return btn

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.device["id"])
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.menu_requested.emit(self.device["id"], event.globalPos())


class DeviceTile(_DeviceItem):
    def __init__(self, device: dict, thumb: Optional[QPixmap], size: str):
        super().__init__(device)
        self.setObjectName("Tile")
        width, preview_h = SIZES[size]
        compact = size == "small"
        footer_h = 46 if compact else 54
        self.setFixedSize(width, preview_h + footer_h + 2)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self.preview = Preview(thumb, device["online"], compact, device.get("status"))
        self.preview.setFixedHeight(preview_h)
        lay.addWidget(self.preview)
        star = self._star_button()
        if star is not None:
            star.setParent(self.preview)  # reparenting hides a widget: show it again
            star.move(width - 36, 8)
            star.show()
            star.raise_()
        footer = QWidget(objectName="TileFooter")
        footer.setFixedHeight(footer_h)
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(12, 6, 4, 6)
        fl.setSpacing(10)
        mon = QLabel()
        mon.setPixmap(pixmap("devices", C["MUTED"], 18 if compact else 20))
        fl.addWidget(mon)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(ElidedLabel(device["title"], "TileName"))
        if not compact:
            detail = format_id(device["id"]) + (f"  ·  {device['subtitle']}" if device["subtitle"] else "")
            text.addWidget(ElidedLabel(detail, "TileId"))
        else:
            text.addWidget(ElidedLabel(format_id(device["id"]), "TileId"))
        fl.addLayout(text, 1)
        wake = self._wake_button()
        if wake is not None:
            fl.addWidget(wake)
        fl.addWidget(self._menu_button())
        lay.addWidget(footer)


class DeviceRow(_DeviceItem):
    def __init__(self, device: dict, thumb: Optional[QPixmap]):
        super().__init__(device)
        self.setObjectName("DeviceRow")
        self.setFixedHeight(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 7, 8, 7)
        lay.setSpacing(14)
        prev = Preview(thumb, False, compact=True)
        prev.setFixedSize(82, 46)
        lay.addWidget(prev)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(ElidedLabel(device["title"], "TileName"))
        text.addWidget(ElidedLabel(format_id(device["id"]), "TileId"))
        lay.addLayout(text, 3)
        seen = QLabel(device["seen"], objectName="TileId")
        lay.addWidget(seen, 2)
        state = True if device["online"] else device.get("status")
        text = status_text(state, device["online"])
        status = QLabel(f"● {text}" if text else "", objectName="TileId")
        status.setStyleSheet(f"color: {STATUS_ON if state else STATUS_OFF};")
        status.setMinimumWidth(110)
        lay.addWidget(status)
        star = self._star_button()
        if star is not None:
            star.setObjectName("RowStar")
            star.setIcon(
                icon("star_fill" if device["favorite"] else "star", STAR_COLOR if device["favorite"] else C["MUTED"], 16)
            )
            lay.addWidget(star)
        wake = self._wake_button()
        if wake is not None:
            lay.addWidget(wake)
        lay.addWidget(self._menu_button())


class DeviceBrowser(QWidget):
    connect_requested = Signal(str)
    favorite_toggled = Signal(str, bool)
    rename_requested = Signal(str)
    forget_requested = Signal(str)
    view_changed = Signal(str)
    wake_requested = Signal(str)
    wake_settings_requested = Signal(str)
    forget_password_requested = Signal(str)

    TABS = (("recent", "Récents"), ("favorites", "Favoris"), ("lan", "Réseau local"))
    EMPTY = {
        "recent": "Aucune connexion récente.\nSaisissez l'ID d'un poste dans la barre ci-dessus pour commencer.",
        "favorites": "Aucun favori.\nCliquez sur l'étoile d'un poste pour l'épingler ici.",
        "lan": "Aucun poste BrickMote détecté sur ce réseau local.",
    }

    def __init__(self, view: str = "large"):
        super().__init__()
        self.view = view if view in ("large", "small", "list") else "large"
        self.tab = "recent"
        self._known: dict = {}
        self._lan: dict = {}
        self._presence: dict = {}
        self._thumb: ThumbFn = lambda _id: None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QHBoxLayout()
        bar.setSpacing(0)
        self.tab_group = QButtonGroup(self)
        self.tab_buttons = {}
        for key, label in self.TABS:
            b = QPushButton(label, objectName="Tab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.set_tab(k))
            self.tab_group.addButton(b)
            self.tab_buttons[key] = b
            bar.addWidget(b)
        self.tab_buttons[self.tab].setChecked(True)
        bar.addStretch(1)
        self.view_group = QButtonGroup(self)
        for key, icon_name, tip in (
            ("large", "grid_large", "Grandes vignettes"),
            ("small", "grid_small", "Petites vignettes"),
            ("list", "list", "Liste"),
        ):
            b = QToolButton(objectName="ViewButton")
            b.setIcon(icon(icon_name, C["FAINT"], 18, active=C["INK"]))
            b.setIconSize(QSize(18, 18))
            b.setCheckable(True)
            b.setChecked(key == self.view)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.set_view(k))
            self.view_group.addButton(b)
            bar.addWidget(b)
        lay.addLayout(bar)
        line = QFrame(objectName="TabLine")
        line.setFixedHeight(1)
        lay.addWidget(line)
        lay.addSpacing(16)

        self.flow_host = QWidget()
        self.flow = FlowLayout(self.flow_host, spacing=14)
        self.flow_host.setLayout(self.flow)
        lay.addWidget(self.flow_host)
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(8)
        lay.addWidget(self.list_host)
        self.empty = EmptyState("")
        lay.addWidget(self.empty)

    # -------------------------------------------------------------- state
    def set_tab(self, tab: str) -> None:
        self.tab = tab
        self.tab_buttons[tab].setChecked(True)
        self._rebuild()

    def set_view(self, view: str) -> None:
        if view != self.view:
            self.view = view
            self.view_changed.emit(view)
            self._rebuild()

    def set_data(
        self, known: dict, lan: dict, thumb: ThumbFn, saved: Optional[set] = None, presence: Optional[dict] = None
    ) -> None:
        self._known, self._lan, self._thumb = known, lan, thumb
        self._presence = dict(presence or {})
        self._saved = set(saved or ())
        counts = {
            "recent": len(known),
            "favorites": sum(1 for e in known.values() if e.get("favorite")),
            "lan": len(lan),
        }
        for key, label in self.TABS:
            self.tab_buttons[key].setText(f"{label}  {counts[key]}" if counts[key] else label)
        self._rebuild()

    def _devices(self) -> list[dict]:
        known, lan = self._known, self._lan

        def make(dev: str) -> dict:
            entry = known.get(dev, {})
            online = dev in lan
            title = entry.get("alias") or entry.get("name") or "Poste distant"
            seen = f"vu {relative_time(entry.get('last_seen'))}" if entry.get("last_seen") else ""
            if self.tab == "lan" and online:
                subtitle = lan[dev]["ip"]
            else:
                subtitle = seen or (lan[dev]["ip"] if online else "")
            return {
                "seen": seen.capitalize() if seen else (lan[dev]["ip"] if online else ""),
                "id": dev,
                "title": title,
                "subtitle": subtitle,
                "online": online,
                "status": True if online else self._presence.get(dev),
                "known": dev in known,
                "favorite": bool(entry.get("favorite")),
                "wol": bool(isinstance(entry.get("wol"), dict) and entry["wol"].get("mac")),
            }

        by_recent = sorted(known, key=lambda d: -int(known[d].get("last_seen", 0) or 0))
        if self.tab == "favorites":
            ids = [d for d in by_recent if known[d].get("favorite")]
        elif self.tab == "lan":
            ids = sorted(lan)
        else:
            ids = by_recent
        return [make(d) for d in ids]

    def _clear(self) -> None:
        self.flow.clear()
        while self.list_lay.count():
            item = self.list_lay.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()

    def _rebuild(self) -> None:
        self._clear()
        devices = self._devices()
        for dev in devices:
            thumb = self._thumb(dev["id"])
            if self.view == "list":
                item = DeviceRow(dev, thumb)
                self.list_lay.addWidget(item)
            else:
                item = DeviceTile(dev, thumb, self.view)
                self.flow.addWidget(item)
            item.activated.connect(self.connect_requested)
            item.favorite_toggled.connect(self.favorite_toggled)
            item.wake_requested.connect(self.wake_requested)
            item.menu_requested.connect(self._menu)
        self.flow_host.setVisible(bool(devices) and self.view != "list")
        self.list_host.setVisible(bool(devices) and self.view == "list")
        self.empty.setText(self.EMPTY[self.tab])
        self.empty.setVisible(not devices)
        for b in self.view_group.buttons():
            b.setChecked(False)
        idx = ("large", "small", "list").index(self.view)
        self.view_group.buttons()[idx].setChecked(True)

    def _menu(self, dev: str, pos) -> None:
        entry = self._known.get(dev)
        menu = QMenu(self)
        menu.addAction(icon("arrow", C["INK"], 16), "Se connecter", lambda: self.connect_requested.emit(dev))
        if entry is not None:
            fav = bool(entry.get("favorite"))
            menu.addAction(
                icon("star_fill" if fav else "star", STAR_COLOR, 16),
                "Retirer des favoris" if fav else "Ajouter aux favoris",
                lambda: self.favorite_toggled.emit(dev, not fav),
            )
            menu.addAction(icon("edit", C["INK"], 16), "Renommer…", lambda: self.rename_requested.emit(dev))
            menu.addSeparator()
            has_wol = isinstance(entry.get("wol"), dict) and entry["wol"].get("mac")
            if has_wol:
                menu.addAction(icon("power", C["INK"], 16), "Réveiller (Wake-on-LAN)", lambda: self.wake_requested.emit(dev))
            menu.addAction(
                icon("settings", C["INK"], 16),
                "Réglages du réveil…" if has_wol else "Configurer le réveil (Wake-on-LAN)…",
                lambda: self.wake_settings_requested.emit(dev),
            )
        menu.addAction(icon("copy", C["INK"], 16), "Copier l'ID", lambda: _copy(dev))
        if dev in getattr(self, "_saved", set()):
            menu.addAction(
                icon("lock", C["INK"], 16),
                "Oublier le mot de passe enregistré",
                lambda: self.forget_password_requested.emit(dev),
            )
        if entry is not None:
            menu.addSeparator()
            menu.addAction(icon("trash", C["DANGER"], 16), "Oublier ce poste", lambda: self.forget_requested.emit(dev))
        menu.exec(pos)


def _copy(text: str) -> None:
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setText(text)
