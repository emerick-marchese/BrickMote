"""Full-window screen shown while a mandatory update is downloaded and handed
over to the installer. It covers the whole application: nothing else can be
used meanwhile, and closing BrickMote is refused until it restarts."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .style import BRICK, logo


class UpdateOverlay(QWidget):
    dismissed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        card = QFrame(objectName="UpdateCard")
        card.setFixedWidth(520)
        card.setStyleSheet(
            f"#UpdateCard {{ background: #181B20; border: 1px solid {BRICK}; }}"
            "#UpdateCard QLabel { color: #E6E8EB; background: transparent; }"
            "QProgressBar { background: #0F1115; border: 1px solid #2A2F37; height: 14px; text-align: center;"
            " color: white; font-size: 8pt; }"
            f"QProgressBar::chunk {{ background: {BRICK}; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(logo(56))
        mark.setAlignment(Qt.AlignCenter)
        lay.addWidget(mark)
        self.title = QLabel("Mise à jour en cours")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        lay.addWidget(self.title)
        self.warning = QLabel("Ne fermez pas BrickMote : il redémarre tout seul une fois la mise à jour installée.")
        self.warning.setAlignment(Qt.AlignCenter)
        self.warning.setWordWrap(True)
        lay.addWidget(self.warning)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        lay.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet("color: #9AA1AB; font-size: 9pt;")
        lay.addWidget(self.status)
        self.button = QPushButton("Continuer")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(self._dismiss)
        self.button.hide()
        lay.addWidget(self.button, 0, Qt.AlignCenter)
        outer.addWidget(card, 0, Qt.AlignHCenter)
        outer.addStretch(2)
        self.hide()
        parent.installEventFilter(self)

    # ------------------------------------------------------------- states
    def start(self, version: str) -> None:
        self.title.setText(f"Mise à jour vers BrickMote {version}")
        self.warning.setText("Ne fermez pas BrickMote : il redémarre tout seul une fois la mise à jour installée.")
        self.status.setText("Préparation…")
        self.bar.setRange(0, 0)
        self.button.hide()
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.setFocus()

    def progress(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * min(done, total) / total))
            self.bar.setFormat(f"{done * 100 // total} %")
            self.status.setText(f"Téléchargement : {done / 1048576:.1f} / {total / 1048576:.1f} Mo")

    def stage(self, text: str) -> None:
        self.bar.setRange(0, 0)  # indeterminate
        self.status.setText(text)

    def fail(self, text: str) -> None:
        self.title.setText("Mise à jour impossible pour le moment")
        self.warning.setText(text)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("")
        self.status.setText("BrickMote réessaiera automatiquement plus tard.")
        self.button.show()
        self.button.setFocus()

    def _dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()

    # ------------------------------------------------------- behaviour
    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(10, 12, 15, 235))
        p.end()

    # swallow every input aimed at the application below
    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def keyPressEvent(self, event):
        event.accept()
