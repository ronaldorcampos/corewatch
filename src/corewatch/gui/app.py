"""Desktop window and tray icon built on Qt (PySide6)."""

import contextlib
import json
import math
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Literal

from PySide6.QtCore import (
    QByteArray,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSettings,
    QSignalBlocker,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QCursor,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QShortcut,
    QTransform,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from corewatch import autostart
from corewatch.cli import MAX_INTERVAL, MIN_INTERVAL
from corewatch.gui import single
from corewatch.gui import theme as themes
from corewatch.gui.cards import CardDeck, ViewData, card_rank, view_for
from corewatch.gui.focus import MIN_CORES, FocusView, related_rows
from corewatch.gui.layout import GAUGES, CardFrame, FlowRow, GaugePanel, Layout, LayoutBar
from corewatch.gui.overview import HEAT_EDGES, GaugeStrip, core_groups, gauge_specs
from corewatch.gui.sensors import CategorySection, SensorGrid
from corewatch.gui.widgets import DetailPanel, chevron_icon, expand_icon, pencil_icon
from corewatch.model import Kind, Row, Status, format_short, format_value
from corewatch.monitor import Collected, Monitor, gather_fans, gather_storage, group_rows, headline
from corewatch.sources import default_sources

# How long closing waits for a reading in flight before giving up on it.
SHUTDOWN_WAIT_MS = 3000

# Lines at the top of a pinned icon's menu: group, name, min, max, average.
TRAY_INFO_LINES = 5
# The logo's size in the toolbar, beside the app's name.
LOGO_SIZE = 34
# Key of corewatch's own tray icon (its logo), which carries the window, reset and quit
# actions. Pinned sensors get number icons of their own beside it.
APP_TRAY = ""

INTERVAL_PRESETS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

THEMES = {
    "system": Qt.ColorScheme.Unknown,
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
}


def app_icon() -> QIcon:
    """corewatch's own icon, bundled with the package so it shows even when it isn't installed
    into the desktop's icon theme."""
    return QIcon(str(files("corewatch") / "assets" / "corewatch.svg"))


# Desktops whose top panel is dark whatever the light/dark setting.
DARK_PANEL_DESKTOPS = ("gnome", "unity", "pantheon")
TRAY_ICON_SIZES = (16, 22, 24, 32, 48, 64)


# The settings button's icon: three sliders, drawn square to match the cards' corner marks.
SETTINGS_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="COLOR" '
    'stroke-width="1.5" stroke-linecap="square"><path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/>'
    '<rect x="13" y="4" width="4" height="4"/><rect x="7" y="10" width="4" height="4"/>'
    '<rect x="15" y="16" width="4" height="4"/></svg>'
)


def settings_icon(color: str) -> QIcon:
    renderer = QSvgRenderer(QByteArray(SETTINGS_SVG.replace("COLOR", color).encode()))
    icon = QIcon()
    for size in (20, 40):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def live_text(interval: float, stalled: bool = False) -> str:
    """The toolbar chip: readings arriving at the interval, or waiting on a slow one."""
    return "●  WAITING FOR A READING" if stalled else f"●  LIVE · {interval:g} s"


def short_host(name: str) -> str:
    """The computer's name without its domain, so a long one can't crowd the toolbar."""
    return name.split(".")[0] or name


class GridBackground(QWidget):
    """The window's backdrop: its colour with a faint square grid ruled over it."""

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        themes.paint_grid(painter, QRectF(self.rect()), themes.current_theme())
        painter.end()


def panel_is_light(desktop: str, scheme: Qt.ColorScheme) -> bool:
    """Best guess at the tray's background, which no API reports. GNOME's panel is black even
    in light mode; elsewhere (KDE, Xfce, ...) the panel usually follows the system scheme."""
    if any(name in desktop.lower() for name in DARK_PANEL_DESKTOPS):
        return False
    return scheme == Qt.ColorScheme.Light


def tray_icon(light_panel: bool = False) -> QIcon:
    """The logo in one colour for the tray, like the desktop's own icons there: white on a dark
    panel, near-black on a light one."""
    svg = (files("corewatch") / "assets" / "corewatch-symbolic.svg").read_text()
    if light_panel:
        svg = svg.replace("#ffffff", "#18181b")
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    icon = QIcon()
    for size in TRAY_ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


# A pinned sensor's tray icon: its number in the panel's own colour over a rule that says how
# it's doing. The rule turns amber once it's warm and the number turns red at critical.
TrayLevel = Literal["normal", "warm", "critical", "none"]
TRAY_RULES: dict[TrayLevel, str] = {"normal": "#22D3EE", "warm": "#F59E0B", "critical": "#FF5470", "none": "#3A4A60"}
TRAY_CRITICAL_TEXT = "#FF5470"
TRAY_SILENT_TEXT = {False: "#8CA3BF", True: "#5E7896"}  # a sensor that stopped reporting, by panel
TRAY_TEXT = {False: "#FFFFFF", True: "#18181B"}  # on a dark panel, on a light one
WARM_CELSIUS = HEAT_EDGES[1]  # where the heat map's warm band starts


def tray_level(row: Row | None) -> TrayLevel:
    """``normal``, ``warm``, ``critical``, or ``none`` for a sensor with no reading. A temperature
    is warm from the heat map's warm band on, before any limit; anything past a limit is warm, and
    so is a stalled fan."""
    reading = row.reading if row is not None else None
    if reading is None or reading.value is None:
        return "none"
    if reading.status is Status.CRITICAL:
        return "critical"
    if reading.status is Status.WARNING:
        return "warm"
    if reading.kind is Kind.TEMPERATURE and reading.value >= WARM_CELSIUS:
        return "warm"
    return "normal"


def number_icon(text: str, level: TrayLevel, light_panel: bool = False) -> QIcon:
    """A tray icon showing a number, like Core Temp does, with a rule under it coloured by
    ``level`` (see ``tray_level``). The digits get a faint outline in the opposite colour, so
    they still read if the guess at the panel's colour (``panel_is_light``) is wrong."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rule = size // 12
    inset = size // 8
    painter.fillRect(inset, size - rule, size - 2 * inset, rule, QColor(TRAY_RULES[level]))
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)  # not the app's bundled fonts
    font.setBold(True)
    font.setPixelSize({1: 44, 2: 44, 3: 32, 4: 24}.get(len(text), 19))
    painter.setFont(font)
    if level == "critical":
        color = TRAY_CRITICAL_TEXT
    elif level == "none":
        color = TRAY_SILENT_TEXT[light_panel]
    else:
        color = TRAY_TEXT[light_panel]
    path = QPainterPath()
    path.addText(0, 0, font, text)
    halo_width = 4
    room = size - halo_width - 2  # the outline reaches half its width past the digits, plus 1 px clear
    if path.boundingRect().width() > room:  # a wide font's three digits
        scale = room / path.boundingRect().width()
        path = QTransform.fromScale(scale, scale).map(path)
    area = QRectF(0, 0, size, size - rule - 2)
    path.translate(area.center() - path.boundingRect().center())
    halo = QColor(TRAY_TEXT[not light_panel])
    halo.setAlpha(150)
    painter.strokePath(
        path, QPen(halo, halo_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    )
    painter.fillPath(path, QColor(color))
    painter.end()
    return QIcon(pixmap)


def gather_cards(rows: list[Row]) -> list[Row]:
    """Every fan into the Fans card and every drive into the Storage card."""
    return gather_storage(gather_fans(rows))


def matches(text: str, *fields: str) -> bool:
    needle = text.strip().lower()
    return not needle or any(needle in field.lower() for field in fields)


class Sampler(QObject):
    """Lives on a worker thread and reads the sensors when asked."""

    collected = Signal(object)

    def __init__(self, monitor: Monitor) -> None:
        super().__init__()
        self.monitor = monitor

    @Slot()
    def run(self) -> None:
        self.collected.emit(self.monitor.collect())


class MainWindow(QMainWindow):
    sample_requested = Signal()

    def __init__(
        self,
        monitor: Monitor,
        settings: QSettings,
        interval: float | None = None,
        fahrenheit: bool | None = None,
        autostart_file: Path | None = None,
    ) -> None:
        super().__init__()
        self.autostart_file = autostart_file or autostart.autostart_path()
        self.monitor = monitor
        self.settings = settings
        stored_interval = settings.value("interval", 1.0)
        try:
            default_interval = float(str(stored_interval))
        except ValueError:
            default_interval = 1.0
        if not math.isfinite(default_interval):
            default_interval = 1.0
        self.interval = min(max(interval or default_interval, MIN_INTERVAL), MAX_INTERVAL)
        self.fahrenheit = bool(settings.value("fahrenheit", False, type=bool)) if fahrenheit is None else fahrenheit
        self.theme = str(settings.value("theme", "system"))
        if self.theme not in THEMES:
            self.theme = "system"
        self.close_to_tray = bool(settings.value("close_to_tray", False, type=bool))
        self.show_unused = bool(settings.value("show_unused", False, type=bool))
        self.start_minimized = bool(settings.value("start_minimized", False, type=bool))
        self.show_min_max = bool(settings.value("show_min_max", True, type=bool))
        self.show_host = bool(settings.value("show_host", True, type=bool))
        # Cards showing their full list as well as their compact view ("All sensors").
        self.expanded: set[str] = set(self._load_list("expanded_cards"))
        self.selected_key: str | None = str(settings.value("selected", "")) or None
        self.focus_key: str | None = None  # the sensor in the focus view, while it's open
        self.pinned: list[str] = self._load_list("pinned")
        self.names: dict[str, str] = self._load_names()
        # Your arrangement of the overview's panels, and whether it's being edited.
        self.panel_layout = Layout.load(settings.value("layout"))
        self.editing = False
        self._panels: list[str] = []  # every panel, gauges included, in the order shown
        self._deck_key: tuple[tuple[str, bool], ...] | None = None
        self._hidden_shown: tuple[str, ...] | None = None

        self.rows: dict[str, Row] = {}
        self.ordered: list[Row] = []
        self.all_rows: list[Row] = []
        self.plain_ordered: list[Row] = []
        self._devices: list[str] = []
        self.sections: dict[str, CategorySection] = {}
        self.gap = 5.0
        self._interval_set_at = 0.0
        self.raw_rows: list[Row] = []
        self._tray_factory: Callable[[], QSystemTrayIcon] | None = None
        self._tray_menus: dict[str, QMenu] = {}
        self._tray_info: dict[str, list[QAction]] = {}
        self.trays: dict[str, QSystemTrayIcon] = {}
        self._quitting = False
        self.light_panel = False  # set by run_gui from the desktop, before any theme override

        self.setWindowTitle("corewatch")
        self.setWindowIcon(app_icon())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._request_sample)
        self.timer.timeout.connect(self._update_live)
        self._sampling = False
        self._stopped = False
        self.worker_stuck = False
        self._tray_states: dict[str, tuple[str, TrayLevel, str]] = {}
        self._build_ui()
        self.apply_theme(self.theme)
        geometry = settings.value("geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        else:
            self.resize(1180, 900)
        QApplication.styleHints().colorSchemeChanged.connect(lambda _: self._restyle())
        self.set_interval(self.interval, remember=False)  # a -i from the command line is for this run only
        self.refresh()
        # Later samples are read on a worker thread so a slow driver never freezes the window.
        self._thread = QThread(self)
        self._sampler = Sampler(monitor)
        self._sampler.moveToThread(self._thread)
        self.sample_requested.connect(self._sampler.run)
        self._sampler.collected.connect(self._on_collected)
        self._thread.start()

    # ----- layout -------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = self.toolbar = QToolBar("Controls", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.toggleViewAction().setVisible(False)
        self.addToolBar(toolbar)

        self.logo = QLabel()
        self.logo.setObjectName("appLogo")
        self.logo.setPixmap(app_icon().pixmap(LOGO_SIZE, LOGO_SIZE))
        self.logo.setToolTip("corewatch")
        self.logo.setAccessibleName("corewatch")
        toolbar.addWidget(self.logo)
        brand = self.brand = QWidget()
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(4, 0, 6, 0)
        brand_layout.setSpacing(0)
        wordmark = QLabel("COREWATCH")
        wordmark.setObjectName("wordmark")
        brand_layout.addWidget(wordmark)
        self.host_label = QLabel(f"HOST {short_host(socket.gethostname()).upper()}")
        self.host_label.setObjectName("hostLabel")
        self.host_label.setVisible(self.show_host)
        brand_layout.addWidget(self.host_label)
        self.brand_action = toolbar.addWidget(brand)
        self.live_chip = QLabel(live_text(self.interval))
        self.live_chip.setObjectName("liveChip")
        self.live_chip.setToolTip("Readings refresh at this interval")
        self.live_chip_action = toolbar.addWidget(self.live_chip)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter sensors  (Ctrl+F)")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMinimumWidth(240)
        self.filter.textChanged.connect(lambda _: self._apply_filter())
        self.filter.textChanged.connect(lambda _: self.close_focus())  # a search is for every sensor
        toolbar.addWidget(self.filter)
        focus_filter = QAction(self)
        focus_filter.setShortcut("Ctrl+F")
        focus_filter.triggered.connect(lambda: self.filter.setFocus())
        self.addAction(focus_filter)

        every = QLabel("Every")
        every.setObjectName("muted")
        toolbar.addWidget(every)
        self.interval_box = QComboBox()
        self.interval_box.setToolTip("How often the readings refresh")
        for seconds in sorted({*INTERVAL_PRESETS, self.interval}):
            self.interval_box.addItem(f"{seconds:g} s", seconds)
        self.interval_box.setCurrentIndex(self.interval_box.findData(self.interval))
        self.interval_box.currentIndexChanged.connect(
            lambda index: self.set_interval(float(self.interval_box.itemData(index)), remember=True)
        )
        toolbar.addWidget(self.interval_box)

        self.unit_buttons = QButtonGroup(self)
        self.celsius_button = QPushButton("°C")
        self.fahrenheit_button = QPushButton("°F")
        for button, is_f in ((self.celsius_button, False), (self.fahrenheit_button, True)):
            button.setObjectName("unit")
            button.setCheckable(True)
            button.setChecked(is_f == self.fahrenheit)
            button.setToolTip("Show temperatures in " + ("Fahrenheit" if is_f else "Celsius"))
            button.clicked.connect(lambda _=False, value=is_f: self.set_fahrenheit(value))
            self.unit_buttons.addButton(button)
            toolbar.addWidget(button)

        self._make_option_actions()
        # Every option, Reset min/max first, behind one button at the far right; the same list
        # as the tray icon's menu.
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settings")
        self.settings_button.setToolTip("Settings")
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.settings_button)
        self._add_display_options(menu)
        self.edit_layout_action = QAction("Edit layout…", self)
        self.edit_layout_action.setToolTip("Move, resize or hide the overview's panels")
        self.edit_layout_action.triggered.connect(lambda: self.set_editing(True))
        menu.addAction(self.edit_layout_action)  # the window's own: not in the tray's menu
        menu.addSeparator()
        self._add_options(menu)
        self.addAction(self.quit_action)  # its shortcut works anywhere in the window
        # Ctrl+R is a window shortcut rather than the action's, so the tray menu, where it can't
        # work, doesn't show it.
        self.reset_shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        self.reset_shortcut.activated.connect(self.reset_action.trigger)
        self.settings_button.setMenu(menu)
        toolbar.addWidget(self.settings_button)

        central = GridBackground()
        central.setObjectName("central")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 4, 12, 0)
        layout.setSpacing(10)

        self.notes = QLabel()
        self.notes.setObjectName("notes")
        self.notes.setWordWrap(True)
        self.notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes.setVisible(False)
        layout.addWidget(self.notes)

        # Over the overview while its layout is being edited.
        self.layout_banner = QFrame()
        self.layout_banner.setObjectName("layoutBanner")
        banner = QHBoxLayout(self.layout_banner)
        banner.setContentsMargins(16, 10, 12, 10)
        banner.setSpacing(14)
        words = QVBoxLayout()
        words.setSpacing(2)
        heading = QLabel("EDITING LAYOUT")
        heading.setObjectName("bannerTitle")
        words.addWidget(heading)
        hint = QLabel("Drag a panel by its handle, or use its arrows. Choose half or full width, or hide it.")
        hint.setWordWrap(True)
        words.addWidget(hint)
        banner.addLayout(words, 1)
        self.reset_layout_button = QPushButton("Reset layout")
        self.reset_layout_button.setToolTip("Every panel back in its usual place and width, none hidden")
        self.reset_layout_button.clicked.connect(self.reset_layout)
        banner.addWidget(self.reset_layout_button)
        self.done_button = QPushButton("Done")
        self.done_button.setObjectName("primary")
        self.done_button.setToolTip("Stop editing the layout (Esc)")
        self.done_button.clicked.connect(lambda: self.set_editing(False))
        banner.addWidget(self.done_button)
        self.layout_banner.setVisible(False)
        layout.addWidget(self.layout_banner)

        self.list_area = QScrollArea()
        self.list_area.setObjectName("listArea")
        self.list_area.setWidgetResizable(True)
        self.list_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.list_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setObjectName("listContainer")
        page = QVBoxLayout(container)
        page.setContentsMargins(0, 0, 4, 0)
        page.setSpacing(12)
        self.gauges = GaugeStrip()
        self.gauges.selected.connect(self._pick)
        self.gauges.opened.connect(self.open_focus)
        self.gauge_panel = GaugePanel(self.gauges)  # one of the deck's panels, first by default
        self.deck = CardDeck()
        page.addWidget(self.deck)
        # While editing: the panels you've hidden, each with a button to bring it back.
        self.hidden_box = CardFrame()
        hidden = QVBoxLayout(self.hidden_box)
        hidden.setContentsMargins(14, 10, 14, 12)
        hidden_title = QLabel("HIDDEN PANELS")
        hidden_title.setObjectName("statLabel")
        hidden.addWidget(hidden_title)
        self.hidden_none = QLabel("None. Hide a panel with its eye button and it waits here.")
        self.hidden_none.setObjectName("muted")
        hidden.addWidget(self.hidden_none)
        self.hidden_buttons = FlowRow()  # wraps, so many hidden panels never widen the page
        hidden.addLayout(self.hidden_buttons)
        self.hidden_box.setVisible(False)
        page.addWidget(self.hidden_box)
        page.addStretch(1)
        self.list_area.setWidget(container)

        self.detail = DetailPanel()
        self.detail.window_changed.connect(lambda seconds: self._show_detail())
        self.detail.pin_toggled.connect(lambda pinned: self.set_pinned(self.selected_key, pinned))
        stored_window = self.settings.value("detail_window", 300.0)
        with contextlib.suppress(ValueError):
            self.detail.set_window(self.detail.nearest_window(float(str(stored_window))))
        self.detail.window_changed.connect(lambda seconds: self.settings.setValue("detail_window", seconds))
        self.detail.focus_requested.connect(self._focus_selected)
        self.detail.expanded_changed.connect(self._set_drawer)
        stored_height = self.settings.value("drawer_height", 360)
        self._drawer_height = 360
        with contextlib.suppress(ValueError, TypeError, OverflowError):
            self._drawer_height = max(120, int(float(str(stored_height))))

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.list_area)
        self.splitter.addWidget(self.detail)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        self.splitter.setSizes([520, 360])
        self.splitter.splitterMoved.connect(self._remember_drawer_height)
        # A click on a sensor opens (or shuts) the drawer only once it can't be the start of a
        # double-click: opened at once, the drawer could land on the sensor and take the second
        # click, so the focus view would never open.
        self._drawer_timer = QTimer(self)
        self._drawer_timer.setSingleShot(True)
        self._drawer_timer.setInterval(QApplication.doubleClickInterval())
        self._drawer_timer.timeout.connect(self._settle_pick)
        self._pending_drawer = False
        self._pick_spot: QRect | None = None
        self._drawer_open = False  # the strip and its arrow change the panel before they say so

        # One sensor across the whole window, in place of the overview until you go back.
        self.focus = FocusView()
        self.focus.back.connect(self.close_focus)
        self.focus.selected.connect(self.open_focus)  # a core in its strip
        self.focus.stepped.connect(self._step_focus)
        self.focus.window_changed.connect(self.detail.set_window)  # one window setting for both
        self.focus.rename_requested.connect(self._rename_interactively)
        self.focus.pin_toggled.connect(lambda pinned: self.set_pinned(self.focus_key, pinned))
        # Esc goes back from the focus view, or shuts the detail drawer, wherever the keyboard is in
        # the window; the rest of the time Esc is left to whatever has the keyboard.
        self.close_focus_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.close_focus_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.close_focus_shortcut.activated.connect(self._escape)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.splitter)
        self.pages.addWidget(self.focus)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self._set_drawer(False)  # the drawer starts shut: the cards get the window

    def _make_option_actions(self) -> None:
        """The app's options as actions, shared by the settings menu and the tray menu so both
        always agree. The toolbar's interval and units are offered in both menus too."""
        self.open_action = QAction("Open corewatch", self)
        self.open_action.triggered.connect(self.show_window)
        self.reset_action = QAction("Reset min/max", self)
        self.reset_action.setToolTip("Forget every recorded lowest, highest and average value (Ctrl+R)")
        self.reset_action.triggered.connect(self.reset_stats)

        self.interval_actions = QActionGroup(self)
        for index in range(self.interval_box.count()):
            seconds = float(self.interval_box.itemData(index))
            action = QAction(self.interval_box.itemText(index), self)
            action.setCheckable(True)
            action.setChecked(seconds == self.interval)
            action.setData(seconds)
            self.interval_actions.addAction(action)
        self.interval_actions.triggered.connect(lambda action: self.set_interval(float(action.data()), remember=True))

        self.unit_actions = QActionGroup(self)
        for text, is_f in (("Celsius (°C)", False), ("Fahrenheit (°F)", True)):
            action = QAction(text, self)
            action.setCheckable(True)
            action.setChecked(is_f == self.fahrenheit)
            action.setData(is_f)
            self.unit_actions.addAction(action)
        self.unit_actions.triggered.connect(lambda action: self.set_fahrenheit(bool(action.data())))

        self.min_max_action = QAction("Show min / max in the list", self)
        self.min_max_action.setCheckable(True)
        self.min_max_action.setChecked(self.show_min_max)
        self.min_max_action.toggled.connect(self.set_show_min_max)

        self.theme_actions = QActionGroup(self)
        for key, text in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            action = QAction(text, self)
            action.setCheckable(True)
            action.setChecked(key == self.theme)
            action.setData(key)
            self.theme_actions.addAction(action)
        self.theme_actions.triggered.connect(lambda action: self.apply_theme(str(action.data())))
        self.unused_action = QAction("Show unused sensors", self)
        self.unused_action.setCheckable(True)
        self.unused_action.setChecked(self.show_unused)
        self.unused_action.setToolTip(
            "Also list inputs with nothing attached: empty fan headers and unconnected temperature probes"
        )
        self.unused_action.toggled.connect(self.set_show_unused)
        self.host_action = QAction("Show this computer's name", self)
        self.host_action.setCheckable(True)
        self.host_action.setChecked(self.show_host)
        self.host_action.setToolTip("Show the computer's name under corewatch's, at the top left")
        self.host_action.toggled.connect(self.set_show_host)
        self.tray_action = QAction("Keep running in the tray when closed", self)
        self.tray_action.setCheckable(True)
        self.tray_action.setChecked(self.close_to_tray)
        self.tray_action.toggled.connect(self._set_close_to_tray)
        self.autostart_action = QAction("Start when I log in", self)
        self.autostart_action.setCheckable(True)
        self.autostart_action.setToolTip("Start corewatch each time you log in to your desktop")
        self._sync_autostart()
        self.autostart_action.toggled.connect(self.set_autostart)
        self.minimized_action = QAction("Start minimized in the tray", self)
        self.minimized_action.setCheckable(True)
        self.minimized_action.setChecked(self.start_minimized)
        self.minimized_action.setToolTip(
            "Start without opening this window; double-click the tray icon to open it. "
            "Applies at login too. Without a system tray the window always opens."
        )
        self.minimized_action.toggled.connect(self._set_start_minimized)
        self.quit_action = QAction("Quit", self)
        self.quit_action.setShortcut("Ctrl+Q")
        self.quit_action.triggered.connect(self.quit)

    def _add_display_options(self, menu: QMenu) -> None:
        """Reset min/max and the display choices: the top of the settings and tray menus."""
        menu.addAction(self.reset_action)
        menu.addSeparator()
        menu.addMenu("Update every").addActions(self.interval_actions.actions())
        menu.addMenu("Temperatures in").addActions(self.unit_actions.actions())
        menu.addAction(self.min_max_action)
        menu.addSeparator()

    def _add_options(self, menu: QMenu) -> None:
        """The options shared by the settings menu and the tray menu, from theme down to Quit."""
        theme_menu = menu.addMenu("Theme")
        theme_menu.addActions(self.theme_actions.actions())
        menu.addActions([self.unused_action, self.host_action])
        menu.addSeparator()
        menu.addActions([self.tray_action, self.autostart_action, self.minimized_action])
        menu.addSeparator()
        menu.addAction(self.quit_action)
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(self._sync_autostart)  # it can be changed outside corewatch too

    # ----- settings -----------------------------------------------------------------

    def set_interval(self, seconds: float, remember: bool = True) -> None:
        seconds = float(seconds)
        if not math.isfinite(seconds):
            seconds = 1.0
        self.interval = min(max(seconds, MIN_INTERVAL), MAX_INTERVAL)
        # Keep the toolbar and the tray menu showing the same choice, whichever made it.
        with QSignalBlocker(self.interval_box):
            self.interval_box.setCurrentIndex(self.interval_box.findData(self.interval))
        for action in self.interval_actions.actions():
            action.setChecked(float(action.data()) == self.interval)
        self.timer.start(round(self.interval * 1000))
        # A sample is "missing" (draw a gap) only if it is well overdue.
        self.gap = self.interval * 3 + 1
        if self.live_chip.property("stalled") is not True:  # a stall stays one whatever the interval
            self._interval_set_at = self.monitor.clock()  # but a shorter interval isn't overdue already
        self._update_live()
        if remember:
            self.settings.setValue("interval", self.interval)

    def set_show_unused(self, show: bool) -> None:
        self.show_unused = show
        self.settings.setValue("show_unused", show)
        self.ordered = self.monitor.visible_rows(self.all_rows, show)
        self.plain_ordered = self.monitor.visible_rows(gather_cards(self.raw_rows), show)
        self.redraw()

    def set_show_min_max(self, show: bool) -> None:
        self.show_min_max = show
        self.min_max_action.setChecked(show)
        self.settings.setValue("show_min_max", show)
        for section in self.sections.values():
            section.grid.set_show_min_max(show)
        self.redraw()  # a longer or shorter grey line: the cards may want new widths

    def set_fahrenheit(self, fahrenheit: bool) -> None:
        self.fahrenheit = fahrenheit
        self.celsius_button.setChecked(not fahrenheit)
        self.fahrenheit_button.setChecked(fahrenheit)
        for action in self.unit_actions.actions():
            action.setChecked(bool(action.data()) == fahrenheit)
        self.settings.setValue("fahrenheit", fahrenheit)
        self.redraw()

    def apply_theme(self, theme: str) -> None:
        self.theme = theme if theme in THEMES else "system"
        themes.set_mode(self.theme)
        QApplication.styleHints().setColorScheme(THEMES[self.theme])
        self.settings.setValue("theme", self.theme)
        self._restyle()

    def _restyle(self) -> None:
        app = QApplication.instance()
        theme = themes.current_theme()
        if isinstance(app, QApplication):
            themes.apply(app, theme)
        self.settings_button.setIcon(settings_icon(theme.muted))
        self.detail.focus_button.setIcon(expand_icon(theme.muted))
        bars = [self.gauge_panel.layout_bar, *(section.layout_bar for section in self.sections.values())]
        for bar in bars:
            if bar is not None:
                bar.restyle()
        self.focus.rename_button.setIcon(pencil_icon(theme.muted))
        for step, button in self.focus.step_buttons.items():
            button.setIcon(chevron_icon(theme.muted, up=step < 0))
        self.redraw()

    def _sync_autostart(self) -> None:
        """Show what's really configured: the autostart file is the setting."""
        with QSignalBlocker(self.autostart_action):
            self.autostart_action.setChecked(autostart.is_enabled(self.autostart_file))

    def set_autostart(self, enabled: bool) -> None:
        try:
            if enabled:
                message = autostart.enable(self.autostart_file)
            elif autostart.disable(self.autostart_file):
                message = "corewatch won't start when you log in"
            else:
                message = (
                    f"{self.autostart_file} wasn't created by corewatch (or can't be read), so it was left "
                    "alone. Turn it off in your desktop's Startup Applications settings."
                )
        except autostart.AutostartError as error:
            message = str(error)
        except OSError as error:
            message = f"Couldn't change the login setting: {error.strerror or error}"
        self._sync_autostart()
        self.statusBar().showMessage(message, 8000)

    def _set_start_minimized(self, enabled: bool) -> None:
        self.start_minimized = enabled
        self.settings.setValue("start_minimized", enabled)

    def start(
        self,
        minimized: bool,
        tray_available: Callable[[], bool],
        tray_factory: Callable[[], QSystemTrayIcon],
        at_login: bool = False,
        retry_ms: int = 1000,
        attempts: int = 30,
    ) -> None:
        """Show the window, or start in the tray.

        Without a tray, the window opens straight away, except at login: there the panel's
        tray often comes up a moment after corewatch does, so a minimized login start waits
        for it, out of sight, and only opens the window if no tray appears. Either way the
        tray is attached whenever it turns up, so close-to-tray works afterwards."""
        if tray_available():
            if not minimized:
                self.show_window()
            self.attach_tray(tray_factory)
            return
        self._set_tray_options_available(False)
        if not (minimized and at_login):
            self.show_window()
        self._tray_tries = attempts

        def retry() -> None:
            if tray_available():
                self._tray_timer.stop()
                self.attach_tray(tray_factory)
                return
            self._tray_tries -= 1
            if self._tray_tries <= 0:
                self._tray_timer.stop()
                if not self.isVisible():
                    self.show_window()  # no tray to return to: never leave corewatch out of reach

        self._tray_timer = QTimer(self)
        self._tray_timer.timeout.connect(retry)
        self._tray_timer.start(retry_ms)

    def _set_tray_options_available(self, available: bool) -> None:
        """Starting minimized needs a tray to come back from; say so instead of silently failing."""
        self.minimized_action.setEnabled(available)
        self.minimized_action.setToolTip(
            "Start without opening this window; double-click the tray icon to open it. Applies at login too."
            if available
            else "Needs a system tray, and this desktop doesn't have one right now"
        )

    def set_show_host(self, show: bool) -> None:
        self.show_host = show
        self.host_action.setChecked(show)
        self.settings.setValue("show_host", show)
        self.host_label.setVisible(show)
        self._fit_toolbar()

    def _update_live(self) -> None:
        """LIVE while readings arrive; WAITING (amber) once one is well overdue, say a hung driver."""
        last = self.monitor.last_sample_at
        stalled = last is not None and self.monitor.clock() - max(last, self._interval_set_at) > self.gap
        text = live_text(self.interval, stalled)
        if self.live_chip.property("stalled") != stalled:
            self.live_chip.setProperty("stalled", stalled)
            self.live_chip.style().unpolish(self.live_chip)
            self.live_chip.style().polish(self.live_chip)
        if text != self.live_chip.text():
            self.live_chip.setText(text)
            self._fit_toolbar()  # WAITING is much wider than LIVE

    def _fit_toolbar(self) -> None:
        """In a narrow window, drop the name block and the LIVE chip before the toolbar would push
        the settings button, the only way to the options, into its overflow menu."""
        layout = self.toolbar.layout()
        spacing = layout.spacing() if layout is not None else 0
        chip = self.live_chip.sizeHint().width() + spacing
        brand = self.brand.sizeHint().width() + spacing
        rest = self.toolbar.sizeHint().width()  # what the toolbar needs without either
        rest -= chip if self.live_chip_action.isVisible() else 0
        rest -= brand if self.brand_action.isVisible() else 0
        # Keep the window at least wide enough for what is left.
        self.toolbar.setMinimumWidth(rest)
        room = self.toolbar.width()
        # Work it out rather than showing both and measuring, which relayouts the toolbar twice
        # on every resize of a narrow window; a toolbar that still overflows sheds more below.
        self.live_chip_action.setVisible(rest + brand + chip <= room)
        self.brand_action.setVisible(rest + brand <= room)
        if self.toolbar.sizeHint().width() > room:
            self.live_chip_action.setVisible(False)
        if self.toolbar.sizeHint().width() > room:
            self.brand_action.setVisible(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_toolbar()

    def _set_close_to_tray(self, enabled: bool) -> None:
        self.close_to_tray = enabled
        self.settings.setValue("close_to_tray", enabled)

    def _remember_table(self, device: str, shown: bool) -> None:
        if shown:
            self.expanded.add(device)
        else:
            self.expanded.discard(device)
        self.settings.setValue("expanded_cards", json.dumps(sorted(self.expanded)))
        self.redraw()  # the card's width needs changed

    # ----- data ---------------------------------------------------------------------

    def reset_stats(self) -> None:
        # Don't take an extra reading here: one taken milliseconds after the last tick
        # measures CPU load over a sliver of time and would seed min/max with noise.
        self.monitor.reset()
        self.redraw()

    def _request_sample(self) -> None:
        if self._sampling:  # the previous reading is still in flight; skip this tick
            return
        self._sampling = True
        self.sample_requested.emit()

    def _on_collected(self, collected: Collected) -> None:
        self._sampling = False
        if not self._stopped:  # ignore a reading that lands after shutdown began
            self.show_rows(self.monitor.ingest(collected))

    def refresh(self) -> None:
        """Take a reading right now on this thread (startup, tests)."""
        if self._sampling:  # the worker owns the sources until it reports back
            return
        self.show_rows(self.monitor.sample())

    def show_rows(self, raw: list[Row]) -> None:
        self.raw_rows = raw
        rows = self._present(raw)
        self.rows.clear()
        self.rows.update({row.reading.key: row for row in rows})
        self.all_rows = rows
        self.ordered = self.monitor.visible_rows(rows, self.show_unused)
        # The same rows under their original names: what headline() and the tray summary look
        # for ("CPU package", "CPU load") must not depend on what you've renamed them to.
        self.plain_ordered = self.monitor.visible_rows(gather_cards(raw), self.show_unused)
        notes = self.monitor.notes()
        self.notes.setText("\n".join(f"•  {note}" for note in notes))
        self.notes.setVisible(bool(notes))
        self.redraw()
        unused = len(rows) - len(self.ordered)
        hidden = f" ({unused} unused hidden)" if unused else ""
        self.statusBar().showMessage(
            f"{len(self.ordered)} sensors{hidden}  ·  updated {time.strftime('%H:%M:%S')}  ·  every {self.interval:g} s"
        )
        self._update_live()

    def redraw(self) -> None:
        text = self.filter.text()
        filtering = bool(text.strip())
        groups = sorted(
            (
                (device, [r for r in rows if matches(text, device, r.reading.label)])
                for device, rows in group_rows(self.ordered)
            ),
            key=lambda group: card_rank(group[0]),  # wide cards first; sorted() keeps the rest in order
        )
        devices = [device for device, _ in groups]
        if devices != self._devices:
            self._rebuild(devices)
        now = self.monitor.last_sample_at or self.monitor.clock()
        # The overview reads the sensors under their original names, so renames don't hide them,
        # and steps aside while filtering.
        self.gauges.set_wanted(not filtering)
        self.gauges.set_specs(gauge_specs(self.plain_ordered, self.fahrenheit))
        hidden = self.panel_layout.hidden
        self.gauge_panel.setVisible(GAUGES not in hidden and not filtering and bool(self.gauges.gauges))
        labels = {key: row.reading.label for key, row in self.rows.items()}
        for device, rows in groups:
            section = self.sections[device]
            data = ViewData(
                [r for r in self.plain_ordered if r.reading.device == device],
                self.plain_ordered,
                labels,
                self.fahrenheit,
                now,
                self.gap,
            )
            section.set_view_data(data, filtering)
            section.setVisible(bool(rows) and device not in hidden)
            section.set_rows(rows, now, self.fahrenheit, self.gap)
            # Beside another card, it needs room for its compact view, or for its list when shown.
            need = themes.px(CardDeck.MIN_HALF)
            if section.table_shown():
                need = max(need, section.grid.cell_min_width() + 28)  # 28: the card's side margins
            self.deck.set_need(section, need)
        self._arrange()
        self.deck.reflow()  # cards with no match while filtering leave no hole
        shown = {row.reading.key for row in self.ordered}
        if self.panel_layout.hidden:  # a sensor on a card you've hidden can't stay selected either
            hidden_cards = [rows for device, rows in group_rows(self.ordered) if device in self.panel_layout.hidden]
            shown -= {row.reading.key for rows in hidden_cards for row in rows}
            # Unless a gauge shows it: you've kept the gauges (a filter hides them only for now).
            if GAUGES not in self.panel_layout.hidden:
                shown |= {gauge.spec.key for gauge in self.gauges.gauges if gauge.spec is not None}
        if self.selected_key not in shown:  # gone, or now hidden
            best = headline([row for row in self.plain_ordered if row.reading.key in shown])
            self.selected_key = best.reading.key if best else next((key for key in self.rows if key in shown), None)
        for section in self.sections.values():
            section.set_selected(self.selected_key)
        self._show_detail()
        self._update_tray()

    def _rebuild(self, devices: list[str]) -> None:
        """Create a card per device, reusing existing cards so their state survives."""
        for device in list(self.sections):
            if device not in devices:
                gone = self.sections.pop(device)
                if gone.layout_bar is not None:
                    gone.layout_bar.cancel_drag()  # its device went away in the middle of a drag
                self.deck.mark_drop(None)
                gone.deleteLater()
        for device in devices:
            section = self.sections.get(device)
            if section is None:
                # Parented at once: a card shown before it has a parent would open as a window.
                section = CategorySection(
                    device, parent=self.deck, view=view_for(device), show_table=device in self.expanded
                )
                section.table_toggled.connect(self._remember_table)
                section.grid.selected.connect(self._pick)
                section.grid.opened.connect(self.open_focus)
                if section.view is not None:
                    section.view.selected.connect(self._pick)
                    section.view.opened.connect(self.open_focus)
                section.grid.context_requested.connect(self._show_sensor_menu)
                section.grid.set_show_min_max(self.show_min_max)
                self.sections[device] = section
                if self.editing:
                    self._bar(device)
                    section.set_editing(True)
        self._devices = devices
        self._deck_key = None  # cards came or went: lay the deck out again

    def _apply_filter(self) -> None:
        self.redraw()

    def _select(self, key: str) -> None:
        self.selected_key = key
        self.settings.setValue("selected", key)
        for section in self.sections.values():
            section.set_selected(key)
        self._show_detail()

    def _show_detail(self) -> None:
        row = self.rows.get(self.selected_key) if self.selected_key else None
        now = self.monitor.last_sample_at or self.monitor.clock()
        pinned = self.selected_key in self.pinned
        can_pin = self._tray_factory is not None
        self.detail.show_row(row, now, self.fahrenheit, self.gap, self.monitor.to_wall, pinned, can_pin)
        self._show_focus()

    # ----- edit layout ----------------------------------------------------------------

    def _panel_widget(self, panel: str) -> GaugePanel | CategorySection:
        return self.gauge_panel if panel == GAUGES else self.sections[panel]

    def _bar(self, panel: str) -> LayoutBar:
        """``panel``'s edit bar, made and wired up the first time it's wanted."""
        widget = self._panel_widget(panel)
        made = widget.layout_bar is None
        bar = widget.edit_bar()
        if made:
            self._connect_bar(panel, bar)
        return bar

    def _connect_bar(self, panel: str, bar: LayoutBar) -> None:
        bar.step.connect(lambda step: self.move_panel(panel, step))
        bar.width_chosen.connect(lambda wide: self.set_panel_wide(panel, wide))
        bar.hide_requested.connect(lambda: self.set_panel_hidden(panel, True))
        bar.grip.moved.connect(
            lambda point: self.deck.mark_drop(self._drop_target(panel, point), self.panel_layout.wide(panel))
        )
        bar.grip.ended.connect(lambda point: self._drop_panel(panel, point))
        bar.grip.cancelled.connect(lambda: self.deck.mark_drop(None))

    def _arrange(self) -> None:
        """Lay the deck out in your order and widths, and bring the edit bars up to date."""
        self._panels = self.panel_layout.arrange([GAUGES, *self._devices])
        key = tuple((panel, self.panel_layout.wide(panel)) for panel in self._panels)
        for panel, wide in key:
            if panel != GAUGES:  # a wide card lists its sensors in two columns, a half-width one in one
                self.sections[panel].grid.set_columns(2 if wide else 1)
        if key != self._deck_key:
            self._deck_key = key
            self.deck.set_cards([(self._panel_widget(panel), wide) for panel, wide in key])
        if not self.editing:
            return
        shown = self._shown_panels()
        for panel in shown:
            self._bar(panel).set_state(panel == shown[0], panel == shown[-1], self.panel_layout.wide(panel))
        self._sync_hidden_box()

    def _shown_panels(self) -> list[str]:
        return [panel for panel in self._panels if not self._panel_widget(panel).isHidden()]

    def _sync_hidden_box(self) -> None:
        # The hidden panels on screen, then any whose device isn't here now (unplugged, or renamed
        # by a driver update), so a hidden panel can always be let go of.
        here = [panel for panel in self._panels if panel in self.panel_layout.hidden]
        away = sorted(self.panel_layout.hidden - set(self._panels))
        hidden = (*here, *away)
        if hidden == self._hidden_shown:
            return
        self._hidden_shown = hidden
        while self.hidden_buttons.count():
            item = self.hidden_buttons.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for panel in hidden:
            button = QPushButton(f"Show {panel}" if panel in here else f"Show {panel} (not here now)")
            button.setToolTip(f"Show {panel} again" if panel in here else "It shows again when its device is back")
            button.clicked.connect(lambda _=False, panel=panel: self.set_panel_hidden(panel, False))
            self.hidden_buttons.addWidget(button)
        self.hidden_none.setVisible(not hidden)

    def set_editing(self, editing: bool) -> None:
        """Edit-layout mode: every panel shows how to move, resize or hide it."""
        if editing == self.editing:
            return
        self.editing = editing
        if editing:
            self.close_focus()
            self.filter.clear()  # every panel in view while you arrange them
        self.filter.setEnabled(not editing)
        self.layout_banner.setVisible(editing)
        self.hidden_box.setVisible(editing)
        for panel in self._panels:
            if editing:
                self._bar(panel)
            self._panel_widget(panel).set_editing(editing)
        if not editing:  # Esc in the middle of a drag: it lands nowhere
            for panel in self._panels:
                bar = self._panel_widget(panel).layout_bar
                if bar is not None:
                    bar.cancel_drag()
            self.deck.mark_drop(None)
        self._sync_escape()
        self.redraw()

    def _change_layout(self) -> None:
        self.settings.setValue("layout", self.panel_layout.dump())
        self.redraw()

    def move_panel(self, panel: str, step: int) -> None:
        self.panel_layout.move(panel, step, self._panels, self._shown_panels())
        self._change_layout()
        QTimer.singleShot(0, self, lambda: self._follow(panel))  # once the deck has its new order

    def _follow(self, panel: str) -> None:
        """Keep a panel moved with its arrows in view, so the arrows can take it the whole way."""
        # Its bar, not the whole panel: one taller than the view would be centred, its arrows
        # scrolled out of sight above.
        if self.editing and panel in self._panels:
            self.list_area.ensureWidgetVisible(self._bar(panel), 0, 16)

    def set_panel_wide(self, panel: str, wide: bool) -> None:
        self.panel_layout.widths[panel] = wide
        self._change_layout()

    def set_panel_hidden(self, panel: str, hidden: bool) -> None:
        if hidden:
            self.panel_layout.hidden.add(panel)
        else:
            self.panel_layout.hidden.discard(panel)
        self._change_layout()

    def reset_layout(self) -> None:
        self.panel_layout = Layout()
        self._change_layout()

    def _drop_target(self, panel: str, point: QPoint) -> tuple[QWidget, bool] | None:
        """Where a panel dragged to ``point`` (on screen) would land; None over itself, outside
        the cards in view (the drawer, the banner, past the window), or once editing has ended."""
        if not self.editing or panel not in self._panels:
            return None
        viewport = self.list_area.viewport()
        if not viewport.rect().contains(viewport.mapFromGlobal(point)):
            return None
        target = self.deck.drop_at(self.deck.mapFromGlobal(point), self.panel_layout.wide(panel))
        return None if target is None or target[0] is self._panel_widget(panel) else target

    def _drop_panel(self, panel: str, point: QPoint) -> None:
        target = self._drop_target(panel, point)
        self.deck.mark_drop(None)
        if target is None:
            return
        widget, before = target
        shown = self._shown_panels()
        index = next(i for i, p in enumerate(shown) if self._panel_widget(p) is widget) + (0 if before else 1)
        following = next((p for p in shown[index:] if p != panel), None)
        # Hidden panels keep their place in the order: put it before the next shown one.
        self.panel_layout.drop(panel, following, self._panels)
        self._change_layout()

    # ----- focus view -----------------------------------------------------------------

    def open_focus(self, key: str) -> None:
        """Show sensor ``key`` across the whole window (it's selected in the overview too)."""
        if key not in self.rows:
            return
        if self.editing:  # a double-click on a sensor while editing: done editing, then focus
            self.set_editing(False)
        self.focus_key = key
        self._drawer_timer.stop()  # a double-click: the click it started with leaves the drawer be
        self._select(key)
        self._sync_escape()
        if self.pages.currentWidget() is not self.focus:
            self.pages.setCurrentWidget(self.focus)
            self.focus.back_button.setFocus(Qt.FocusReason.OtherFocusReason)  # so Esc goes back

    def _pick(self, key: str) -> None:
        """A click on a sensor in the overview: select it, then, once the click can't be the start
        of a double-click, open the drawer to its chart, or, on the sensor the open drawer shows,
        shut it. A double-click opens the focus view instead and leaves the drawer as it is."""
        if self.focus_key is not None:  # the release ending a double-click, after the focus view opened
            return
        self._pending_drawer = not (key == self.selected_key and self.detail.expanded)
        self._pick_spot = self._spot(key)
        self._select(key)
        self._drawer_timer.start()

    def _settle_pick(self) -> None:
        self._set_drawer(self._pending_drawer)
        spot = self._pick_spot
        if self._pending_drawer and spot is not None:  # keep what was clicked clear of the drawer
            self.list_area.ensureVisible(spot.center().x(), spot.center().y(), 0, spot.height() // 2 + 16)

    def _spot(self, key: str) -> QRect | None:
        """Where the clicked sensor is, in the scrolled cards' coordinates."""
        source = self.sender()
        content = self.list_area.widget()
        if not isinstance(source, QWidget) or content is None or not content.isAncestorOf(source):
            return None
        if isinstance(source, SensorGrid):
            rect = next((cell.rect for cell in source.cells() if cell.key == key), source.rect())
        else:  # a gauge, a core or a tile: around the pointer, or the whole view from the keyboard
            point = source.mapFromGlobal(QCursor.pos())
            rect = QRect(point.x() - 1, point.y() - 20, 2, 40) if source.rect().contains(point) else source.rect()
        return QRect(source.mapTo(content, rect.topLeft()), rect.size())

    def _set_drawer(self, expanded: bool) -> None:
        """Open the detail drawer to its chart, at the height it was last left at, or shut it to its
        one-line strip so the cards get the window. Already open, it stays the height it is."""
        self._drawer_timer.stop()  # shut (or opened) since a click: that click has had its say
        opening = expanded and not self._drawer_open
        self._drawer_open = expanded
        if self.detail.expanded != expanded:
            self.detail.set_expanded(expanded)
        if opening:
            total = sum(self.splitter.sizes()) or self.splitter.height()
            height = max(min(self._drawer_height, total - 200), self.detail.minimumSizeHint().height())
            self.splitter.setSizes([max(total - height, 0), height])
        self._sync_escape()

    def _remember_drawer_height(self, position: int, index: int) -> None:
        if self.detail.expanded:
            self._drawer_height = self.splitter.sizes()[1]
            self.settings.setValue("drawer_height", self._drawer_height)

    def _sync_escape(self) -> None:
        self.close_focus_shortcut.setEnabled(self.editing or self.focus_key is not None or self.detail.expanded)

    def _escape(self) -> None:
        if self.editing:
            self.set_editing(False)
        elif self.focus_key is not None:
            self.close_focus()
        elif self.detail.expanded:
            self._set_drawer(False)

    def _focus_selected(self) -> None:
        if self.selected_key is not None:
            self.open_focus(self.selected_key)

    def close_focus(self) -> None:
        if self.focus_key is None:
            return
        self.focus_key = None
        self._sync_escape()
        self.pages.setCurrentWidget(self.splitter)

    def _show_focus(self) -> None:
        if self.focus_key is None:
            return
        row = self.rows.get(self.focus_key)
        shown = row is not None and any(r.reading.key == self.focus_key for r in self.ordered)
        if row is None or not shown:  # it stopped reporting, or is hidden now: back to every sensor
            self.close_focus()
            return
        device = row.reading.origin or row.reading.device
        self.focus.set_window(self.detail.window_seconds)  # the detail panel holds the setting
        cores = None
        if device.startswith("CPU"):
            labels = {key: r.reading.label for key, r in self.rows.items()}
            plain = [r for r in self.plain_ordered if (r.reading.origin or r.reading.device) == device]
            cores = ViewData(plain, self.plain_ordered, labels, self.fahrenheit, self._now(), self.gap)
        # Under its original name, as the rows it's matched with are: a graphics card's fan finds
        # its control by name.
        plain_row = next((r for r in self.plain_ordered if r.reading.key == row.reading.key), row)
        # Per-core sensors go in the core strip, unless there's too few cores for one.
        cores_shown = cores is not None and sum(len(group) for _, group in core_groups(cores.rows)) >= MIN_CORES
        neighbours, total = related_rows(plain_row, self.plain_ordered, cores_shown=cores_shown)
        names = {key: r.reading.label for key, r in self.rows.items()}
        related = ViewData(neighbours, self.plain_ordered, names, self.fahrenheit, self._now(), self.gap)
        self.focus.show_row(
            row,
            cores,
            related,
            total,
            self._now(),
            self.fahrenheit,
            self.gap,
            self.monitor.to_wall,
            self.focus_key in self.pinned,
            self._tray_factory is not None,
        )

        previous, following = self._neighbours()
        self.focus.set_neighbours(*(self._step_name(r) if r is not None else None for r in (previous, following)))

    def _step_order(self) -> list[Row]:
        """Every sensor in the overview's order: card by card, each card's in its list's order
        (only what the filter matches, while there's one)."""
        cards = [p for p in self._panels if p in self.sections and p not in self.panel_layout.hidden]
        return [row for device in cards for _, rows in self.sections[device].grid.blocks() for row in rows]

    def _neighbours(self) -> tuple[Row | None, Row | None]:
        """The focus view's previous and next sensor; the last one steps round to the first. From a
        sensor the filter doesn't match, the steps go to its last and first match."""
        order = self._step_order()
        keys = [r.reading.key for r in order]
        if self.focus_key not in keys:
            return (order[-1], order[0]) if order else (None, None)
        if len(order) == 1:
            return None, None
        index = keys.index(self.focus_key)
        return order[index - 1], order[(index + 1) % len(order)]

    def _step_name(self, row: Row) -> str:
        return f"{row.reading.label} on {row.reading.origin or row.reading.device}"  # the list's own names

    def _step_focus(self, step: int) -> None:
        row = self._neighbours()[0 if step < 0 else 1]
        if row is not None:
            self.open_focus(row.reading.key)

    def _now(self) -> float:
        return self.monitor.last_sample_at or self.monitor.clock()

    # ----- tray & lifecycle -----------------------------------------------------------

    def _summary(self) -> list[str]:
        def first(device_prefix: str, kind: Kind, label_prefix: str = "") -> Row | None:
            return next(
                (
                    r
                    for r in self.plain_ordered
                    if r.reading.device.startswith(device_prefix)
                    and r.reading.kind is kind
                    and r.reading.label.startswith(label_prefix)
                ),
                None,
            )

        lines = []
        for name, temp, load in (
            ("CPU", headline(self.plain_ordered), first("CPU", Kind.LOAD, "CPU load")),
            ("GPU", first("GPU", Kind.TEMPERATURE), first("GPU", Kind.LOAD, "GPU load")),
        ):
            if temp is None:
                continue
            line = f"{name} {format_value(Kind.TEMPERATURE, temp.reading.value, self.fahrenheit)}"
            if load is not None and load.reading.value is not None:
                line += f" · {load.reading.value:.0f}% load"
            lines.append(line)
        return lines

    def _tray_content(self, key: str) -> tuple[str, TrayLevel, str]:
        """(number on the icon, its level for ``tray_level``, tooltip) for one tray icon."""
        if key == APP_TRAY:  # the logo; only its tooltip changes
            return "", "normal", "\n".join(["corewatch", *self._summary()])
        row = self.rows.get(key)
        if row is None:  # the sensor went away (driver unloaded, GPU asleep)
            return "?", "none", "corewatch\nThis pinned sensor isn't reporting right now"
        reading, stats = row.reading, row.stats

        def shown(value: float | None) -> str:
            return format_value(reading.kind, value, self.fahrenheit)

        # The icon already shows the current value; hovering adds how it has behaved.
        tooltip = "\n".join(
            [
                reading.device,
                reading.label,
                f"min: {shown(stats.minimum)}",
                f"max: {shown(stats.maximum)}",
                f"average: {shown(stats.average)}",
            ]
        )
        return format_short(reading.kind, reading.value, self.fahrenheit), tray_level(row), tooltip

    def _update_tray(self) -> None:
        """corewatch's own icon, then one icon per pinned sensor."""
        if self._tray_factory is None:
            return
        wanted = [APP_TRAY, *self.pinned]
        for key in [key for key in self.trays if key not in wanted]:
            gone = self.trays.pop(key)
            gone.hide()
            gone.deleteLater()
            self._tray_menus.pop(key).deleteLater()
            self._tray_info.pop(key, None)
            self._tray_states.pop(key, None)
        for key in wanted:
            tray = self.trays.get(key)
            if tray is None:
                tray = self.trays[key] = self._tray_factory()
                menu = self._tray_menus[key] = self._tray_menu_for(key)
                tray.setContextMenu(menu)
                tray.activated.connect(self._on_tray_activated)
                if key == APP_TRAY:
                    tray.setIcon(tray_icon(self.light_panel))
            text, level, tooltip = self._tray_content(key)
            unpin = self._tray_menus[key].property("unpin")
            if isinstance(unpin, QAction):
                row = self.rows.get(key)
                unpin.setText(f"Unpin {row.reading.label}" if row else "Unpin this sensor")
            # Each update is a D-Bus round trip to the tray host, so only send real changes.
            if self._tray_states.get(key) != (text, level, tooltip):
                self._tray_states[key] = (text, level, tooltip)
                lines = tooltip.splitlines()
                for index, action in enumerate(self._tray_info.get(key, [])):
                    action.setText(lines[index] if index < len(lines) else "")
                    action.setVisible(index < len(lines))
                if key != APP_TRAY:
                    tray.setIcon(number_icon(text, level, self.light_panel))
                tray.setToolTip(tooltip)
            if not tray.isVisible():
                tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Clicking or double-clicking any of corewatch's icons brings the window up. Ubuntu's
        panel opens the menu on a click and reports a double click as a click, so this never
        hides the window (the menu's Show / hide window does)."""
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def attach_tray(self, factory: Callable[[], QSystemTrayIcon]) -> None:
        """``factory`` makes one tray icon; it's called for corewatch's own and once per pinned sensor."""
        self._tray_factory = factory
        self._update_tray()
        self._show_detail()  # pinning is possible now
        self._set_tray_options_available(True)

    def _tray_menu_for(self, key: str) -> QMenu:
        """corewatch's own icon has the app's actions. Each pinned sensor's icon has its own
        menu, so it can always be unpinned from there, even after it has stopped reporting."""
        menu = QMenu(self)
        if key == APP_TRAY:
            # Ubuntu's panel opens this menu on a click, so opening the window has to be here.
            menu.addAction(self.open_action)
            menu.addSeparator()
            self._add_display_options(menu)
            self._add_options(menu)
            return menu
        # Ubuntu's panel shows no tooltips, so the name, min, max and average are also
        # listed here, where every tray can show them. Updated with the tooltip.
        info = [menu.addAction("") for _ in range(TRAY_INFO_LINES)]
        menu.insertSeparator(info[1])  # between the group and the sensor
        for action in info:
            action.setEnabled(False)
        self._tray_info[key] = info
        menu.addSeparator()
        unpin = menu.addAction("Unpin this sensor", lambda: self.set_pinned(key, False))
        menu.setProperty("unpin", unpin)
        return menu

    def set_pinned(self, key: str | None, pinned: bool) -> None:
        if key is None or (key in self.pinned) == pinned:
            return
        if pinned:
            self.pinned.append(key)
        else:
            self.pinned.remove(key)
        self.settings.setValue("pinned", json.dumps(self.pinned))
        self._update_tray()
        self._show_detail()

    # ----- per-sensor menu: pin and rename --------------------------------------------

    def sensor_menu(self, key: str) -> QMenu:
        menu = QMenu(self)
        pin = menu.addAction("Pin to tray")
        pin.setCheckable(True)
        pin.setChecked(key in self.pinned)
        pin.setEnabled(self._tray_factory is not None)
        if self._tray_factory is None:
            pin.setToolTip("No system tray is available")
        pin.toggled.connect(lambda checked: self.set_pinned(key, checked))
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self._rename_interactively(key))
        reset = menu.addAction("Reset name", lambda: self.rename_sensor(key, None))
        reset.setEnabled(key in self.names)
        return menu

    def _show_sensor_menu(self, key: str, position: QPoint) -> None:
        self._select(key)  # but leave the drawer as it is
        menu = self.sensor_menu(key)
        self._popup(menu, position)
        menu.deleteLater()  # one is built per right-click; don't keep them all

    def _popup(self, menu: QMenu, position: QPoint) -> None:
        menu.exec(position)

    def _ask_name(self, current: str) -> str | None:
        text, accepted = QInputDialog.getText(self, "Rename sensor", "Name:", text=current)
        return text if accepted else None

    def _rename_interactively(self, key: str) -> None:
        row = self.rows.get(key)
        name = self._ask_name(row.reading.label if row else "")
        if name is not None:
            self.rename_sensor(key, name)

    def rename_sensor(self, key: str, name: str | None) -> None:
        """Give a sensor your own name; None or an empty name goes back to the original."""
        name = (name or "").strip()
        if name:
            self.names[key] = name
        else:
            self.names.pop(key, None)
        self.settings.setValue("names", json.dumps(self.names, sort_keys=True))
        if self.raw_rows:
            self.show_rows(self.raw_rows)  # same readings, new labels; no extra sample

    def _present(self, rows: list[Row]) -> list[Row]:
        """Rows as shown: every fan in the Fans card, and your own names applied."""
        presented = gather_cards(rows)
        if not self.names:
            return presented
        return [
            Row(replace(row.reading, label=self.names[row.reading.key]), row.stats)
            if row.reading.key in self.names
            else row
            for row in presented
        ]

    def _load_list(self, name: str) -> list[str]:
        try:
            stored = json.loads(str(self.settings.value(name, "[]")))
        except ValueError:
            return []
        return [str(item) for item in stored] if isinstance(stored, list) else []

    def _load_names(self) -> dict[str, str]:
        try:
            stored = json.loads(str(self.settings.value("names", "{}")))
        except ValueError:
            return {}
        if not isinstance(stored, dict):
            return {}
        return {str(key): str(name) for key, name in stored.items() if str(name).strip()}

    def show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def quit(self) -> None:
        self._quitting = True
        self.close()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        # When the desktop session is ending, close for real so we never block a logout.
        app = QGuiApplication.instance()
        ending_session = isinstance(app, QGuiApplication) and app.isSavingSession()
        if (
            self.close_to_tray
            and self._tray_factory is not None  # corewatch's own icon is there to come back with
            and not self._quitting
            and not ending_session
        ):
            self.hide()
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.shutdown()
        event.accept()
        if not self._quitting:
            QApplication.quit()

    def shutdown(self) -> bool:
        """Stop sampling and wait for the worker to finish its current reading. Idempotent.

        Returns False if a reading is stuck (a hung GPU driver, a drive in error recovery):
        the worker is then left alone rather than waited on again.
        """
        self._stopped = True
        self.timer.stop()
        if self.worker_stuck:
            return False
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(SHUTDOWN_WAIT_MS):
                self.worker_stuck = True
        return not self.worker_stuck


def run_gui(
    interval: float | None = None, fahrenheit: bool | None = None, minimized: bool = False, at_login: bool = False
) -> int:
    # "corewatch" for argv[0]: started as python3 -m corewatch (the .deb), it would be __main__.py,
    # which X11 window managers would take as the app's name.
    app = QApplication(["corewatch", *sys.argv[1:]])
    app.setApplicationName("corewatch")
    app.setOrganizationName("corewatch")
    app.setDesktopFileName("corewatch")
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    themes.load_fonts()
    app.setQuitOnLastWindowClosed(False)  # the tray may keep us alive; quitting is explicit
    # Read now: once the window applies a chosen theme, the hint reports that instead.
    light_panel = panel_is_light(os.environ.get("XDG_CURRENT_DESKTOP", ""), app.styleHints().colorScheme())
    # Already running? Bring that one forward (unless this start wants to stay out of sight)
    # and stop. Decided before anything slow, so two starts a moment apart can't both run.
    lock_path, socket_path = single.default_paths()
    lock, answered = single.claim(lock_path, socket_path, show=not (minimized or at_login))
    if lock is None:
        if not answered:
            print("corewatch: another corewatch is running but not answering", file=sys.stderr)
        return 0
    monitor = Monitor(default_sources())
    window = MainWindow(monitor, QSettings(), interval=interval, fahrenheit=fahrenheit)
    window.light_panel = light_panel
    single.InstanceServer(socket_path, window.show_window, window)
    app.aboutToQuit.connect(window.shutdown)
    with contextlib.suppress(autostart.AutostartError, OSError):
        autostart.refresh(window.autostart_file)  # keep our login entry pointing at this install
    window.start(
        minimized or window.start_minimized,
        QSystemTrayIcon.isSystemTrayAvailable,
        lambda: QSystemTrayIcon(window),
        at_login=at_login,
    )
    code = 1
    try:
        code = app.exec()
    finally:
        finish(window, monitor, code)
        lock.release()
    return code


def finish(window: MainWindow, monitor: Monitor, code: int) -> None:
    """Release the sensors once the worker has stopped reading them.

    If a reading never returned, closing the sources would race the call still inside
    the driver, and Qt aborts when a running thread's object is destroyed. Save the
    settings and leave without any cleanup instead.
    """
    if window.shutdown():
        monitor.close()
        return
    window.settings.sync()
    print("corewatch: a sensor read never returned; exiting without cleanup", file=sys.stderr)
    os._exit(code)
