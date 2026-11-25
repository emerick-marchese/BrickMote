"""Visual identity: colours, stylesheet and the programmatically drawn logo."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

BRICK = "#C8423B"
BRICK_DARK = "#9E2F2A"
INK = "#1E2228"
MUTED = "#6B7280"
BG = "#F3F4F6"
CARD = "#FFFFFF"
OK = "#1E9E5A"
WARN = "#D9822B"

STYLESHEET = f"""
QWidget {{ font-size: 10pt; color: {INK}; }}
QMainWindow, QDialog {{ background: {BG}; }}
#Header {{ background: {INK}; }}
#Header QLabel {{ color: white; }}
#HeaderTitle {{ font-size: 16pt; font-weight: 700; }}
#Card {{ background: {CARD}; border: 1px solid #E2E4E9; border-radius: 10px; }}
#CardTitle {{ font-size: 11pt; font-weight: 700; color: {INK}; }}
#BigId {{ font-size: 26pt; font-weight: 700; color: {BRICK}; letter-spacing: 2px; }}
#Muted {{ color: {MUTED}; }}
#Small {{ color: {MUTED}; font-size: 8.5pt; }}
#Sas {{ font-size: 20pt; font-weight: 700; color: {INK}; letter-spacing: 3px; }}
#Footer {{ color: {MUTED}; font-size: 8.5pt; }}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: white; border: 1px solid #CDD1D8; border-radius: 6px; padding: 6px;
}}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {BRICK}; }}
#TargetInput {{ font-size: 15pt; padding: 8px; }}
QPushButton {{
    background: white; border: 1px solid #CDD1D8; border-radius: 6px; padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {BRICK}; }}
QPushButton#Primary {{ background: {BRICK}; color: white; border: none; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {BRICK_DARK}; }}
QPushButton#Danger {{ background: white; color: {BRICK}; border: 1px solid {BRICK}; }}
QPushButton:disabled {{ color: #9CA3AF; background: #F3F4F6; }}
QListWidget {{ background: white; border: 1px solid #E2E4E9; border-radius: 6px; }}
QListWidget::item {{ padding: 6px; }}
QListWidget::item:selected {{ background: #FBE9E8; color: {INK}; }}
QTabWidget::pane {{ border: 1px solid #E2E4E9; border-radius: 6px; background: white; }}
QTabBar::tab {{ padding: 8px 14px; }}
QTabBar::tab:selected {{ color: {BRICK}; font-weight: 600; }}
QToolBar {{ background: {INK}; border: none; spacing: 4px; padding: 3px; }}
QToolBar QToolButton {{ color: white; padding: 5px 9px; border-radius: 4px; }}
QToolBar QToolButton:hover {{ background: #343A44; }}
QToolBar QToolButton:checked {{ background: {BRICK}; }}
QToolBar QLabel {{ color: #D1D5DB; }}
QToolBar QComboBox {{ padding: 2px 6px; }}
QStatusBar {{ background: {INK}; color: #D1D5DB; }}
QStatusBar QLabel {{ color: #D1D5DB; }}
QGroupBox {{ font-weight: 600; border: 1px solid #E2E4E9; border-radius: 6px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
"""


def draw_logo(size: int = 256) -> QPixmap:
    """A rounded red tile with a small brick-wall pattern."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    p.fillPath(path, QColor(BRICK))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("white"))
    margin = size * 0.17
    rows = 3
    gap = size * 0.045
    row_h = (size - 2 * margin - (rows - 1) * gap) / rows
    inner_w = size - 2 * margin
    for r in range(rows):
        y = margin + r * (row_h + gap)
        if r % 2 == 0:
            bricks = [(0.0, 0.5), (0.5, 1.0)]
        else:
            bricks = [(0.0, 0.25), (0.25, 0.75), (0.75, 1.0)]
        for a, b in bricks:
            x0 = margin + a * inner_w + (gap / 2 if a > 0 else 0)
            x1 = margin + b * inner_w - (gap / 2 if b < 1 else 0)
            p.drawRoundedRect(QRectF(x0, y, x1 - x0, row_h), size * 0.03, size * 0.03)
    p.end()
    return pix


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(draw_logo(s))
    return icon
