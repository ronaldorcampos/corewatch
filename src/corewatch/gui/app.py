"""Desktop window and tray icon built on Qt (PySide6)."""

import contextlib
import math
import os
import sys
import time

from PySide6.QtCore import QByteArray, QObject, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
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

from corewatch.cli import MAX_INTERVAL, MIN_INTERVAL
from corewatch.gui import theme as themes
from corewatch.gui.sensors import CategorySection
from corewatch.gui.widgets import DetailPanel
from corewatch.model import Kind, Row, Status, format_value
from corewatch.monitor import Collected, Monitor, group_rows, headline
from corewatch.sources import default_sources

# How long closing waits for a reading in flight before giving up on it.
SHUTDOWN_WAIT_MS = 3000

INTERVAL_PRESETS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

THEMES = {
    "system": Qt.ColorScheme.Unknown,
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
}


def temperature_icon(celsius: float | None, status: Status, fahrenheit: bool) -> QIcon:
    """A tray icon showing the temperature as a number, like Core Temp does."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    background = {Status.OK: "#2563eb", Status.WARNING: "#d97706", Status.CRITICAL: "#dc2626"}[status]
    painter.setBrush(QColor(background))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, 64, 64, 14, 14)
    text = "?" if celsius is None else f"{(celsius * 9 / 5 + 32) if fahrenheit else celsius:.0f}"
    font = QFont()
    font.setBold(True)
    font.setPixelSize(40 if len(text) <= 2 else 28)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return QIcon(pixmap)


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
    ) -> None:
        super().__init__()
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
        self.show_min_max = bool(settings.value("show_min_max", True, type=bool))
        collapsed = settings.value("collapsed", [], type=list)
        self.collapsed: set[str] = {str(d) for d in collapsed} if isinstance(collapsed, list) else set()
        self.selected_key: str | None = str(settings.value("selected", "")) or None

        self.rows: dict[str, Row] = {}
        self.ordered: list[Row] = []
        self.all_rows: list[Row] = []
        self._devices: list[str] = []
        self.sections: dict[str, CategorySection] = {}
        self.gap = 5.0
        self.tray: QSystemTrayIcon | None = None
        self._quitting = False

        self.setWindowTitle("corewatch")
        self.setWindowIcon(QIcon.fromTheme("utilities-system-monitor", temperature_icon(None, Status.OK, False)))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._request_sample)
        self._sampling = False
        self._stopped = False
        self.worker_stuck = False
        self._tray_state: tuple[str, Status, str] | None = None
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

        title = QLabel("corewatch")
        title.setObjectName("appTitle")
        toolbar.addWidget(title)
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

    def show_rows(self, rows: list[Row]) -> None:
        self.rows.clear()
        self.rows.update({row.reading.key: row for row in rows})
        self.all_rows = rows
        self.ordered = self.monitor.visible_rows(rows, self.show_unused)
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
            best = headline(self.ordered)
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
        self.detail.show_row(row, now, self.fahrenheit, self.gap, self.monitor.to_wall)

    # ----- tray & lifecycle -----------------------------------------------------------

    def _summary(self) -> list[str]:
        def first(device_prefix: str, kind: Kind, label_prefix: str = "") -> Row | None:
            return next(
                (
                    r
                    for r in self.ordered
                    if r.reading.device.startswith(device_prefix)
                    and r.reading.kind is kind
                    and r.reading.label.startswith(label_prefix)
                ),
                None,
            )

        lines = []
        for name, temp, load in (
            ("CPU", headline(self.ordered), first("CPU", Kind.LOAD, "CPU load")),
            ("GPU", first("GPU", Kind.TEMPERATURE), first("GPU", Kind.LOAD, "GPU load")),
        ):
            if temp is None:
                continue
            line = f"{name} {format_value(Kind.TEMPERATURE, temp.reading.value, self.fahrenheit)}"
            if load is not None and load.reading.value is not None:
                line += f" · {load.reading.value:.0f}% load"
            lines.append(line)
        return lines

    def _update_tray(self) -> None:
        if self.tray is None:
            return
        best = headline(self.ordered)
        reading = best.reading if best else None
        celsius = reading.value if reading else None
        status = reading.status if reading else Status.OK
        tooltip = "\n".join(["corewatch", *self._summary()])
        number = "?" if celsius is None else f"{(celsius * 9 / 5 + 32) if self.fahrenheit else celsius:.0f}"
        # Each update is a D-Bus round trip to the tray host, so only send real changes.
        if (number, status, tooltip) == self._tray_state:
            return
        self._tray_state = (number, status, tooltip)
        self.tray.setIcon(temperature_icon(celsius, status, self.fahrenheit))
        self.tray.setToolTip(tooltip)

    def attach_tray(self, tray: QSystemTrayIcon) -> None:
        self.tray = tray
        menu = QMenu(self)
        menu.addAction("Show / hide window", self.toggle_visible)
        menu.addAction("Reset min/max", self.reset_stats)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self.toggle_visible() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self._update_tray()
        tray.show()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
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
            and self.tray is not None
            and self.tray.isVisible()
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


def run_gui(interval: float | None = None, fahrenheit: bool | None = None) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("corewatch")
    app.setOrganizationName("corewatch")
    app.setDesktopFileName("corewatch")
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)  # the tray may keep us alive; quitting is explicit
    monitor = Monitor(default_sources())
    window = MainWindow(monitor, QSettings(), interval=interval, fahrenheit=fahrenheit)
    app.aboutToQuit.connect(window.shutdown)
    if QSystemTrayIcon.isSystemTrayAvailable():
        window.attach_tray(QSystemTrayIcon(window))
    window.show()
    code = 1
    try:
        code = app.exec()
    finally:
        finish(window, monitor, code)
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
