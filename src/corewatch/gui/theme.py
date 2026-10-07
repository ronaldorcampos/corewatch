"""Light and dark colour themes (zinc neutrals, blue accent) and the matching stylesheet."""

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from corewatch.model import Status


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

    def status(self, status: Status) -> QColor:
        return QColor({Status.OK: self.accent, Status.WARNING: self.warning, Status.CRITICAL: self.critical}[status])

    def value_color(self, status: Status) -> QColor:
        return QColor(self.text) if status is Status.OK else self.status(status)


LIGHT = Theme(
    dark=False,
    window="#f4f4f5",
    surface="#ffffff",
    raised="#fafafa",
    border="#e4e4e7",
    text="#18181b",
    muted="#71717a",
    accent="#2563eb",
    accent_soft="#dbeafe",
    accent_text="#1e3a8a",
    warning="#b45309",
    warning_soft="#fef3c7",
    critical="#dc2626",
    critical_soft="#fee2e2",
)

DARK = Theme(
    dark=True,
    window="#09090b",
    surface="#111113",
    raised="#18181b",
    border="#27272a",
    text="#f4f4f5",
    muted="#a1a1aa",
    accent="#60a5fa",
    accent_soft="#172554",
    accent_text="#dbeafe",
    warning="#fbbf24",
    warning_soft="#3b2a06",
    critical="#f87171",
    critical_soft="#3f1212",
)


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
    return f"""
    QMainWindow, #central {{ background: {t.window}; }}
    QToolBar {{ background: {t.window}; border: none; padding: 8px 10px 4px 10px; spacing: 8px; }}
    QToolBar QLabel#appLogo {{ padding: 0 8px 0 2px; }}
    QLineEdit, QComboBox {{
        background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px;
        padding: 5px 8px; selection-background-color: {t.accent_soft}; selection-color: {t.accent_text};
    }}
    QLineEdit:focus, QComboBox:focus {{ border-color: {t.accent}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {t.raised}; border: 1px solid {t.border}; selection-background-color: {t.accent_soft};
        selection-color: {t.accent_text}; outline: 0;
    }}
    QPushButton, QToolButton {{
        background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px; padding: 5px 12px;
    }}
    QPushButton:hover, QToolButton:hover {{ background: {t.raised}; border-color: {t.muted}; }}
    QPushButton:checked, QToolButton:checked {{
        background: {t.accent_soft}; color: {t.accent_text}; border-color: {t.accent};
    }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    #card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; }}
    #notes {{
        background: {t.warning_soft}; color: {t.warning}; border-radius: 8px; padding: 8px 12px;
    }}
    QTreeView {{
        background: {t.surface}; border: none; border-radius: 10px; outline: 0;
        alternate-background-color: {t.surface};
    }}
    QTreeView::item {{ padding: 4px 2px; border: none; }}
    QTreeView::item:selected {{ background: {t.accent_soft}; color: {t.accent_text}; }}
    QHeaderView {{ background: {t.surface}; border: none; }}
    QHeaderView::section {{
        background: {t.surface}; color: {t.muted}; border: none; border-bottom: 1px solid {t.border};
        padding: 6px 8px; font-size: 12px;
    }}
    QSplitter::handle {{ background: {t.window}; }}
    #listArea, #listContainer {{ background: {t.window}; }}
    QLabel#sectionTitle {{ font-size: 14px; font-weight: 600; }}
    QToolButton#chevron {{ border: none; background: transparent; padding: 0; }}
    QPushButton#segment {{ padding: 2px 8px; min-width: 14px; font-size: 12px; border-radius: 5px; }}
    QStatusBar {{ background: {t.window}; color: {t.muted}; font-size: 12px; }}
    QStatusBar::item {{ border: none; }}
    QLabel#muted, QLabel#statLabel {{ color: {t.muted}; font-size: 12px; }}
    QLabel#statValue {{ font-size: 14px; font-weight: 500; }}
    QLabel#detailTitle {{ font-size: 16px; font-weight: 600; }}
    QLabel#detailValue {{ font-size: 26px; font-weight: 600; }}
    QMenu {{ background: {t.raised}; border: 1px solid {t.border}; border-radius: 8px; padding: 4px; }}
    QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {t.accent_soft}; color: {t.accent_text}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {t.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolTip {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border}; padding: 4px 6px; }}
    """


def apply(app: QApplication, theme: Theme) -> None:
    app.setPalette(palette(theme))
    app.setStyleSheet(stylesheet(theme))
