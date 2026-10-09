"""Light and dark colour themes (a cool cyan accent warming to amber and red), the bundled fonts,
the matching stylesheet, and the cards' corner marks and the window's background grid."""

import contextlib
from dataclasses import dataclass
from importlib.resources import files

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication

from corewatch.model import Status

# Bundled (SIL Open Font License, licences beside them): headings and labels, numbers, body text.
DISPLAY_FONT = "Chakra Petch"
MONO_FONT = "JetBrains Mono"
BODY_FONT = "IBM Plex Sans"
FONT_FILES = ("ChakraPetch-SemiBold.ttf", "ChakraPetch-Bold.ttf", "JetBrainsMono.ttf", "IBMPlexSans.ttf")

GRID_STEP = 32  # px between the background grid's lines
CORNER_MARK = 16  # px length of each arm of a card's corner marks

# Text is sized in pixels for the design, then scaled up for desktops set to larger text than
# the usual 11 pt at 96 dpi (never down, and at most 1.6x, which the list's 30 px rows still fit).
DESIGN_TEXT_PIXELS = 11.0 * 96 / 72
MAX_TEXT_SCALE = 1.6
_text_scale = 1.0


def desktop_text_pixels(desktop: QFont, dpi: float) -> float:
    """How tall the desktop's text is in pixels. A desktop enlarges text either by its point size or,
    on X11 (Large Text, KDE's forced font DPI, Xft.dpi), by the font DPI, so both count."""
    if desktop.pointSizeF() > 0:
        return desktop.pointSizeF() * dpi / 72
    return float(desktop.pixelSize())  # a pixel-sized desktop font, or -1 if unknown


def text_scale_for(pixels: float) -> float:
    if pixels <= 0:
        return 1.0
    return min(max(pixels / DESIGN_TEXT_PIXELS, 1.0), MAX_TEXT_SCALE)


def px(pixels: float) -> int:
    """A design size in pixels, scaled to the desktop's text size."""
    return round(pixels * _text_scale)


@dataclass(frozen=True)
class Theme:
    dark: bool
    window: str
    surface: str
    raised: str
    border: str
    text: str
    muted: str
    accent: str
    accent_soft: str
    accent_text: str
    warning: str
    warning_soft: str
    critical: str
    critical_soft: str
    grid: str  # the background grid's lines, very faint; #AARRGGBB, Qt's order for 8 digits
    edge: str  # cards' outlines, quieter than the controls' borders
    # The core heat map's bands, coolest first: (outline, fill, the core's name on it).
    heat: tuple[tuple[str, str, str], ...]

    def status(self, status: Status) -> QColor:
        return QColor({Status.OK: self.accent, Status.WARNING: self.warning, Status.CRITICAL: self.critical}[status])

    def value_color(self, status: Status) -> QColor:
        return QColor(self.text) if status is Status.OK else self.status(status)


LIGHT = Theme(
    dark=False,
    window="#eef3f8",
    surface="#ffffff",
    raised="#f4f8fb",
    border="#c3d2e1",
    text="#0b1726",
    muted="#4a5d73",
    accent="#0e7490",
    accent_soft="#cff4fc",
    accent_text="#164e63",
    warning="#a14a08",
    warning_soft="#fef3c7",
    critical="#be123c",
    critical_soft="#ffe4e6",
    grid="#0f0e7490",
    edge="#d4dfea",
    heat=(
        ("#2563eb", "#dbeafe", "#1e3a8a"),
        ("#0e7490", "#cff4fc", "#164e63"),
        ("#b45309", "#fef3c7", "#78350f"),
        ("#be123c", "#ffe4e6", "#881337"),
    ),
)

DARK = Theme(
    dark=True,
    window="#05070d",
    surface="#0a0f1a",
    raised="#0e1524",
    border="#1f3a55",
    text="#e6f1ff",
    muted="#8ca3bf",
    accent="#22d3ee",
    accent_soft="#0f2a36",
    accent_text="#a5f3fc",
    warning="#f59e0b",
    warning_soft="#2a1a05",
    critical="#ff5470",
    critical_soft="#2e0a12",
    grid="#0922d3ee",
    edge="#16243a",
    heat=(
        ("#3b82f6", "#0c1a33", "#93c5fd"),
        ("#22d3ee", "#08202a", "#a5f3fc"),
        ("#f59e0b", "#2a1a05", "#fcd34d"),
        ("#ff5470", "#2e0a12", "#fda4af"),
    ),
)


def load_fonts() -> set[str]:
    """Register the bundled fonts with Qt, make the body font the application's default, size
    text to the desktop's setting, and return the families registered. Safe to call more than
    once; a font that fails to load falls back to the desktop's.

    The fonts are handed to Qt as bytes, not paths: Qt reads a font file lazily, and a package
    installed as a zip has no lasting file to read."""
    global _text_scale
    families: set[str] = set()
    for name in FONT_FILES:
        with contextlib.suppress(OSError):
            data = (files("corewatch") / "assets" / "fonts" / name).read_bytes()
            font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
            if font_id >= 0:
                families.update(QFontDatabase.applicationFontFamilies(font_id))
    desktop = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    screen = QGuiApplication.primaryScreen()
    _text_scale = text_scale_for(desktop_text_pixels(desktop, screen.logicalDotsPerInchY() if screen else 96.0))
    body = QFont(BODY_FONT)
    body.setPointSizeF(max(desktop.pointSizeF(), 10.0))
    QApplication.setFont(body)
    return families


def font(family: str, pixels: int, weight: QFont.Weight = QFont.Weight.Normal, spacing: float = 0.0) -> QFont:
    """A bundled font at a pixel size; ``spacing`` is extra letter spacing in px (for caps labels).
    Numbers always get equal-width digits, so columns of them don't jiggle."""
    result = QFont(family)
    result.setPixelSize(px(pixels))
    result.setWeight(weight)
    result.setFeature(QFont.Tag("tnum"), 1)
    if spacing:
        result.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return result


def grid_lines(width: int, height: int, step: int = GRID_STEP) -> tuple[list[int], list[int]]:
    """x positions of the background grid's vertical lines and y positions of its horizontal ones."""
    return list(range(0, max(width, 0), step)), list(range(0, max(height, 0), step))


def paint_grid(painter: QPainter, rect: QRectF, theme: Theme) -> None:
    """Fill with the window colour and rule the faint background grid over it."""
    painter.fillRect(rect, QColor(theme.window))
    painter.setPen(QPen(QColor(theme.grid), 1))
    xs, ys = grid_lines(int(rect.width()), int(rect.height()))
    for x in xs:
        painter.drawLine(QPointF(rect.left() + x + 0.5, rect.top()), QPointF(rect.left() + x + 0.5, rect.bottom()))
    for y in ys:
        painter.drawLine(QPointF(rect.left(), rect.top() + y + 0.5), QPointF(rect.right(), rect.top() + y + 0.5))


def corner_marks(rect: QRectF, arm: float = CORNER_MARK) -> list[tuple[QPointF, QPointF]]:
    """The four short lines that bracket a card's top-left and bottom-right corners."""
    arm = min(arm, rect.width() / 2, rect.height() / 2)
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    return [
        (QPointF(left, top), QPointF(left + arm, top)),
        (QPointF(left, top), QPointF(left, top + arm)),
        (QPointF(right, bottom), QPointF(right - arm, bottom)),
        (QPointF(right, bottom), QPointF(right, bottom - arm)),
    ]


def paint_card(painter: QPainter, rect: QRectF, theme: Theme) -> None:
    """A card: surface fill, a quiet outline, and accent marks on two opposite corners."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
    painter.setPen(QPen(QColor(theme.edge), 1))
    painter.setBrush(QColor(theme.surface))
    painter.drawRect(body)
    painter.setPen(QPen(QColor(theme.accent), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
    for start, end in corner_marks(rect.adjusted(1, 1, -1, -1)):
        painter.drawLine(start, end)
    painter.restore()


_mode = "system"


def set_mode(mode: str) -> None:
    """``light`` or ``dark`` force a theme; ``system`` follows the desktop's preference."""
    global _mode
    _mode = mode


def current_theme() -> Theme:
    if _mode == "dark":
        return DARK
    if _mode == "light":
        return LIGHT
    return DARK if QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark else LIGHT


def palette(theme: Theme) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.window,
        QPalette.ColorRole.Base: theme.surface,
        QPalette.ColorRole.AlternateBase: theme.raised,
        QPalette.ColorRole.Button: theme.surface,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.PlaceholderText: theme.muted,
        QPalette.ColorRole.Highlight: theme.accent_soft,
        QPalette.ColorRole.HighlightedText: theme.accent_text,
        QPalette.ColorRole.ToolTipBase: theme.raised,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.Mid: theme.border,
        QPalette.ColorRole.Link: theme.accent,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(theme.muted))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(theme.muted))
    return p


def stylesheet(t: Theme) -> str:
    display, mono = f'"{DISPLAY_FONT}"', f'"{MONO_FONT}"'
    return f"""
    QMainWindow {{ background: {t.window}; }}
    #central {{ background: transparent; }}
    QToolBar {{ background: transparent; border: none; padding: 12px 12px 6px 12px; spacing: 10px; }}
    QToolBar QLabel#appLogo {{ padding: 0 2px 0 2px; }}
    QLabel#wordmark {{ font-family: {display}; font-size: {px(19)}px; font-weight: 700; letter-spacing: 5px; }}
    QLabel#hostLabel {{
        font-family: {display}; font-size: {px(10)}px; font-weight: 600; letter-spacing: 2px; color: {t.muted};
    }}
    QLabel#liveChip {{
        font-family: {mono}; font-size: {px(12)}px; color: {t.accent_text}; background: {t.accent_soft};
        border: 1px solid {t.border}; padding: 4px 10px;
    }}
    QLabel#liveChip[stalled="true"] {{ color: {t.warning}; background: {t.warning_soft}; border-color: {t.warning}; }}
    QLineEdit, QComboBox {{
        background: {t.surface}; border: 1px solid {t.border}; border-radius: 0;
        padding: 6px 10px; min-height: {px(22)}px;
        selection-background-color: {t.accent_soft}; selection-color: {t.accent_text};
    }}
    QComboBox {{ font-family: {mono}; font-size: {px(12)}px; }}
    QLineEdit:focus, QComboBox:focus {{ border-color: {t.accent}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {t.raised}; border: 1px solid {t.border}; selection-background-color: {t.accent_soft};
        selection-color: {t.accent_text}; outline: 0;
    }}
    QPushButton, QToolButton {{
        background: {t.raised}; border: 1px solid {t.border}; border-radius: 0; padding: 6px 14px;
        font-family: {display}; font-size: {px(12)}px; font-weight: 600; letter-spacing: 1px;
    }}
    QPushButton:hover, QToolButton:hover {{ border-color: {t.accent}; }}
    QPushButton:checked, QToolButton:checked {{
        background: {t.accent_soft}; color: {t.accent}; border-color: {t.accent};
    }}
    QPushButton:disabled {{ color: {t.muted}; border-color: {t.edge}; }}
    QPushButton#unit {{
        font-family: {mono}; font-size: {px(12)}px; letter-spacing: 0; min-width: 22px; padding: 6px 10px;
    }}
    QToolButton#settings {{ padding: 6px; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    #notes {{
        background: {t.warning_soft}; color: {t.warning}; border: 1px solid {t.warning}; padding: 8px 12px;
    }}
    QSplitter::handle {{ background: transparent; }}
    #listArea, #listContainer {{ background: transparent; }}
    QLabel#sectionTitle {{ font-family: {display}; font-size: {px(17)}px; font-weight: 700; letter-spacing: 1px; }}
    QStatusBar {{ background: transparent; color: {t.muted}; font-family: {mono}; font-size: {px(11)}px; }}
    QStatusBar::item {{ border: none; }}
    QLabel#muted {{ color: {t.muted}; font-size: {px(12)}px; }}
    QLabel#statLabel {{
        color: {t.muted}; font-family: {display}; font-size: {px(10)}px; font-weight: 600; letter-spacing: 2px;
    }}
    QLabel#statValue {{ font-family: {mono}; font-size: {px(14)}px; font-weight: 700; }}
    QLabel#detailTitle {{ font-family: {display}; font-size: {px(19)}px; font-weight: 700; letter-spacing: 1px; }}
    QLabel#detailValue {{ font-family: {mono}; font-size: {px(30)}px; font-weight: 700; }}
    QLabel#focusTitle {{ font-family: {display}; font-size: {px(28)}px; font-weight: 700; letter-spacing: 1px; }}
    QLabel#focusDevice {{
        color: {t.accent}; font-family: {display}; font-size: {px(11)}px; font-weight: 600; letter-spacing: 2px;
    }}
    QPushButton#back {{ padding: 10px 16px; }}
    QPushButton#iconButton {{ padding: 6px 8px; }}
    QMenu {{ background: {t.raised}; border: 1px solid {t.border}; padding: 6px 0; }}
    QMenu::item {{ padding: 8px 22px 8px 16px; }}
    QMenu::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
    QMenu::item:disabled {{ color: {t.muted}; }}
    QMenu::separator {{ height: 1px; background: {t.edge}; margin: 6px 14px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t.border}; min-height: {px(30)}px; }}
    QScrollBar::handle:vertical:hover {{ background: {t.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolTip {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border}; padding: 4px 6px; }}
    """


def apply(app: QApplication, theme: Theme) -> None:
    app.setPalette(palette(theme))
    app.setStyleSheet(stylesheet(theme))
