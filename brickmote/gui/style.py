"""Visual identity: colours, fonts, stylesheet, logo, avatars and shadows."""

from __future__ import annotations

import hashlib

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

BRICK = "#D2463C"
BRICK_DARK = "#B23A31"
BRICK_SOFT = "#FCECEA"
INK = "#15171C"
MUTED = "#6B7280"
FAINT = "#9AA1AD"
LINE = "#E7E9EE"
BG = "#F4F5F7"
CARD = "#FFFFFF"
SIDEBAR = "#15171C"
SIDEBAR_HOVER = "#1F2229"
SIDEBAR_ACTIVE = "#2A2E37"
OK = "#16A34A"
OK_SOFT = "#E8F6EE"
WARN = "#D97706"
WARN_SOFT = "#FDF3E4"
DANGER = "#DC2626"
DANGER_SOFT = "#FDECEC"
NEUTRAL_SOFT = "#EEF0F3"

AVATAR_COLORS = ["#D2463C", "#2563EB", "#0F9D7A", "#7C3AED", "#DB2777", "#EA8A0C", "#0891B2", "#4B5563"]

FONT_FAMILIES = [
    "Segoe UI Variable Text",
    "Segoe UI",
    "Inter",
    "SF Pro Text",
    ".AppleSystemUIFont",
    "Helvetica Neue",
    "Ubuntu",
    "Cantarell",
    "Noto Sans",
    "DejaVu Sans",
]

STYLESHEET = f"""
* {{ font-size: 10pt; color: {INK}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {INK}; color: white; border: none; padding: 6px 8px; border-radius: 6px; }}

/* ---------- sidebar ---------- */
#Sidebar {{ background: {SIDEBAR}; }}
#Sidebar QLabel {{ color: #E5E7EB; }}
#Brand {{ font-size: 15pt; font-weight: 700; color: white; }}
#SidebarCaption {{ color: #6B7280; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}
#SidebarFoot {{ color: #8B919C; font-size: 8.5pt; }}
QPushButton#Nav {{
    background: transparent; border: none; border-radius: 9px; padding: 10px 12px;
    text-align: left; color: #A3A9B4; font-weight: 500;
}}
QPushButton#Nav:hover {{ background: {SIDEBAR_HOVER}; color: white; }}
QPushButton#Nav:checked {{ background: {SIDEBAR_ACTIVE}; color: white; font-weight: 600; }}
#NavBadge {{ background: {BRICK}; color: white; border-radius: 9px; font-size: 8pt; font-weight: 700;
            min-width: 18px; max-height: 18px; padding: 0 5px; }}

/* ---------- content ---------- */
#Page {{ background: {BG}; }}
#PageTitle {{ font-size: 18pt; font-weight: 700; }}
#PageSubtitle {{ color: {MUTED}; }}
#SectionTitle {{ font-size: 11.5pt; font-weight: 700; }}
#SectionHint {{ color: {FAINT}; font-size: 9pt; }}
#Card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 14px; }}
#CardTitle {{ font-size: 11pt; font-weight: 700; color: {INK}; }}
#Muted {{ color: {MUTED}; }}
#Small {{ color: {MUTED}; font-size: 8.5pt; }}
#Sas {{ font-size: 22pt; font-weight: 700; color: {INK}; letter-spacing: 3px; }}

/* hero card with this computer's ID */
#Hero {{
    border-radius: 16px; border: none;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #E2543F, stop:0.55 #C93F36, stop:1 #9C2C29);
}}
#Hero QLabel {{ color: white; background: transparent; }}
#HeroCaption {{ font-size: 8.5pt; font-weight: 700; letter-spacing: 1.5px; color: rgba(255,255,255,0.75); }}
#HeroId {{ font-size: 30pt; font-weight: 700; letter-spacing: 2px; }}
#HeroMeta {{ color: white; font-size: 10.5pt; font-weight: 600; }}
#HeroHint {{ color: rgba(255,255,255,0.78); font-size: 9pt; }}
#HeroFp {{ color: rgba(255,255,255,0.62); font-size: 8pt; }}
QPushButton#HeroButton {{
    background: rgba(255,255,255,0.16); color: white; border: 1px solid rgba(255,255,255,0.28);
    border-radius: 9px; padding: 7px 14px; font-weight: 600;
}}
QPushButton#HeroButton:hover {{ background: rgba(255,255,255,0.26); }}
#HeroPill {{ background: rgba(255,255,255,0.16); border-radius: 11px; }}
#HeroPillText {{ font-size: 8.5pt; font-weight: 600; }}

/* connect bar */
#ConnectBar {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 16px; }}
QLineEdit#TargetInput {{ border: none; background: transparent; font-size: 15pt; padding: 6px 2px; }}
QLineEdit#PasswordInput {{ background: {BG}; border: 1px solid {LINE}; border-radius: 9px; padding: 8px 10px; }}
QPushButton#ConnectButton {{
    background: {BRICK}; color: white; border: none; border-radius: 11px;
    padding: 12px 22px; font-size: 11pt; font-weight: 700;
}}
QPushButton#ConnectButton:hover {{ background: {BRICK_DARK}; }}
QPushButton#ConnectButton:pressed {{ background: #9C2C29; }}

/* status rows & pills */
#StatusTitle {{ font-weight: 600; }}
#StatusDetail {{ color: {MUTED}; font-size: 9pt; }}
#IconBubble {{ background: {NEUTRAL_SOFT}; border-radius: 10px; }}
#PillOk {{ background: {OK_SOFT}; color: {OK}; border-radius: 10px; padding: 3px 10px; font-size: 8.5pt; font-weight: 700; }}
#PillWarn {{ background: {WARN_SOFT}; color: {WARN}; border-radius: 10px; padding: 3px 10px; font-size: 8.5pt; font-weight: 700; }}
#PillDanger {{ background: {DANGER_SOFT}; color: {DANGER}; border-radius: 10px; padding: 3px 10px; font-size: 8.5pt; font-weight: 700; }}
#PillNeutral {{ background: {NEUTRAL_SOFT}; color: {MUTED}; border-radius: 10px; padding: 3px 10px; font-size: 8.5pt; font-weight: 700; }}
#Warning {{ background: {WARN_SOFT}; color: #92400E; border-radius: 10px; padding: 10px 12px; font-size: 9pt; }}

/* device cards */
#DeviceCard {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 14px; }}
#DeviceCard:hover {{ border: 1px solid {BRICK}; background: #FFFCFB; }}
#DeviceName {{ font-weight: 700; font-size: 10.5pt; }}
#DeviceId {{ color: {MUTED}; font-size: 9pt; letter-spacing: 0.5px; }}
#DeviceTag {{ color: {FAINT}; font-size: 8.5pt; }}
#Empty {{ color: {FAINT}; background: transparent; border: 2px dashed {LINE}; border-radius: 14px; padding: 22px; }}

/* generic controls */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: white; border: 1px solid #D5D9E0; border-radius: 8px; padding: 6px 8px;
    selection-background-color: {BRICK};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {BRICK}; }}
QPushButton {{
    background: white; border: 1px solid #D5D9E0; border-radius: 8px; padding: 7px 14px; font-weight: 500;
}}
QPushButton:hover {{ border-color: {BRICK}; color: {BRICK}; }}
QPushButton#Primary {{ background: {BRICK}; color: white; border: none; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {BRICK_DARK}; color: white; }}
QPushButton#Danger {{ background: white; color: {BRICK}; border: 1px solid {BRICK}; }}
QPushButton#Danger:hover {{ background: {BRICK_SOFT}; }}
QPushButton#Ghost {{ background: transparent; border: none; color: {MUTED}; padding: 6px 8px; }}
QPushButton#Ghost:hover {{ color: {BRICK}; }}
QPushButton:disabled {{ color: #9CA3AF; background: #F3F4F6; border-color: {LINE}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1.5px solid #C4C9D2; background: white; }}
QCheckBox::indicator:hover {{ border-color: {BRICK}; }}
QCheckBox::indicator:checked {{ background: {BRICK}; border-color: {BRICK}; image: url(__CHECK__); }}
QListWidget {{ background: white; border: 1px solid {LINE}; border-radius: 10px; padding: 4px; }}
QListWidget::item {{ padding: 7px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {BRICK_SOFT}; color: {INK}; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 10px; background: white; top: -1px; }}
QTabBar::tab {{ padding: 9px 16px; background: transparent; border: none; color: {MUTED}; font-weight: 500; }}
QTabBar::tab:selected {{ color: {BRICK}; font-weight: 700; border-bottom: 2px solid {BRICK}; }}
QGroupBox {{ font-weight: 700; border: 1px solid {LINE}; border-radius: 10px; margin-top: 16px; padding: 12px; background: white; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #CBD0D8; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QMenu {{ background: white; border: 1px solid {LINE}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 7px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: {BRICK_SOFT}; color: {INK}; }}

/* ---------- remote session window ---------- */
QToolBar#SessionBar {{ background: {SIDEBAR}; border: none; spacing: 2px; padding: 5px 8px; }}
QToolBar#SessionBar QToolButton {{ color: #D1D5DB; padding: 6px 8px; border-radius: 7px; border: none; }}
QToolBar#SessionBar QToolButton:hover {{ background: {SIDEBAR_ACTIVE}; color: white; }}
QToolBar#SessionBar QToolButton:checked {{ background: rgba(210, 70, 60, 0.30); color: white; }}
QToolBar#SessionBar QToolButton#Disconnect {{ background: {BRICK}; color: white; font-weight: 700; padding: 6px 12px; }}
QToolBar#SessionBar QToolButton#Disconnect:hover {{ background: {BRICK_DARK}; }}
QToolBar#SessionBar QLabel {{ color: #9AA1AD; }}
QToolBar#SessionBar QComboBox {{
    background: {SIDEBAR_ACTIVE}; color: white; border: 1px solid #3A3F4A; border-radius: 7px; padding: 4px 8px;
}}
QToolBar#SessionBar QComboBox QAbstractItemView {{ background: {SIDEBAR_ACTIVE}; color: white; selection-background-color: {BRICK}; }}
QToolBar#SessionBar::separator {{ background: #343842; width: 1px; margin: 6px 6px; }}
QStatusBar {{ background: {SIDEBAR}; color: #A3A9B4; }}
QStatusBar QLabel {{ color: #A3A9B4; padding: 2px 6px; }}
"""


def stylesheet() -> str:
    """The stylesheet, with the checkbox tick rendered to a cached PNG file."""
    import tempfile
    from pathlib import Path

    from .icons import pixmap

    path = Path(tempfile.gettempdir()) / "brickmote-ui" / "check.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap("check", "#FFFFFF", 14, 2.6).save(str(path), "PNG")
        return STYLESHEET.replace("__CHECK__", path.as_posix())
    except OSError:
        return STYLESHEET.replace("image: url(__CHECK__);", "")


def apply_font(app) -> None:
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSizeF(10)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)


def shadow(widget: QWidget, blur: int = 30, dy: int = 6, alpha: int = 22) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(15, 17, 22, alpha))
    widget.setGraphicsEffect(effect)


def initials(name: str) -> str:
    words = [w for w in (name or "").replace("-", " ").replace("_", " ").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def avatar(name: str, seed: str, size: int = 42) -> QPixmap:
    color = AVATAR_COLORS[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(AVATAR_COLORS)]
    dpr = 2.0
    pix = QPixmap(int(size * dpr), int(size * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.3, size * 0.3)
    p.fillPath(path, QColor(color))
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPixelSize(int(size * 0.38))
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, initials(name))
    p.end()
    return pix


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


def logo(size: int) -> QPixmap:
    """Crisp logo for on-screen use (rendered at 2x)."""
    pix = draw_logo(size * 2)
    pix.setDevicePixelRatio(2.0)
    return pix


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(draw_logo(s))
    return icon
