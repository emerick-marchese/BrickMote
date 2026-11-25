"""Reusable widgets for the main window: flow layout, device tiles, status rows."""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ..util import format_id
from .icons import pixmap
from .style import FAINT, MUTED, OK, avatar, shadow


def restyle(widget: QWidget, object_name: str) -> None:
    widget.setObjectName(object_name)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def relative_time(ts: Optional[int]) -> str:
    if not ts:
        return ""
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return "à l'instant"
    if delta < 3600:
        return f"il y a {delta // 60} min"
    if delta < 86400:
        return f"il y a {delta // 3600} h"
    if delta < 2 * 86400:
        return "hier"
    return f"il y a {delta // 86400} j"


class FlowLayout(QLayout):
    """Lays out children left to right, wrapping to new lines."""

    def __init__(self, parent=None, spacing: int = 14):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect: QRect, test: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            nx = x + hint.width() + self._spacing
            if nx - self._spacing > rect.right() + 1 and line > 0:
                x, y = rect.x(), y + line + self._spacing
                nx, line = x + hint.width() + self._spacing, 0
            if not test:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x, line = nx, max(line, hint.height())
        return y + line - rect.y()

    def clear(self) -> None:
        while self._items:
            item = self._items.pop()
            if item.widget():
                item.widget().deleteLater()


class DeviceCard(QFrame):
    activated = Signal(str)

    def __init__(
        self,
        device_id: str,
        name: str,
        tag: str,
        online: bool = False,
        actions: Optional[list[tuple[str, Callable[[], None]]]] = None,
    ):
        super().__init__()
        self.device_id = device_id
        self._actions = actions or []
        self.setObjectName("DeviceCard")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(258, 84)
        self.setToolTip(f"Se connecter à {name or format_id(device_id)}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 12, 12)
        lay.setSpacing(12)
        av = QLabel()
        av.setFixedSize(44, 44)
        if name:
            av.setPixmap(avatar(name, device_id, 44))
        else:  # unknown device (seen on the LAN only): neutral icon instead of initials
            av.setObjectName("IconBubble")
            av.setAlignment(Qt.AlignCenter)
            av.setPixmap(pixmap("devices", "#4B5563", 22))
        lay.addWidget(av)
        text = QVBoxLayout()
        text.setSpacing(1)
        title = QLabel(name or "Poste distant", objectName="DeviceName")
        title.setMaximumWidth(150)
        text.addWidget(title)
        text.addWidget(QLabel(format_id(device_id), objectName="DeviceId"))
        tag_row = QHBoxLayout()
        tag_row.setSpacing(5)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {OK if online else FAINT}; font-size: 7pt;")
        tag_row.addWidget(dot)
        tag_row.addWidget(QLabel(tag, objectName="DeviceTag"))
        tag_row.addStretch(1)
        text.addLayout(tag_row)
        lay.addLayout(text, 1)
        arrow = QLabel()
        arrow.setPixmap(pixmap("arrow", MUTED, 18))
        lay.addWidget(arrow, alignment=Qt.AlignVCenter)
        shadow(self, blur=18, dy=3, alpha=14)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.device_id)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Se connecter", lambda: self.activated.emit(self.device_id))
        for text, callback in self._actions:
            menu.addAction(text, callback)
        menu.exec(event.globalPos())


class StatusRow(QWidget):
    """Icon bubble, title, detail line and a coloured pill."""

    def __init__(self, icon_name: str, title: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(12)
        bubble = QLabel(objectName="IconBubble")
        bubble.setFixedSize(38, 38)
        bubble.setAlignment(Qt.AlignCenter)
        bubble.setPixmap(pixmap(icon_name, "#374151", 19))
        lay.addWidget(bubble, alignment=Qt.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(QLabel(title, objectName="StatusTitle"))
        self.detail = QLabel("", objectName="StatusDetail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(self.detail)
        lay.addLayout(text, 1)
        self.pill = QLabel("", objectName="PillNeutral")
        lay.addWidget(self.pill, alignment=Qt.AlignTop)

    def set(self, pill_text: str, kind: str, detail: str) -> None:
        self.pill.setText(pill_text)
        restyle(self.pill, {"ok": "PillOk", "warn": "PillWarn", "danger": "PillDanger"}.get(kind, "PillNeutral"))
        self.detail.setText(detail)
        self.detail.setToolTip(detail)


class EmptyState(QLabel):
    def __init__(self, text: str):
        super().__init__(text, objectName="Empty")
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)


def section_header(title: str, hint: str = "") -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 8, 0, 0)
    lay.addWidget(QLabel(title, objectName="SectionTitle"))
    lay.addStretch(1)
    if hint:
        lay.addWidget(QLabel(hint, objectName="SectionHint"))
    return w


class Card(QFrame):
    def __init__(self, margins: int = 20, spacing: int = 10, object_name: str = "Card"):
        super().__init__(objectName=object_name)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(margins, margins - 2, margins, margins - 2)
        self.body.setSpacing(spacing)
        shadow(self)
