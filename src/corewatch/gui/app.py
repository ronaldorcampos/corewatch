"""Desktop window and tray icon built on Qt (PySide6)."""

import contextlib
import json
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QPoint, QSettings, QSignalBlocker, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
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
from corewatch.gui.sensors import CategorySection
from corewatch.gui.widgets import DetailPanel
from corewatch.model import Kind, Row, Status, format_short, format_value
from corewatch.monitor import Collected, Monitor, gather_fans, group_rows, headline
from corewatch.sources import default_sources

# How long closing waits for a reading in flight before giving up on it.
SHUTDOWN_WAIT_MS = 3000

# Lines at the top of a pinned icon's menu: group, name, min, max, average.
TRAY_INFO_LINES = 5
# The logo's size in the toolbar, where the app name used to be.
LOGO_SIZE = 28
# Key of the tray icon shown when no sensor is pinned: the CPU temperature.
DEFAULT_TRAY = ""

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


def number_icon(text: str, status: Status) -> QIcon:
    """A tray icon showing a number, like Core Temp does, coloured by status."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    background = {Status.OK: "#2563eb", Status.WARNING: "#d97706", Status.CRITICAL: "#dc2626"}[status]
    painter.setBrush(QColor(background))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, 64, 64, 14, 14)
    font = QFont()
    font.setBold(True)
    font.setPixelSize({1: 40, 2: 40, 3: 30, 4: 23}.get(len(text), 19))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return QIcon(pixmap)


def temperature_icon(celsius: float | None, status: Status, fahrenheit: bool) -> QIcon:
    return number_icon(format_short(Kind.TEMPERATURE, celsius, fahrenheit), status)


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
        collapsed = settings.value("collapsed", [], type=list)
        self.collapsed: set[str] = {str(d) for d in collapsed} if isinstance(collapsed, list) else set()
        self.selected_key: str | None = str(settings.value("selected", "")) or None
        self.pinned: list[str] = self._load_list("pinned")
        self.names: dict[str, str] = self._load_names()

        self.rows: dict[str, Row] = {}
        self.ordered: list[Row] = []
        self.all_rows: list[Row] = []
        self.plain_ordered: list[Row] = []
        self._devices: list[str] = []
        self.sections: dict[str, CategorySection] = {}
        self.gap = 5.0
        self.raw_rows: list[Row] = []
        self._tray_factory: Callable[[], QSystemTrayIcon] | None = None
        self._tray_menus: dict[str, QMenu] = {}
        self._tray_info: dict[str, list[QAction]] = {}
        self.trays: dict[str, QSystemTrayIcon] = {}
        self._quitting = False

        self.setWindowTitle("corewatch")
        self.setWindowIcon(app_icon())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._request_sample)
        self._sampling = False
        self._stopped = False
        self.worker_stuck = False
        self._tray_states: dict[str, tuple[str, Status, str]] = {}
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
        toolbar = QToolBar("Controls", self)
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
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter sensors  (Ctrl+F)")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMinimumWidth(240)
        self.filter.textChanged.connect(lambda _: self._apply_filter())
        self._filter_folds: dict[str, bool] = {}
        toolbar.addWidget(self.filter)
        focus_filter = QAction(self)
        focus_filter.setShortcut("Ctrl+F")
        focus_filter.triggered.connect(lambda: self.filter.setFocus())
        self.addAction(focus_filter)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

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
            button.setCheckable(True)
            button.setChecked(is_f == self.fahrenheit)
            button.setToolTip("Show temperatures in " + ("Fahrenheit" if is_f else "Celsius"))
            button.clicked.connect(lambda _=False, value=is_f: self.set_fahrenheit(value))
            self.unit_buttons.addButton(button)
            toolbar.addWidget(button)

        self.min_max_button = QPushButton("Min / max")
        self.min_max_button.setCheckable(True)
        self.min_max_button.setChecked(self.show_min_max)
        self.min_max_button.setToolTip("Show the lowest and highest value of each sensor in the list")
        self.min_max_button.toggled.connect(self.set_show_min_max)
        toolbar.addWidget(self.min_max_button)

        self.reset_button = QPushButton("Reset min/max")
        self.reset_button.setToolTip("Forget every recorded lowest, highest and average value (Ctrl+R)")
        self.reset_button.setShortcut("Ctrl+R")
        self.reset_button.clicked.connect(self.reset_stats)
        toolbar.addWidget(self.reset_button)

        self.menu_button = QToolButton()
        self.menu_button.setText("⋯")
        self.menu_button.setToolTip("Options")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.menu_button)
        theme_menu = menu.addMenu("Theme")
        self.theme_actions = QActionGroup(self)
        for key, text in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            action = theme_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(key == self.theme)
            action.setData(key)
            self.theme_actions.addAction(action)
        self.theme_actions.triggered.connect(lambda action: self.apply_theme(str(action.data())))
        self.unused_action = menu.addAction("Show unused sensors")
        self.unused_action.setCheckable(True)
        self.unused_action.setChecked(self.show_unused)
        self.unused_action.setToolTip(
            "Also list inputs with nothing attached: empty fan headers and unconnected temperature probes"
        )
        menu.setToolTipsVisible(True)
        self.unused_action.toggled.connect(self.set_show_unused)
        self.expand_action = menu.addAction("Expand all", self._expand_all)
        self.collapse_action = menu.addAction("Collapse all", self._collapse_all)
        menu.addSeparator()
        self.tray_action = menu.addAction("Keep running in the tray when closed")
        self.tray_action.setCheckable(True)
        self.tray_action.setChecked(self.close_to_tray)
        self.tray_action.toggled.connect(self._set_close_to_tray)
        self.autostart_action = menu.addAction("Start when I log in")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setToolTip("Start corewatch each time you log in to your desktop")
        menu.aboutToShow.connect(self._sync_autostart)  # it can be changed outside corewatch too
        self._sync_autostart()
        self.autostart_action.toggled.connect(self.set_autostart)
        self.minimized_action = menu.addAction("Start minimized in the tray")
        self.minimized_action.setCheckable(True)
        self.minimized_action.setChecked(self.start_minimized)
        self.minimized_action.setToolTip(
            "Start without opening this window; click the tray icon to open it. "
            "Applies at login too. Without a system tray the window always opens."
        )
        self.minimized_action.toggled.connect(self._set_start_minimized)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.quit)
        self.addAction(quit_action)
        self.menu_button.setMenu(menu)
        toolbar.addWidget(self.menu_button)

        central = QWidget()
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

        self.list_area = QScrollArea()
        self.list_area.setObjectName("listArea")
        self.list_area.setWidgetResizable(True)
        self.list_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.list_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setObjectName("listContainer")
        self.sections_layout = QVBoxLayout(container)
        self.sections_layout.setContentsMargins(0, 0, 4, 0)
        self.sections_layout.setSpacing(12)
        self.sections_layout.addStretch(1)
        self.list_area.setWidget(container)

        self.detail = DetailPanel()
        self.detail.window_changed.connect(lambda seconds: self._show_detail())
        self.detail.pin_toggled.connect(lambda pinned: self.set_pinned(self.selected_key, pinned))
        stored_window = self.settings.value("detail_window", 300.0)
        with contextlib.suppress(ValueError):
            self.detail.set_window(self.detail.nearest_window(float(str(stored_window))))
        self.detail.window_changed.connect(lambda seconds: self.settings.setValue("detail_window", seconds))

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.list_area)
        self.splitter.addWidget(self.detail)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        self.splitter.setSizes([520, 360])
        layout.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

    # ----- settings -----------------------------------------------------------------

    def set_interval(self, seconds: float, remember: bool = True) -> None:
        seconds = float(seconds)
        if not math.isfinite(seconds):
            seconds = 1.0
        self.interval = min(max(seconds, MIN_INTERVAL), MAX_INTERVAL)
        self.timer.start(round(self.interval * 1000))
        # A sample is "missing" (draw a gap) only if it is well overdue.
        self.gap = self.interval * 3 + 1
        if remember:
            self.settings.setValue("interval", self.interval)

    def set_show_unused(self, show: bool) -> None:
        self.show_unused = show
        self.settings.setValue("show_unused", show)
        self.ordered = self.monitor.visible_rows(self.all_rows, show)
        self.plain_ordered = self.monitor.visible_rows(gather_fans(self.raw_rows), show)
        self.redraw()

    def set_show_min_max(self, show: bool) -> None:
        self.show_min_max = show
        self.settings.setValue("show_min_max", show)
        for section in self.sections.values():
            section.grid.set_show_min_max(show)

    def set_fahrenheit(self, fahrenheit: bool) -> None:
        self.fahrenheit = fahrenheit
        self.celsius_button.setChecked(not fahrenheit)
        self.fahrenheit_button.setChecked(fahrenheit)
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
        if isinstance(app, QApplication):
            themes.apply(app, themes.current_theme())
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
            self.attach_tray(tray_factory)
            if not minimized:
                self.show_window()
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
            "Start without opening this window; click the tray icon to open it. Applies at login too."
            if available
            else "Needs a system tray, and this desktop doesn't have one right now"
        )

    def _set_close_to_tray(self, enabled: bool) -> None:
        self.close_to_tray = enabled
        self.settings.setValue("close_to_tray", enabled)

    def _on_fold(self, device: str, collapsed: bool) -> None:
        """A click on a card's chevron: remembered, unless it was made while filtering."""
        if self.filter.text().strip():
            self._filter_folds[device] = collapsed
        else:
            self._remember_collapse(device, collapsed)

    def _remember_collapse(self, device: str, collapsed: bool) -> None:
        if collapsed:
            self.collapsed.add(device)
        else:
            self.collapsed.discard(device)
        self.settings.setValue("collapsed", sorted(self.collapsed))

    def _expand_all(self) -> None:
        for device, section in self.sections.items():
            section.set_collapsed(False)
            self._remember_collapse(device, False)

    def _collapse_all(self) -> None:
        for device, section in self.sections.items():
            section.set_collapsed(True)
            self._remember_collapse(device, True)

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
        self.plain_ordered = self.monitor.visible_rows(gather_fans(raw), self.show_unused)
        notes = self.monitor.notes()
        self.notes.setText("\n".join(f"•  {note}" for note in notes))
        self.notes.setVisible(bool(notes))
        self.redraw()
        unused = len(rows) - len(self.ordered)
        hidden = f" ({unused} unused hidden)" if unused else ""
        self.statusBar().showMessage(
            f"{len(self.ordered)} sensors{hidden}  ·  updated {time.strftime('%H:%M:%S')}  ·  every {self.interval:g} s"
        )

    def redraw(self) -> None:
        text = self.filter.text()
        filtering = bool(text.strip())
        groups = [
            (device, [r for r in rows if matches(text, device, r.reading.label)])
            for device, rows in group_rows(self.ordered)
        ]
        devices = [device for device, _ in groups]
        if devices != self._devices:
            self._rebuild(devices)
        now = self.monitor.last_sample_at or self.monitor.clock()
        for device, rows in groups:
            section = self.sections[device]
            section.setVisible(bool(rows))
            # While filtering, open every matching card without touching the remembered state.
            if filtering:
                # Cards open while filtering; a fold made during the search lasts until it ends.
                section.set_collapsed(self._filter_folds.get(device, False))
            else:
                section.set_collapsed(device in self.collapsed)
            section.set_rows(rows, now, self.fahrenheit, self.gap)
        if self.selected_key not in {row.reading.key for row in self.ordered}:  # gone, or now hidden
            best = headline(self.plain_ordered)
            self.selected_key = best.reading.key if best else next(iter(self.rows), None)
        for section in self.sections.values():
            section.grid.set_selected(self.selected_key)
        self._show_detail()
        self._update_tray()

    def _rebuild(self, devices: list[str]) -> None:
        """Create a card per device, reusing existing cards so their state survives."""
        for device in list(self.sections):
            if device not in devices:
                self.sections.pop(device).deleteLater()
        for index, device in enumerate(devices):
            section = self.sections.get(device)
            if section is None:
                section = CategorySection(device, device in self.collapsed)
                section.collapse_toggled.connect(self._on_fold)
                section.grid.selected.connect(self._select)
                section.grid.context_requested.connect(self._show_sensor_menu)
                section.grid.set_show_min_max(self.show_min_max)
                self.sections[device] = section
            self.sections_layout.insertWidget(index, section)
        self._devices = devices

    def _apply_filter(self) -> None:
        if not self.filter.text().strip():
            self._filter_folds.clear()
        self.redraw()

    def _select(self, key: str) -> None:
        self.selected_key = key
        self.settings.setValue("selected", key)
        for section in self.sections.values():
            section.grid.set_selected(key)
        self._show_detail()

    def _show_detail(self) -> None:
        row = self.rows.get(self.selected_key) if self.selected_key else None
        now = self.monitor.last_sample_at or self.monitor.clock()
        pinned = self.selected_key in self.pinned
        can_pin = self._tray_factory is not None
        self.detail.show_row(row, now, self.fahrenheit, self.gap, self.monitor.to_wall, pinned, can_pin)

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

    def _tray_content(self, key: str) -> tuple[str, Status, str]:
        """(number on the icon, status, tooltip) for one tray icon."""
        if key == DEFAULT_TRAY:
            best = headline(self.plain_ordered)
            reading = best.reading if best else None
            text = format_short(Kind.TEMPERATURE, reading.value if reading else None, self.fahrenheit)
            return text, reading.status if reading else Status.OK, "\n".join(["corewatch", *self._summary()])
        row = self.rows.get(key)
        if row is None:  # the sensor went away (driver unloaded, GPU asleep)
            return "?", Status.OK, "corewatch\nThis pinned sensor isn't reporting right now"
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
        return format_short(reading.kind, reading.value, self.fahrenheit), reading.status, tooltip

    def _update_tray(self) -> None:
        """One icon per pinned sensor (the CPU temperature when nothing is pinned)."""
        if self._tray_factory is None:
            return
        wanted = list(self.pinned) or [DEFAULT_TRAY]
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
                tray.activated.connect(
                    lambda reason: self.toggle_visible() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
                )
            text, status, tooltip = self._tray_content(key)
            unpin = self._tray_menus[key].property("unpin")
            if isinstance(unpin, QAction):
                row = self.rows.get(key)
                unpin.setText(f"Unpin {row.reading.label}" if row else "Unpin this sensor")
            # Each update is a D-Bus round trip to the tray host, so only send real changes.
            if self._tray_states.get(key) != (text, status, tooltip):
                self._tray_states[key] = (text, status, tooltip)
                lines = tooltip.splitlines()
                for index, action in enumerate(self._tray_info.get(key, [])):
                    action.setText(lines[index] if index < len(lines) else "")
                    action.setVisible(index < len(lines))
                tray.setIcon(number_icon(text, status))
                tray.setToolTip(tooltip)
            if not tray.isVisible():
                tray.show()

    def attach_tray(self, factory: Callable[[], QSystemTrayIcon]) -> None:
        """``factory`` makes one tray icon; it's called once per pinned sensor."""
        self._tray_factory = factory
        self._update_tray()
        self._show_detail()  # pinning is possible now
        self._set_tray_options_available(True)

    def _tray_menu_for(self, key: str) -> QMenu:
        """Each icon gets its own menu, so a pinned sensor can always be unpinned from its
        icon, even after the sensor itself has stopped reporting."""
        menu = QMenu(self)
        if key != DEFAULT_TRAY:
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
            menu.addSeparator()
        menu.addAction("Show / hide window", self.toggle_visible)
        menu.addAction("Reset min/max", self.reset_stats)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
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
        presented = gather_fans(rows)
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

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_window()

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
            and any(tray.isVisible() for tray in self.trays.values())
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
    app = QApplication(sys.argv)
    app.setApplicationName("corewatch")
    app.setOrganizationName("corewatch")
    app.setDesktopFileName("corewatch")
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)  # the tray may keep us alive; quitting is explicit
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
