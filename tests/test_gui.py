import json
import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from fakes import FakeSource, load, temp
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel

from corewatch.gui import theme
from corewatch.gui.app import MainWindow, temperature_icon
from corewatch.gui.sensors import CELL_MIN_W, COLUMN_GAP, short_label
from corewatch.gui.widgets import nice_ticks, split_on_gaps, value_range
from corewatch.model import Kind, Status, format_duration
from corewatch.monitor import Monitor


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@pytest.fixture(scope="module", autouse=True)
def bundled_fonts(qapp):  # type: ignore[no-untyped-def]
    """Measure and paint every GUI test in the fonts the app really uses, at the designed text size
    whatever the desktop running the tests is set to."""
    families = theme.load_fonts()
    theme._text_scale = 1.0
    return families


@pytest.fixture
def settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "corewatch.ini"), QSettings.Format.IniFormat)


def make_window(qtbot, settings: QSettings, script, **kwargs) -> MainWindow:  # type: ignore[no-untyped-def]
    monitor = Monitor([FakeSource("s", script)], clock=Clock())
    window = MainWindow(monitor, settings, **kwargs)
    qtbot.addWidget(window)
    window.timer.stop()  # tests drive refresh() by hand
    return window


SCRIPT = [
    [
        temp("gpu", "GPU · RTX", 41.0, label="GPU temperature"),
        temp("pkg", "CPU · i7", 50.0, label="CPU package", high=80.0, crit=100.0),
        load("cpu-load", "CPU · i7", 5.0, label="CPU load"),
    ],
    [
        temp("gpu", "GPU · RTX", 43.0, label="GPU temperature"),
        temp("pkg", "CPU · i7", 85.0, label="CPU package", high=80.0, crit=100.0),
        load("cpu-load", "CPU · i7", 15.0, label="CPU load"),
    ],
]


def texts(window: MainWindow, key: str) -> list[str]:
    section = next(s for s in window.sections.values() if any(c.key == key for c in s.grid.cells()))
    return section.grid.cell_texts(key)


def visible_keys(window: MainWindow) -> set[str]:
    return {c.key for s in window.sections.values() if not s.isHidden() for c in s.grid.cells()}


def test_cards_group_sensors_and_show_stats(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    assert list(window.sections) == ["CPU · i7", "GPU · RTX"]
    window.refresh()
    assert texts(window, "pkg") == ["CPU package", "85.0 °C", "50.0 °C", "85.0 °C", "67.5 °C"]
    assert window.sections["CPU · i7"].count.text() == "2 sensors"
    grid = window.sections["CPU · i7"].grid
    assert [(kind, [r.reading.key for r in rows]) for kind, rows in grid.blocks()] == [
        (Kind.TEMPERATURE, ["pkg"]),
        (Kind.LOAD, ["cpu-load"]),
    ]


def test_detail_panel_defaults_to_cpu_package_and_follows_clicks(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.resize(1200, 900)
    window.show()
    window.refresh()
    detail = window.detail
    assert detail.title.text() == "CPU package"
    assert detail.stat_values["highest"].text().startswith("85.0 °C")
    assert "samples" not in detail.stat_values and "watching" not in detail.stat_values
    assert detail.stat_values["past_limit"].text() == "0 s"  # charged once the next sample arrives
    gpu_grid = window.sections["GPU · RTX"].grid
    [cell] = gpu_grid.cells()
    qtbot.mouseClick(gpu_grid, Qt.MouseButton.LeftButton, pos=cell.rect.center())
    assert detail.title.text() == "GPU temperature"
    assert detail.stat_values["critical"].text() == "no limit"
    assert settings.value("selected") == "gpu"
    assert gpu_grid.selected_key == "gpu"
    assert window.sections["CPU · i7"].grid.selected_key == "gpu"  # no second highlight elsewhere
    assert gpu_grid.key_at(QPoint(-5, -5)) is None


def test_unit_toggle_filter_and_reset(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.refresh()
    window.fahrenheit_button.click()
    assert texts(window, "pkg")[1] == "185.0 °F"
    assert window.detail.value.text() == "185.0 °F"
    assert settings.value("fahrenheit", type=bool) is True

    window.filter.setText("gpu")
    assert visible_keys(window) == {"gpu"}
    window.filter.setText("cpu ·")  # device name matches -> all its sensors show
    assert visible_keys(window) == {"pkg", "cpu-load"}
    assert window.sections["GPU · RTX"].isHidden()

    window.filter.clear()
    window.reset_action.trigger()
    assert texts(window, "pkg")[2] == "—"  # statistics start over at the next reading


def test_cards_use_two_columns_or_one_when_narrow(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    grid = window.sections["CPU · i7"].grid
    assert grid.columns == 2
    need = grid.cell_min_width()
    assert grid.effective_columns(need * 2 + COLUMN_GAP) == 2
    assert grid.effective_columns(need * 2 + COLUMN_GAP - 1) == 1
    assert not hasattr(window.sections["CPU · i7"], "column_buttons")  # no per-card picker


def test_number_columns_grow_to_fit_and_never_shrink_mid_session(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    def fan(rpm: float) -> list[Reading]:
        return [Reading("f", "Motherboard", "Fan 2", Kind.FAN, rpm, empty_if_idle=True)]

    window = make_window(qtbot, settings, [fan(900.0), fan(12_345.0), fan(900.0)])
    grid = window.sections["Fans"].grid
    narrow = grid.stat_widths["value"]
    window.refresh()  # 12,345 RPM is wider than 900 RPM
    wide = grid.stat_widths["value"]
    assert wide > narrow
    window.refresh()  # back to 900: the column keeps its width so nothing jiggles
    assert grid.stat_widths["value"] == wide
    assert grid.cell_min_width() > CELL_MIN_W - 4 * 64  # driven by the measured widths


def test_cells_fill_each_column_top_to_bottom(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    script = [[temp(f"t{i}", "CPU", 40.0 + i) for i in range(5)]]
    window = make_window(qtbot, settings, script)
    grid = window.sections["CPU"].grid
    grid.resize(CELL_MIN_W * 2 + COLUMN_GAP + 40, 10)
    grid.set_columns(2)
    by_column: dict[int, list[str]] = {}
    for cell in grid.cells():
        by_column.setdefault(cell.rect.x(), []).append(cell.key)
    assert list(by_column.values()) == [["t0", "t1", "t2"], ["t3", "t4"]]


def test_short_labels_under_kind_headings() -> None:
    from corewatch.model import Row

    assert short_label(Row(load("k", "CPU", 3.0, label="P-core 3 load"))) == "P-core 3"
    assert short_label(Row(load("k", "CPU", 3.0, label="CPU load (all cores)"))) == "CPU load (all cores)"
    assert short_label(Row(temp("k", "CPU", 3.0, label="Core load"))) == "Core load"  # only for its own kind


def test_settings_round_trip(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.interval_box.setCurrentIndex(window.interval_box.findData(5.0))
    assert window.interval == 5.0
    assert window.timer.interval() == 5000
    window.apply_theme("dark")
    assert theme.current_theme() is theme.DARK
    reopened = make_window(qtbot, settings, SCRIPT)
    assert (reopened.interval, reopened.theme) == (5.0, "dark")
    window.apply_theme("light")


def test_cli_interval_overrides_and_is_offered(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT, interval=1.5)
    assert window.interval_box.currentData() == 1.5
    assert window.timer.interval() == 1500


def test_corrupt_settings_fall_back_to_defaults(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    settings.setValue("interval", "banana")
    settings.setValue("theme", "neon")
    window = make_window(qtbot, settings, SCRIPT)
    assert (window.interval, window.theme) == (1.0, "system")


def test_tray_icon_draws_status_colour(qapp) -> None:  # type: ignore[no-untyped-def]
    image = temperature_icon(85.0, Status.CRITICAL, False).pixmap(64, 64).toImage()
    assert isinstance(image, QImage)
    assert image.pixelColor(32, 4).name() == "#dc2626"


def test_chart_helpers() -> None:
    assert nice_ticks(46.2, 63.7) == [45.0, 50.0, 55.0, 60.0, 65.0]
    assert nice_ticks(1.18, 1.26) == [1.18, 1.2, 1.22, 1.24, 1.26]
    low, high = value_range([50.0, 50.0], Kind.TEMPERATURE)
    assert high - low == pytest.approx(4.0 * 1.15)  # flat line still gets a calm minimum span
    assert value_range([0.0, 2.0], Kind.LOAD)[0] == 0.0  # load can't go negative
    assert split_on_gaps([(0, 1), (1, 1), (10, 1), (11, 1)], gap=3) == [[(0, 1), (1, 1)], [(10, 1), (11, 1)]]


def test_unused_sensors_hidden_until_toggled(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    script = [[temp("t", "CPU · i7", 50.0), Reading("fan1", "Motherboard", "Fan 1", Kind.FAN, 0.0, empty_if_idle=True)]]
    window = make_window(qtbot, settings, script)
    assert "fan1" not in visible_keys(window)
    assert "1 unused hidden" in window.statusBar().currentMessage()
    window.unused_action.setChecked(True)
    assert "fan1" in visible_keys(window)
    assert settings.value("show_unused", type=bool) is True


def test_background_sampling_updates_the_window(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    assert texts(window, "pkg")[1] == "50.0 °C"
    with qtbot.waitSignal(window._sampler.collected, timeout=3000):
        window._request_sample()
    qtbot.waitUntil(lambda: texts(window, "pkg")[1] == "85.0 °C", timeout=3000)
    assert window._sampling is False


def test_a_tick_is_skipped_while_a_reading_is_in_flight(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    emitted = []
    window.sample_requested.connect(lambda: emitted.append(1))
    window._sampling = True
    window._request_sample()
    window.refresh()  # the worker owns the sources: no reading on this thread either
    assert emitted == []
    assert window.monitor.sources[0].calls == 1  # only the startup reading


def test_reset_clears_stats_without_taking_a_reading(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.refresh()
    calls = window.monitor.sources[0].calls
    window.reset_action.trigger()
    assert window.monitor.sources[0].calls == calls
    assert texts(window, "pkg")[1:] == ["85.0 °C", "—", "—", "—"]  # value stays, statistics start over
    assert window.detail.stat_values["average"].text() == "—"


@pytest.mark.parametrize("stored", ["nan", "inf", "-inf"])
def test_non_finite_interval_in_settings_falls_back(qtbot, settings, stored) -> None:  # type: ignore[no-untyped-def]
    settings.setValue("interval", stored)
    window = make_window(qtbot, settings, SCRIPT)
    assert window.interval == 1.0


@pytest.mark.parametrize(
    ("stored", "expected"), [("0", 60.0), ("-60", 60.0), ("120", 60.0), ("700", 900.0), ("nan", 300.0)]
)
def test_detail_window_from_settings_snaps_to_an_offered_choice(qtbot, settings, stored, expected) -> None:  # type: ignore[no-untyped-def]
    settings.setValue("detail_window", stored)
    window = make_window(qtbot, settings, SCRIPT)
    assert window.detail.window_seconds == expected
    assert sum(b.isChecked() for b in window.detail.window_buttons.buttons()) == 1
    window.detail.chart.grab()  # paints without dividing by zero


def test_command_line_interval_is_not_saved(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    make_window(qtbot, settings, SCRIPT, interval=0.25)
    assert settings.value("interval") is None
    window = make_window(qtbot, settings, SCRIPT)
    window.interval_box.setCurrentIndex(window.interval_box.findData(2.0))
    assert float(settings.value("interval")) == 2.0  # choosing in the window is remembered


class FakeTray(QObject):
    activated = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.icons = 0
        self.tooltips: list[str] = []
        self.visible = False

    def setContextMenu(self, menu) -> None:  # type: ignore[no-untyped-def]
        self.menu = menu

    def deleteLater(self) -> None:
        pass

    def setIcon(self, icon) -> None:  # type: ignore[no-untyped-def]
        self.icons += 1

    def setToolTip(self, text: str) -> None:
        self.tooltips.append(text)

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def isVisible(self) -> bool:
        return self.visible


def test_tray_updates_only_when_something_changes(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [SCRIPT[0]])
    trays: list[FakeTray] = []
    window.attach_tray(lambda: trays.append(FakeTray()) or trays[-1])  # type: ignore[arg-type,func-returns-value]
    window.set_pinned("pkg", True)
    logo, tray = trays
    assert "CPU 50.0 °C · 5% load" in logo.tooltips[-1]
    assert tray.icons == 1
    window.refresh()
    window.refresh()
    assert tray.icons == 1 and len(logo.tooltips) == 1  # same reading, nothing sent
    window.set_fahrenheit(True)
    assert tray.icons == 2
    assert logo.icons == 1 and "122.0 °F" in logo.tooltips[-1]  # the logo itself never changes


def test_close_to_tray_hides_and_quit_really_closes(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    window = make_window(qtbot, settings, SCRIPT)
    window.attach_tray(FakeTray)  # type: ignore[arg-type]
    window.tray_action.setChecked(True)
    window.show()
    monkeypatch.setattr(QApplication, "quit", lambda *a: None)
    window.close()
    assert window.isHidden() and window._thread.isRunning()  # still sampling in the tray
    window.show_window()
    assert window.isVisible()
    window.quit()
    assert window.isHidden() and not window._thread.isRunning()
    assert settings.value("geometry") is not None


def test_close_to_tray_steps_aside_when_the_session_ends(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    window = make_window(qtbot, settings, SCRIPT)
    window.attach_tray(FakeTray)  # type: ignore[arg-type]
    window.tray_action.setChecked(True)
    window.show()
    monkeypatch.setattr(QApplication, "quit", lambda *a: None)
    monkeypatch.setattr(QGuiApplication, "isSavingSession", lambda self: True)
    window.close()
    assert not window._thread.isRunning()  # closed for real so logout isn't blocked


def test_chart_and_grid_paint_in_both_units(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    clock_values = [[Reading("p", "GPU · RTX", "Power draw", Kind.POWER, 90.0 + i % 7, cap=100.0)] for i in range(400)]
    window = make_window(qtbot, settings, [*SCRIPT, *clock_values])
    window.resize(1200, 900)
    window.show()
    for _ in range(400):
        window.refresh()
    window.grab()  # paints every card and the detail chart
    window.set_fahrenheit(True)
    window.grab()
    window._select("p")
    image = window.detail.chart.grab().toImage()
    assert window.detail.stat_values["past_limit"].text() == "no limit"  # a cap never warns
    assert "limit 100.0 W" in window.detail.limits.text()
    colours = {image.pixelColor(x, y).name() for x in range(0, image.width(), 3) for y in range(0, image.height(), 3)}
    assert theme.current_theme().accent.lower() in colours  # the line was drawn


def test_decimate_keeps_extremes() -> None:
    from corewatch.gui.widgets import decimate

    points = [(t / 10, 50.0) for t in range(1000)]
    points[500] = (50.0, 99.0)  # a one-sample spike
    kept = decimate(points, 0.0, 100.0, 50)
    assert len(kept) <= 100
    assert (50.0, 99.0) in kept
    assert kept == sorted(kept)
    assert decimate(points[:10], 0.0, 100.0, 50) == points[:10]


def test_min_max_toggle_hides_the_columns_and_is_remembered(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    grid = window.sections["CPU · i7"].grid
    assert window.min_max_action.isChecked() and grid.shown_stats() == ("value", "min", "max", "avg")
    with_columns = grid.cell_min_width()
    window.min_max_action.trigger()
    assert grid.shown_stats() == ("value", "avg")
    assert grid.cell_min_width() < with_columns  # the space goes back to the cell
    parts = grid._parts(grid.cells()[0].rect)
    assert "min" not in parts and "max" not in parts
    assert settings.value("show_min_max", type=bool) is False
    reopened = make_window(qtbot, settings, SCRIPT)
    assert not reopened.min_max_action.isChecked()
    assert reopened.sections["CPU · i7"].grid.shown_stats() == ("value", "avg")
    grid.grab()  # paints without the hidden columns


def test_sparkline_takes_the_space_the_label_does_not_need(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.sensors import SPARK_MIN_W

    window = make_window(qtbot, settings, SCRIPT)
    grid = window.sections["CPU · i7"].grid
    narrow = grid._parts(QRect(0, 0, grid.cell_min_width(), 30))
    assert narrow["spark"].width() >= SPARK_MIN_W - 1
    wide = grid._parts(QRect(0, 0, grid.cell_min_width() + 300, 30))
    assert wide["spark"].width() >= narrow["spark"].width() + 290  # extra width goes to the sparkline
    assert wide["label"].width() == narrow["label"].width() == grid.label_width
    assert wide["label"].right() < wide["spark"].left() < wide["spark"].right() < wide["value"].left()


def test_hovering_the_chart_picks_the_nearest_reading(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    script = [[temp("t", "CPU", float(v))] for v in (40, 41, 42, 43, 44, 45)]
    window = make_window(qtbot, settings, script)
    for _ in range(5):
        window.refresh()  # the fake clock advances 1 s per reading
    window.detail.set_window(60.0)
    chart = window.detail.chart
    chart.resize(700, 220)
    plot = chart.plot_rect()
    # Aim at the third reading: its x position on the 60 s axis.
    third = window.rows["t"].stats.history[2]
    x = plot.left() + (third[0] - (chart.now - 60)) / 60 * plot.width()
    move = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(x + 1, 50),
        QPointF(x + 1, 50),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(chart, move)
    assert chart.hovered_point() == third
    value, when = chart.hover_text()  # type: ignore[misc]
    assert value == "42.0 °C"
    assert when.endswith(f"· {format_duration(chart.now - third[0])} ago")
    chart.grab()  # paints the guide line and bubble
    chart.hover_x = plot.left() - 20  # outside the plot: nothing hovered
    assert chart.hovered_point() is None
    QApplication.sendEvent(chart, QEvent(QEvent.Type.Leave))
    assert chart.hover_x is None


def test_drawable_segments_split_on_raw_gaps_before_thinning() -> None:
    from corewatch.gui.widgets import drawable_segments

    steady = [(i * 0.25, 1.2) for i in range(3600)]  # 15 min at 0.25 s, no missing readings
    segments = drawable_segments(steady, 0.0, 900.0, 180, gap=1.75)
    assert len(segments) == 1  # thinning must not invent gaps
    assert 2 <= len(segments[0]) <= 360
    gapped = steady[:100] + steady[200:]  # 25 s really missing
    assert len(drawable_segments(gapped, 0.0, 900.0, 180, gap=1.75)) == 2


def test_steady_sensor_still_draws_on_a_narrow_long_chart(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    script = [[Reading("v", "Motherboard", "+12V", Kind.VOLTAGE, 12.0)]]
    monitor = Monitor([FakeSource("s", script)], clock=lambda: 0.0)
    for i in range(3600):
        monitor.clock = lambda i=i: i * 0.25  # type: ignore[misc]
        monitor.sample()
    window = MainWindow(monitor, settings, interval=0.25)
    qtbot.addWidget(window)
    window.timer.stop()
    window.detail.set_window(900.0)
    window._select("v")
    chart = window.detail.chart
    chart.resize(720, 220)
    image = chart.grab().toImage()
    accent = theme.current_theme().accent.lower()
    lit = sum(image.pixelColor(x, y).name() == accent for x in range(image.width()) for y in range(image.height()))
    # Guards what you see (a line, not a blank chart). The ordering that caused the blank
    # chart is pinned by test_drawable_segments_split_on_raw_gaps_before_thinning.
    assert lit > 300


def test_late_reading_after_shutdown_is_ignored_and_ingest_runs_on_the_gui_thread(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtCore import QThread

    window = make_window(qtbot, settings, SCRIPT)
    threads = []
    real_ingest = window.monitor.ingest

    def ingest(collected):  # type: ignore[no-untyped-def]
        threads.append(QThread.currentThread())
        return real_ingest(collected)

    window.monitor.ingest = ingest  # type: ignore[method-assign]
    with qtbot.waitSignal(window._sampler.collected, timeout=3000):
        window._request_sample()
    qtbot.waitUntil(lambda: bool(threads), timeout=3000)
    assert threads == [QApplication.instance().thread()]
    late = window.monitor.collect()
    assert window.shutdown() is True
    before = texts(window, "pkg")
    window._on_collected(late)
    assert texts(window, "pkg") == before and len(threads) == 1


def test_a_stuck_reading_is_abandoned_not_closed_under(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import threading

    from corewatch.gui import app as app_module

    release = threading.Event()

    class Hangs(FakeSource):
        def sample(self):  # type: ignore[no-untyped-def]
            if self.calls >= 1:
                release.wait(10)
            return super().sample()

    source = Hangs("hang", SCRIPT)
    monitor = Monitor([source])
    window = MainWindow(monitor, settings)
    qtbot.addWidget(window)
    window.timer.stop()
    monkeypatch.setattr(app_module, "SHUTDOWN_WAIT_MS", 100)
    exits = []
    monkeypatch.setattr(app_module.os, "_exit", lambda code: exits.append(code))
    window._request_sample()
    qtbot.wait(50)
    app_module.finish(window, monitor, 0)
    assert exits == [0] and not source.closed  # left alone: closing would race the hung call
    assert window.shutdown() is False  # and nobody waits on it a second time
    release.set()
    window._thread.wait(3000)


def test_finish_closes_the_sources_after_a_clean_stop(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui import app as app_module

    window = make_window(qtbot, settings, SCRIPT)
    app_module.finish(window, window.monitor, 0)
    assert window.monitor.sources[0].closed


def test_selection_moves_off_a_sensor_that_becomes_hidden(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    script = [
        [
            temp("t", "CPU · i7", 50.0, label="CPU package"),
            Reading("fan1", "Motherboard", "Fan 1", Kind.FAN, 0.0, empty_if_idle=True),
        ]
    ]
    window = make_window(qtbot, settings, script)
    window.unused_action.setChecked(True)
    window._select("fan1")
    window.unused_action.setChecked(False)
    assert window.selected_key == "t"
    assert window.detail.title.text() == "CPU package"


def test_fans_from_every_device_share_one_card(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    script = [
        [
            temp("pkg", "CPU · i7", 50.0, label="CPU package"),
            Reading("mb-fan", "Motherboard · Z790", "Fan 2", Kind.FAN, 1500.0),
            Reading("gpu-rpm", "GPU · RTX", "Fan 1", Kind.FAN, 2320.0),
            Reading("gpu-pct", "GPU · RTX", "Fan 1 speed", Kind.FAN_DUTY, 72.0),
            temp("gpu", "GPU · RTX", 41.0, label="GPU temperature"),
        ]
    ]
    window = make_window(qtbot, settings, script)
    assert list(window.sections) == ["CPU · i7", "GPU · RTX", "Fans"]  # wide cards first, then the halves
    labels = {c.key: texts(window, c.key)[0] for c in window.sections["Fans"].grid.cells()}
    assert labels == {"mb-fan": "Fan 2", "gpu-rpm": "GPU fan 1", "gpu-pct": "GPU fan 1 speed"}
    assert [c.key for c in window.sections["GPU · RTX"].grid.cells()] == ["gpu"]


def test_rename_and_reset_a_sensor(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    calls = window.monitor.sources[0].calls
    window._ask_name = lambda current: "  Package (renamed)  "  # type: ignore[method-assign]
    window.sensor_menu("pkg").actions()[2].trigger()  # Rename…
    assert texts(window, "pkg")[0] == "Package (renamed)"
    assert window.detail.title.text() == "Package (renamed)"
    assert window.monitor.sources[0].calls == calls  # relabelled without a new reading
    reopened = make_window(qtbot, settings, SCRIPT)
    assert texts(reopened, "pkg")[0] == "Package (renamed)"
    menu = window.sensor_menu("pkg")
    assert menu.actions()[3].isEnabled()  # Reset name
    menu.actions()[3].trigger()
    assert texts(window, "pkg")[0] == "CPU package"
    window._ask_name = lambda current: None  # type: ignore[method-assign]  # dialog cancelled
    window.sensor_menu("pkg").actions()[2].trigger()
    assert texts(window, "pkg")[0] == "CPU package"


def test_pinned_sensors_get_their_own_tray_icons(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    made: list[FakeTray] = []
    window.attach_tray(lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    assert list(window.trays) == [""]  # nothing pinned: only corewatch's own icon
    window.sensor_menu("gpu").actions()[0].trigger()  # Pin to tray
    window.set_pinned("cpu-load", True)
    assert list(window.trays) == ["", "gpu", "cpu-load"]
    assert made[0].visible  # corewatch's icon stays beside the pinned ones
    assert made[1].tooltips[-1] == "GPU · RTX\nGPU temperature\nmin: 41.0 °C\nmax: 41.0 °C\naverage: 41.0 °C"
    assert window._tray_states["cpu-load"][0] == "5"
    window._select("gpu")
    assert window.detail.pin_button.isChecked()
    window.detail.pin_button.click()  # unpin from the detail panel
    assert list(window.trays) == ["", "cpu-load"]
    assert json.loads(settings.value("pinned")) == ["cpu-load"]
    reopened = make_window(qtbot, settings, SCRIPT)
    reopened.attach_tray(FakeTray)  # type: ignore[arg-type]
    assert list(reopened.trays) == ["", "cpu-load"]


def test_pin_is_disabled_without_a_system_tray(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    assert not window.sensor_menu("pkg").actions()[0].isEnabled()


def test_short_tray_numbers() -> None:
    from corewatch.model import format_short

    assert format_short(Kind.TEMPERATURE, 47.4) == "47"
    assert format_short(Kind.TEMPERATURE, 100.0, fahrenheit=True) == "212"
    assert format_short(Kind.FAN, 2320.0) == "2.3k" and format_short(Kind.FAN, 850.0) == "850"
    assert format_short(Kind.CLOCK, 4800.0) == "4.8"
    assert format_short(Kind.VOLTAGE, 1.184) == "1.18" and format_short(Kind.VOLTAGE, 12.0) == "12.0"
    assert format_short(Kind.THROUGHPUT, 953_900.0) == "954K" and format_short(Kind.THROUGHPUT, 1_200_000.0) == "1.2M"
    assert format_short(Kind.LOAD, None) == "?"


def test_renaming_never_changes_what_the_tray_and_summary_track(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    script = [
        [
            temp("pkg", "CPU · i7", 50.0, label="CPU package"),
            temp("core", "CPU · i7", 70.0, label="P-core 0"),
            load("cpu-load", "CPU · i7", 5.0, label="CPU load (all cores)"),
        ]
    ]
    window = make_window(qtbot, settings, script)
    window.attach_tray(FakeTray)  # type: ignore[arg-type]
    window.rename_sensor("pkg", "My CPU")
    window.rename_sensor("cpu-load", "Busy")
    assert "CPU 50.0 °C" in window._tray_states[""][2]  # still the package, not the hottest core
    assert window._summary() == ["CPU 50.0 °C · 5% load"]


def test_a_vanished_pinned_sensor_can_be_unpinned_from_its_icon(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    script = [SCRIPT[0], [temp("pkg", "CPU · i7", 50.0, label="CPU package")]]  # "gpu" stops reporting
    window = make_window(qtbot, settings, script)
    made: list[FakeTray] = []
    window.attach_tray(lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    window.set_pinned("gpu", True)
    menu = made[-1].menu

    def unpin():  # type: ignore[no-untyped-def]
        return next(a for a in menu.actions() if a.text().startswith("Unpin"))

    assert unpin().text() == "Unpin GPU temperature"
    window.refresh()
    assert window._tray_states["gpu"][0] == "?"
    assert "isn't reporting" in made[-1].tooltips[-1]
    assert unpin().text() == "Unpin this sensor"
    assert [a.text() for a in menu.actions()[:6] if a.isVisible() and not a.isSeparator()] == [
        "corewatch",
        "This pinned sensor isn't reporting right now",
    ]
    unpin().trigger()
    assert window.pinned == [] and list(window.trays) == [""]


def test_tray_icons_and_menus_are_freed_when_unpinned(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QMenu

    window = make_window(qtbot, settings, SCRIPT)

    class QtTray(FakeTray):
        pass

    window.attach_tray(lambda: QtTray())  # type: ignore[arg-type]
    before = len(window.findChildren(QMenu))  # the settings menu, corewatch's tray menu and their submenus
    for _ in range(3):
        window.set_pinned("gpu", True)
        window.set_pinned("gpu", False)
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(window.findChildren(QMenu)) == before


def test_right_click_opens_the_sensor_menu_and_frees_it(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QMenu

    window = make_window(qtbot, settings, SCRIPT)
    window.resize(1200, 900)
    window.show()
    shown = []
    # Record the menu instead of opening it: a real popup would wait for a click.
    window._popup = lambda menu, pos: shown.append([a.text() for a in menu.actions()])  # type: ignore[method-assign]
    grid = window.sections["GPU · RTX"].grid
    before = len(window.findChildren(QMenu))
    for _ in range(4):
        point = grid.cells()[0].rect.center()
        QApplication.sendEvent(grid, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, grid.mapToGlobal(point)))
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert shown[0][0] == "Pin to tray" and "Rename…" in shown[0]
    assert window.selected_key == "gpu"  # right-click also selects
    assert len(window.findChildren(QMenu)) == before


def test_detail_pin_button_is_disabled_without_a_tray(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    assert not window.detail.pin_button.isEnabled()
    window.attach_tray(FakeTray)  # type: ignore[arg-type]
    assert window.detail.pin_button.isEnabled()


def test_detail_subtitle_shows_the_fans_real_device(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    script = [[Reading("g", "GPU · RTX", "Fan 1", Kind.FAN, 2320.0)]]
    window = make_window(qtbot, settings, script)
    window._select("g")
    assert window.detail.subtitle.text() == "GPU · RTX"


def test_start_at_login_toggle_writes_and_removes_the_autostart_entry(qtbot, settings, private_config) -> None:  # type: ignore[no-untyped-def]
    entry = private_config / "autostart" / "corewatch.desktop"
    window = make_window(qtbot, settings, SCRIPT)
    assert window.autostart_file == entry and not window.autostart_action.isChecked()
    window.autostart_action.setChecked(True)
    assert entry.exists() and "X-Corewatch-Autostart=true" in entry.read_text()
    assert "will start when you log in (runs " in window.statusBar().currentMessage()
    entry.unlink()  # removed from the desktop's Startup Applications instead
    window._sync_autostart()  # (runs whenever the settings menu opens)
    assert not window.autostart_action.isChecked()
    window.autostart_action.setChecked(True)
    window.autostart_action.setChecked(False)
    assert not entry.exists()


def test_start_at_login_leaves_a_foreign_entry_alone(qtbot, settings, private_config) -> None:  # type: ignore[no-untyped-def]
    entry = private_config / "autostart" / "corewatch.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nExec=corewatch --their-own-flags\n")
    window = make_window(qtbot, settings, SCRIPT)
    assert window.autostart_action.isChecked()
    window.autostart_action.setChecked(False)
    assert entry.exists() and window.autostart_action.isChecked()
    assert "Startup Applications" in window.statusBar().currentMessage()


def test_start_at_login_reports_a_write_failure(qtbot, settings, private_config) -> None:  # type: ignore[no-untyped-def]
    private_config.mkdir(parents=True)
    (private_config / "autostart").write_text("not a folder")
    window = make_window(qtbot, settings, SCRIPT)
    window.autostart_action.setChecked(True)
    assert not window.autostart_action.isChecked()
    assert window.statusBar().currentMessage().startswith("Couldn't change the login setting")


def test_an_unreadable_login_entry_never_stops_corewatch_starting(qtbot, settings, private_config) -> None:  # type: ignore[no-untyped-def]
    entry = private_config / "autostart" / "corewatch.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"[Desktop Entry]\nName=caf\xe9\n")
    window = make_window(qtbot, settings, SCRIPT)
    assert not window.autostart_action.isChecked()
    window.autostart_action.setChecked(True)
    assert not window.autostart_action.isChecked() and "can't be read" in window.statusBar().currentMessage()


def test_start_minimized_option_is_remembered(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    assert not window.minimized_action.isChecked()
    window.minimized_action.setChecked(True)
    assert make_window(qtbot, settings, SCRIPT).start_minimized


def test_start_shows_the_window_or_stays_in_the_tray(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    shown = make_window(qtbot, settings, SCRIPT)
    shown.start(False, lambda: True, FakeTray)  # type: ignore[arg-type]
    assert shown.isVisible() and shown.trays
    tray_only = make_window(qtbot, settings, SCRIPT)
    tray_only.start(True, lambda: True, FakeTray)  # type: ignore[arg-type]
    assert not tray_only.isVisible() and tray_only.trays


def test_a_tray_that_turns_up_late_is_waited_for(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    ready = []
    window = make_window(qtbot, settings, SCRIPT)
    window.start(True, lambda: bool(ready), FakeTray, at_login=True, retry_ms=10, attempts=50)  # type: ignore[arg-type]
    assert not window.isVisible() and not window.trays  # waiting, out of sight
    ready.append(True)
    qtbot.waitUntil(lambda: bool(window.trays), timeout=2000)
    assert not window.isVisible()  # the tray arrived: stay there


def test_no_tray_at_all_means_the_window_opens(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.start(True, lambda: False, FakeTray, at_login=True, retry_ms=10, attempts=3)  # type: ignore[arg-type]
    qtbot.waitUntil(window.isVisible, timeout=2000)
    assert not window.trays


def test_a_window_opened_without_a_tray_still_gets_one_later(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    ready = []
    window = make_window(qtbot, settings, SCRIPT)
    window.start(False, lambda: bool(ready), FakeTray, retry_ms=10, attempts=50)  # type: ignore[arg-type]
    assert window.isVisible()
    ready.append(True)
    qtbot.waitUntil(lambda: bool(window.trays), timeout=2000)  # so close-to-tray works after all


def test_start_at_login_reports_a_folder_it_cannot_write(qtbot, settings, private_config) -> None:  # type: ignore[no-untyped-def]
    folder = private_config / "autostart"
    folder.mkdir(parents=True)
    folder.chmod(0o500)  # readable, not writable
    try:
        window = make_window(qtbot, settings, SCRIPT)
        window.autostart_action.setChecked(True)
        assert not window.autostart_action.isChecked()
        assert window.statusBar().currentMessage().startswith("Couldn't change the login setting")
        assert list(folder.iterdir()) == []
    finally:
        folder.chmod(0o700)


def test_minimized_without_a_tray_opens_at_once_unless_starting_at_login(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.start(True, lambda: False, FakeTray, retry_ms=10, attempts=50)  # type: ignore[arg-type]
    assert window.isVisible()  # an ordinary launch never makes you wait
    assert not window.minimized_action.isEnabled()  # and the option says it needs a tray


def test_start_minimized_is_offered_once_a_tray_exists(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    ready = []
    window = make_window(qtbot, settings, SCRIPT)
    window.start(False, lambda: bool(ready), FakeTray, retry_ms=10, attempts=50)  # type: ignore[arg-type]
    assert not window.minimized_action.isEnabled()
    ready.append(True)
    qtbot.waitUntil(window.minimized_action.isEnabled, timeout=2000)


def test_pinned_tray_tooltip_shows_min_max_and_average(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)  # CPU package reads 50 °C, then 85 °C
    made: list[FakeTray] = []
    window.attach_tray(lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    window.set_pinned("pkg", True)
    window.refresh()
    assert made[-1].tooltips[-1] == "CPU · i7\nCPU package\nmin: 50.0 °C\nmax: 85.0 °C\naverage: 67.5 °C"
    window.set_fahrenheit(True)
    assert made[-1].tooltips[-1] == "CPU · i7\nCPU package\nmin: 122.0 °F\nmax: 185.0 °F\naverage: 153.5 °F"
    window.rename_sensor("pkg", "My CPU")
    assert made[-1].tooltips[-1].startswith("CPU · i7\nMy CPU\n")


def test_pinned_tray_menu_lists_name_min_max_and_average(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)  # CPU package reads 50 °C, then 85 °C
    made: list[FakeTray] = []
    window.attach_tray(lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    window.set_pinned("pkg", True)
    window.refresh()

    def texts() -> list[str]:
        return ["---" if a.isSeparator() else a.text() for a in made[-1].menu.actions() if a.isVisible()]

    assert texts()[:8] == [
        "CPU · i7",
        "---",
        "CPU package",
        "min: 50.0 °C",
        "max: 85.0 °C",
        "average: 67.5 °C",
        "---",
        "Unpin CPU package",
    ]
    info = [a for a in made[-1].menu.actions()[:6] if not a.isSeparator()]
    assert len(info) == 5 and not any(a.isEnabled() for a in info)  # information, not commands
    window.set_fahrenheit(True)
    assert texts()[3] == "min: 122.0 °F"


def test_the_logo_leads_the_toolbar_and_is_the_window_icon(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.app import LOGO_SIZE, app_icon

    window = make_window(qtbot, settings, SCRIPT)
    assert not app_icon().isNull()
    pixmap = window.logo.pixmap()
    assert not pixmap.isNull() and pixmap.width() >= LOGO_SIZE
    assert window.logo.toolTip() == "corewatch" and window.logo.accessibleName() == "corewatch"
    assert not window.windowIcon().isNull()
    labels = [label.text() for label in window.findChildren(QLabel)]
    assert "COREWATCH" in labels and any(text.startswith("HOST ") for text in labels)  # name and host beside it


def test_the_icon_ships_inside_the_package() -> None:
    from importlib.resources import files

    from PySide6.QtSvg import QSvgRenderer

    icon = files("corewatch") / "assets" / "corewatch.svg"
    assert icon.is_file()
    assert QSvgRenderer(str(icon)).isValid()


def test_launcher_and_login_entries_use_the_corewatch_icon() -> None:
    from pathlib import Path

    from corewatch import autostart

    assert "Icon=corewatch" in Path("packaging/corewatch.desktop").read_text().splitlines()
    assert "Icon=corewatch" in autostart.entry(["/usr/bin/corewatch"]).splitlines()


def test_corewatchs_tray_menu_has_the_apps_options(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    made: list[FakeTray] = []
    window.attach_tray(lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    window.set_pinned("pkg", True)
    window.refresh()
    logo, pinned = made

    def texts(menu) -> list[str]:  # type: ignore[no-untyped-def]
        return ["---" if a.isSeparator() else a.text() for a in menu.actions() if a.isVisible()]

    assert logo.icons == 1
    assert texts(logo.menu) == [
        "Open corewatch",
        "---",
        "Reset min/max",
        "---",
        "Update every",
        "Temperatures in",
        "Show min / max in the list",
        "---",
        "Theme",
        "Show unused sensors",
        "Show this computer's name",
        "---",
        "Keep running in the tray when closed",
        "Start when I log in",
        "Start minimized in the tray",
        "---",
        "Quit",
    ]
    # The settings button holds the same options as the tray icon, minus opening the window.
    assert texts(window.settings_button.menu()) == texts(logo.menu)[2:]
    assert texts(pinned.menu)[-1] == "Unpin CPU package"  # a pinned icon's menu is about its sensor only
    assert "Quit" not in texts(pinned.menu)

    def item(text: str):  # type: ignore[no-untyped-def]
        return next(a for a in logo.menu.actions() if a.text() == text)

    item("Open corewatch").trigger()
    assert window.isVisible()
    assert window.rows["pkg"].stats.maximum == 85.0
    item("Reset min/max").trigger()
    assert window.rows["pkg"].stats.maximum is None

    # Every option in the tray stays in step with the window's own controls, both ways.
    every = {a.text(): a for a in item("Update every").menu().actions()}
    every["5 s"].trigger()
    assert window.interval == 5.0 and window.interval_box.currentText() == "5 s"
    assert settings.value("interval", type=float) == 5.0
    window.interval_box.setCurrentIndex(window.interval_box.findData(2.0))
    assert window.interval == 2.0 and every["2 s"].isChecked() and not every["5 s"].isChecked()
    units = {a.text(): a for a in item("Temperatures in").menu().actions()}
    units["Fahrenheit (°F)"].trigger()
    assert window.fahrenheit and window.fahrenheit_button.isChecked()
    window.celsius_button.click()
    assert not window.fahrenheit and units["Celsius (°C)"].isChecked()
    item("Show min / max in the list").trigger()
    assert not window.show_min_max and not window.min_max_action.isChecked()
    window.set_show_min_max(True)
    assert window.show_min_max and item("Show min / max in the list").isChecked()
    dark = next(a for a in item("Theme").menu().actions() if a.text() == "Dark")
    dark.trigger()
    assert window.theme == "dark" and dark.isChecked()
    window.apply_theme("system")
    item("Show unused sensors").trigger()
    assert window.show_unused and window.unused_action.isChecked()

    quits: list[bool] = []
    monkeypatch.setattr(window, "quit", lambda: quits.append(True))
    item("Quit").trigger()
    assert quits == [True]


def test_corewatchs_own_icon_stays_while_the_window_is_open(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    made: list[FakeTray] = []
    window.start(False, lambda: True, lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    [logo] = made
    assert window.isVisible() and logo.visible
    window.tray_action.setChecked(True)  # close-to-tray works with nothing pinned: the logo is the way back
    monkeypatch.setattr(QApplication, "quit", lambda *a: None)
    window.close()
    assert window.isHidden() and window._thread.isRunning() and logo.visible
    window.quit()  # really stop sampling: a close at teardown would only hide it again
    assert not window._thread.isRunning()


def test_starting_minimized_shows_corewatchs_icon(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    made: list[FakeTray] = []
    window.start(True, lambda: True, lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    assert not window.isVisible() and made[0].visible


def test_clicking_or_double_clicking_any_tray_icon_shows_the_window(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QSystemTrayIcon

    Reason = QSystemTrayIcon.ActivationReason
    window = make_window(qtbot, settings, SCRIPT)
    made: list[FakeTray] = []
    window.attach_tray(lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    window.set_pinned("gpu", True)
    for tray in made:  # corewatch's own icon and a pinned sensor's
        for reason in (Reason.DoubleClick, Reason.Trigger):
            window.hide()
            tray.activated.emit(reason)
            assert window.isVisible()
            tray.activated.emit(reason)  # already open: it stays open (Ubuntu reports a double click as Trigger)
            assert window.isVisible()
    window.hide()
    made[0].activated.emit(Reason.Context)  # the right-click menu doesn't open the window
    assert window.isHidden()


def test_the_tray_logo_is_one_colour_to_suit_the_panel(qapp) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.app import TRAY_ICON_SIZES, panel_is_light, tray_icon

    def colours(light_panel: bool) -> set[tuple[int, int, int]]:
        image = tray_icon(light_panel).pixmap(64, 64).toImage()
        seen = set()
        for x in range(64):
            for y in range(64):
                pixel = image.pixelColor(x, y)
                if pixel.alpha() == 255:
                    seen.add((pixel.red(), pixel.green(), pixel.blue()))
        return seen

    assert colours(False) == {(255, 255, 255)}  # white, and nothing else, for a dark panel
    assert colours(True) == {(0x18, 0x18, 0x1B)}
    assert sorted(s.width() for s in tray_icon().availableSizes()) == sorted(TRAY_ICON_SIZES)
    assert not panel_is_light("ubuntu:GNOME", Qt.ColorScheme.Light)  # GNOME's panel is black in light mode too
    assert not panel_is_light("KDE", Qt.ColorScheme.Dark)
    assert panel_is_light("KDE", Qt.ColorScheme.Light)
    assert not panel_is_light("", Qt.ColorScheme.Unknown)


def test_the_tray_shows_the_one_colour_logo(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    icons: list[object] = []

    class Tray(FakeTray):
        def setIcon(self, icon) -> None:  # type: ignore[no-untyped-def]
            super().setIcon(icon)
            icons.append(icon)

    window.attach_tray(Tray)  # type: ignore[arg-type]
    pixel = icons[0].pixmap(64, 64).toImage().pixelColor(32, 32)  # type: ignore[attr-defined]
    assert (pixel.red(), pixel.green(), pixel.blue()) == (255, 255, 255)  # the core, not the colour logo's amber


def test_settings_menu_leads_with_reset_and_ctrl_r_still_resets(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    first = window.settings_button.menu().actions()[0]
    assert first is window.reset_action and first.text() == "Reset min/max"
    assert window.reset_shortcut.key().toString() == "Ctrl+R"
    assert window.reset_action.shortcut().isEmpty()  # so the tray menu shows no shortcut it can't honour
    with qtbot.waitActive(window):
        window.show()
        window.activateWindow()
    window.refresh()
    assert texts(window, "pkg")[2] != "—"
    qtbot.keyClick(window.filter, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)  # focus in the filter box
    assert texts(window, "pkg")[2] == "—"
    assert window.settings_button.accessibleName() == "Settings"
    assert not window.settings_button.icon().isNull()


def test_live_chip_shows_the_update_interval(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.set_interval(2.0)
    assert window.live_chip.text().endswith("LIVE · 2 s")
    window.refresh()
    window.set_interval(0.25)  # a reading 2 s old isn't overdue under the new, shorter interval
    assert window.live_chip.text().endswith("LIVE · 0.25 s")


def test_bundled_fonts_load_with_their_licences(bundled_fonts) -> None:  # type: ignore[no-untyped-def]
    from importlib.resources import files

    from PySide6.QtGui import QFontDatabase

    fonts = files("corewatch") / "assets" / "fonts"
    for name in theme.FONT_FILES:
        assert (fonts / name).is_file()
    for licence in ("ChakraPetch-OFL.txt", "JetBrainsMono-OFL.txt", "IBMPlexSans-OFL.txt"):
        assert "SIL Open Font License" in (fonts / licence).read_text()
    assert {theme.DISPLAY_FONT, theme.MONO_FONT, theme.BODY_FONT} <= bundled_fonts
    assert all(QFontDatabase.hasFamily(name) for name in bundled_fonts)
    assert QApplication.font().family() == theme.BODY_FONT


def test_background_grid_and_corner_mark_geometry() -> None:
    from PySide6.QtCore import QPointF, QRectF

    assert theme.grid_lines(70, 40, 32) == ([0, 32, 64], [0, 32])
    assert theme.grid_lines(0, -5) == ([], [])
    marks = theme.corner_marks(QRectF(0, 0, 100, 50), arm=16)
    assert marks[0] == (QPointF(0, 0), QPointF(16, 0)) and marks[1] == (QPointF(0, 0), QPointF(0, 16))
    assert marks[2] == (QPointF(100, 50), QPointF(84, 50)) and marks[3] == (QPointF(100, 50), QPointF(100, 34))
    tiny = theme.corner_marks(QRectF(0, 0, 10, 6), arm=16)  # never longer than half the card
    assert tiny[0][1] == QPointF(3, 0) and tiny[1][1] == QPointF(0, 3)


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT])
def test_background_grid_is_a_faint_tint_of_the_accent(palette) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QColor

    grid, accent = QColor(palette.grid), QColor(palette.accent)
    assert (grid.red(), grid.green(), grid.blue()) == (accent.red(), accent.green(), accent.blue())
    assert 0 < grid.alpha() <= 16  # Qt reads 8 hex digits as #AARRGGBB


def test_background_grid_paints_faint_lines_over_the_window_colour(qapp) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QPainter

    image = QImage(100, 70, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    theme.paint_grid(painter, QRectF(0, 0, 100, 70), theme.DARK)
    painter.end()
    window, line = QColor(theme.DARK.window), image.pixelColor(32, 10)
    assert image.pixelColor(10, 10) == window  # between lines
    assert line != window and abs(line.blue() - window.blue()) <= 12  # on a line: barely different
    assert line.blue() > window.blue() and line.red() <= window.red() + 2  # and tinted cyan, not olive


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT])
def test_cards_paint_accent_corner_marks(qtbot, palette, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QColor

    from corewatch.gui.sensors import CategorySection

    monkeypatch.setattr("corewatch.gui.sensors.current_theme", lambda: palette)
    section = CategorySection("CPU · i7")
    qtbot.addWidget(section)
    section.resize(300, 120)
    image = section.grab().toImage()
    accent, surface = QColor(palette.accent), QColor(palette.surface)
    assert image.pixelColor(8, 1) == accent  # top-left mark, along the top edge
    assert image.pixelColor(1, 8) == accent  # and down the left edge
    assert image.pixelColor(image.width() - 9, image.height() - 2) == accent  # bottom-right mark
    assert image.pixelColor(image.width() - 9, 1) != accent  # top-right corner has no mark
    assert image.pixelColor(150, 100) == surface


def _contrast(a: str, b: str) -> float:
    from PySide6.QtGui import QColor

    def luminance(hex_color: str) -> float:
        c = QColor(hex_color)
        channels = []
        for value in (c.redF(), c.greenF(), c.blueF()):
            channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT])
def test_theme_text_is_readable(palette) -> None:  # type: ignore[no-untyped-def]
    for ground in (palette.window, palette.surface, palette.raised):
        assert _contrast(palette.text, ground) >= 7
        assert _contrast(palette.muted, ground) >= 4.5
    # Status colours draw values as 13 px text, also on a selected row's tint.
    for color in (palette.accent, palette.warning, palette.critical):
        for ground in (palette.surface, palette.raised, palette.accent_soft):
            assert _contrast(color, ground) >= 4.5
    for color in (palette.text, palette.muted, palette.accent_text):  # a selected row's label and numbers
        assert _contrast(color, palette.accent_soft) >= 4.5
    assert _contrast(palette.warning, palette.warning_soft) >= 4.5  # the notes banner and a stalled chip
    assert _contrast(palette.accent_text, palette.accent_soft) >= 4.5  # the LIVE chip


def test_text_scales_with_the_desktops_text_size(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QFont

    def scale(points: float, dpi: float = 96) -> float:
        desktop = QFont()
        desktop.setPointSizeF(points)
        return theme.text_scale_for(theme.desktop_text_pixels(desktop, dpi))

    assert scale(11) == pytest.approx(1.0)
    assert scale(9) == 1.0  # never smaller than designed
    assert scale(16.5) == pytest.approx(1.5)
    assert scale(11, dpi=144) == pytest.approx(1.5)  # X11 enlarges text by font DPI
    assert scale(30) == theme.MAX_TEXT_SCALE
    in_pixels = QFont()
    in_pixels.setPixelSize(22)
    assert theme.text_scale_for(theme.desktop_text_pixels(in_pixels, 96)) == pytest.approx(1.5)
    assert theme.text_scale_for(-1) == 1.0  # unknown
    monkeypatch.setattr(theme, "_text_scale", 1.5)
    assert theme.px(10) == 15
    assert theme.font(theme.BODY_FONT, 10).pixelSize() == 15
    assert "font-size: 15px" in theme.stylesheet(theme.DARK) and "font-size: 10px" not in theme.stylesheet(theme.DARK)


def test_tray_number_icons_ignore_the_apps_bundled_font(qapp) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QFont

    before = QApplication.font()
    try:
        QApplication.setFont(QFont(theme.BODY_FONT))
        body = temperature_icon(45.0, Status.OK, False).pixmap(64, 64).toImage()
        QApplication.setFont(QFont(theme.MONO_FONT))
        mono = temperature_icon(45.0, Status.OK, False).pixmap(64, 64).toImage()
    finally:
        QApplication.setFont(before)
    assert body == mono  # drawn in the desktop's own font whatever the app's default is


def test_host_name_is_short_and_can_be_hidden(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.app import short_host

    assert short_host("sophia.lan.example.org") == "sophia" and short_host("box") == "box"
    window = make_window(qtbot, settings, SCRIPT)
    assert window.host_action.isChecked() and not window.host_label.isHidden()
    window.host_action.trigger()
    assert window.host_label.isHidden() and settings.value("show_host", type=bool) is False
    reopened = make_window(qtbot, settings, SCRIPT)
    assert reopened.host_label.isHidden() and not reopened.host_action.isChecked()


def test_live_chip_turns_to_waiting_when_readings_stall(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.refresh()
    last = window.monitor.last_sample_at
    assert last is not None
    window.monitor.clock = lambda: last + window.gap + 1  # type: ignore[method-assign]
    window._update_live()
    assert "WAITING" in window.live_chip.text() and window.live_chip.property("stalled") is True
    window.set_interval(0.5)  # changing the interval doesn't hide a stall
    assert "WAITING" in window.live_chip.text()
    window.monitor.clock = lambda: last + 0.1  # type: ignore[method-assign]
    window._update_live()
    assert "LIVE" in window.live_chip.text() and window.live_chip.property("stalled") is False


def test_narrow_window_keeps_the_settings_button(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.show()
    window.resize(1400, 800)
    qtbot.waitUntil(lambda: window.width() == 1400)
    assert window.brand_action.isVisible() and window.live_chip_action.isVisible()
    window.resize(900, 800)
    qtbot.waitUntil(lambda: window.width() == 900)
    assert window.brand_action.isVisible() and not window.live_chip_action.isVisible()  # the chip goes first
    for width in (900, 800, window.minimumSizeHint().width()):
        window.resize(width, 800)
        qtbot.waitUntil(lambda width=width: window.width() == width)
        QApplication.processEvents()
        assert window.settings_button.isVisible(), width
    assert not window.live_chip_action.isVisible() and not window.brand_action.isVisible()
    window.resize(1400, 800)
    qtbot.waitUntil(lambda: window.width() == 1400)
    assert window.brand_action.isVisible() and window.live_chip_action.isVisible()  # and back


def test_toolbar_hides_blocks_only_when_they_dont_fit(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.show()
    toolbar = window.toolbar

    def check(width: int) -> None:
        QApplication.processEvents()  # the toolbar lays itself out on a posted event
        assert window.settings_button.isVisible(), width
        assert toolbar.sizeHint().width() <= toolbar.width(), width
        # Anything hidden had to be. The chip goes first, so the next step up is the brand alone,
        # then both.
        chip, brand = window.live_chip_action, window.brand_action
        bigger = [brand] if not brand.isVisible() else [brand, chip] if not chip.isVisible() else []
        if bigger:
            for action in bigger:
                action.setVisible(True)
            QApplication.processEvents()
            assert toolbar.sizeHint().width() > toolbar.width(), width
            window._fit_toolbar()

    for width in [*range(window.minimumSizeHint().width(), 1300, 10), *range(1300, 700, -10)]:
        window.resize(width, 800)
        qtbot.waitUntil(lambda width=width: window.width() == width)
        check(width)


def test_a_stall_in_a_narrow_window_keeps_the_settings_button(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    window.show()
    window.refresh()
    window.resize(960, 800)
    qtbot.waitUntil(lambda: window.width() == 960)
    last = window.monitor.last_sample_at
    assert last is not None
    window.monitor.clock = lambda: last + window.gap + 1  # type: ignore[method-assign]
    window._update_live()  # WAITING is far wider than LIVE, with no resize to refit the toolbar
    assert "WAITING" in window.live_chip.text()
    assert window.settings_button.isVisible()
    assert window.toolbar.sizeHint().width() <= window.toolbar.width()


@pytest.mark.parametrize("fahrenheit", [False, True])
def test_chart_labels_fit_at_the_largest_text_size(qtbot, settings, monkeypatch, fahrenheit) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QFontMetrics

    monkeypatch.setattr(theme, "_text_scale", theme.MAX_TEXT_SCALE)
    script = [[temp("t", "CPU", float(v))] for v in (100, 104, 108, 112, 116, 120)]
    window = make_window(qtbot, settings, script)
    for _ in range(5):
        window.refresh()
    window.set_fahrenheit(fahrenheit)
    chart = window.detail.chart
    chart.resize(700, 220)
    chart.grab()
    metrics = QFontMetrics(theme.font(theme.MONO_FONT, 11))
    plot = chart.plot_rect()
    widest = metrics.horizontalAdvance("248.0 °F" if fahrenheit else "120.0 °C")
    assert plot.left() - 8 >= widest  # value labels: no leading digit lost
    assert plot.bottom() + 6 + metrics.height() <= chart.height()  # time labels: not cut at the bottom
    assert plot.top() >= metrics.height() / 2  # top value label: not cut at the top


def test_chart_keeps_its_designed_margins_at_normal_text_size(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    script = [[temp("t", "CPU", float(v))] for v in (40, 41, 42)]
    window = make_window(qtbot, settings, script)
    for _ in range(2):
        window.refresh()
    chart = window.detail.chart
    chart.resize(700, 220)
    chart.grab()
    assert chart.plot_rect().left() == 64


# ----- overview: gauges and the core heat map ------------------------------------------------

CPU = "CPU · i7"
GPU = "GPU · RTX"


def _reading(key: str, device: str, label: str, kind: Kind, value: float | None, **extra: float | None):  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    return Reading(key, device, label, kind, value, **extra)


def machine(cores: dict[str, float] | None = None) -> list:  # type: ignore[type-arg]
    """One sample of a hybrid Intel CPU and an NVIDIA GPU, as the real sources name things."""
    cores = cores if cores is not None else {"P-core 0": 41.0, "P-core 1": 56.0, "E-core 0": 38.0, "E-core 1": 72.0}
    readings = [temp("pkg", CPU, 55.0, label="CPU package", high=80.0, crit=100.0)]
    readings += [temp(f"core/{name}", CPU, value, label=name) for name, value in cores.items()]
    readings += [
        _reading("load", CPU, "CPU load (all cores)", Kind.LOAD, 7.0),
        _reading("load/p0", CPU, "P-core 0 load", Kind.LOAD, 12.0),
        _reading("load/p1", CPU, "P-core 1 load", Kind.LOAD, 31.0),
        _reading("clock/p0", CPU, "P-core 0 clock", Kind.CLOCK, 5300.0),
        _reading("clock/p1", CPU, "P-core 1 clock", Kind.CLOCK, 2100.0),
        _reading("pkgw", CPU, "Package power", Kind.POWER, 74.8),
        _reading("corew", CPU, "Cores power", Kind.POWER, 64.1),
        temp("gpu", GPU, 33.0, label="GPU temperature", high=94.0, crit=99.0),
        temp("hot", GPU, 35.9, label="Hotspot temperature"),
        temp("mem", GPU, 38.0, label="Memory temperature"),
        _reading("gpuw", GPU, "Power draw", Kind.POWER, 53.9, cap=100.0),
    ]
    return readings


def _rows(readings):  # type: ignore[no-untyped-def]
    from corewatch.model import Row

    return [Row(r) for r in readings]


def test_core_groups_put_p_cores_first_and_sort_by_number() -> None:
    from corewatch.gui.overview import core_groups, cores_text, tile_name

    rows = _rows(
        [
            temp(f"c{n}", CPU, 40.0, label=label)
            for n, label in enumerate(["E-core 1", "P-core 10", "P-core 2", "E-core 0"])
        ]
        + [temp("pkg", CPU, 50.0, label="CPU package"), temp("x", CPU, 50.0, label="Core voltage")]
    )
    groups = core_groups(rows)
    assert [(name, [r.reading.label for r in members]) for name, members in groups] == [
        ("P-core", ["P-core 2", "P-core 10"]),  # by number, not as text
        ("E-core", ["E-core 0", "E-core 1"]),
    ]
    assert cores_text(groups) == "2 P-cores · 2 E-cores"
    assert cores_text(core_groups(_rows([temp("c", CPU, 40.0, label="Core 0")]))) == "1 core"
    assert cores_text(core_groups(_rows([temp(f"d{n}", CPU, 40.0, label=f"CCD {n}") for n in (1, 2)]))) == "2 CCDs"
    assert (tile_name("P-core 4"), tile_name("E-core 0"), tile_name("Core 7"), tile_name("CCD 1")) == (
        "P4",
        "E0",
        "C7",
        "CCD1",
    )
    loads = _rows([_reading("l", CPU, "P-core 3 load", Kind.LOAD, 9.0), _reading("a", CPU, "CPU load", Kind.LOAD, 9.0)])
    assert [r.reading.key for _, g in core_groups(loads, Kind.LOAD, " load") for r in g] == ["l"]


def test_heat_bands_and_legend() -> None:
    from corewatch.gui.overview import heat_band, heat_legend

    assert [heat_band(t) for t in (-5.0, 39.9, 40.0, 49.9, 50.0, 69.9, 70.0, 105.0)] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert heat_legend(False) == ["under 40 °C", "40–50 °C", "50–70 °C", "70 °C and up"]
    assert heat_legend(True) == ["under 104 °F", "104–122 °F", "122–158 °F", "158 °F and up"]


def test_gauges_summarise_the_machine() -> None:
    from corewatch.gui.overview import ACCENT, AMBER, HEAT, gauge_specs

    specs = gauge_specs(_rows(machine()), fahrenheit=False)
    assert [s.title for s in specs] == ["CPU temperature", "GPU temperature", "CPU load", "CPU + GPU power"]
    cpu, gpu, load, power = specs
    assert (cpu.key, cpu.value, cpu.unit, cpu.caption) == ("pkg", "55.0", "°C", "0–100 °C")
    assert cpu.details == ("hottest E1 · 72.0 °C",) and cpu.arcs == ((0.55, HEAT),) and cpu.celsius == 55.0
    assert (gpu.key, gpu.value) == ("gpu", "33.0")
    assert gpu.details == ("hotspot 35.9 °C", "memory 38.0 °C")
    assert (load.key, load.value, load.unit) == ("load", "7", "%")
    assert load.details == ("busiest P1 · 31 %", "fastest 5,300 MHz") and load.arcs == ((0.07, ACCENT),)
    assert power.key == "pkgw" and (power.value, power.unit) == ("128.7", "W")  # package power, not cores power
    assert power.details == ("CPU 74.8 W", "GPU 53.9 W")
    assert [role for _, role in power.arcs] == [AMBER, ACCENT]
    assert sum(fraction for fraction, _ in power.arcs) == pytest.approx(1.0)
    assert power.arcs[0][0] == pytest.approx(74.8 / 128.7)


def test_gauges_follow_fahrenheit_and_leave_out_what_is_missing() -> None:
    from corewatch.gui.overview import gauge_specs

    hot = gauge_specs(_rows(machine()), fahrenheit=True)[0]
    assert (hot.value, hot.unit, hot.caption) == ("131.0", "°F", "32–212 °F")
    assert hot.arcs[0][0] == pytest.approx(0.55)  # the ring is the same temperature either way
    assert hot.details == ("hottest E1 · 161.6 °F",)

    gpu_only = [r for r in machine() if r.device == GPU]
    specs = gauge_specs(_rows(gpu_only), fahrenheit=False)
    assert [s.title for s in specs] == ["GPU temperature", "GPU power"]
    assert specs[1].caption == "of 100 W" and specs[1].arcs[0][0] == pytest.approx(0.539)
    assert specs[1].details == ("limit 100.0 W",)

    no_cores = [r for r in machine(cores={}) if r.device == CPU and r.kind is Kind.TEMPERATURE]
    (only,) = gauge_specs(_rows(no_cores), fahrenheit=False)
    assert only.details == ("high 80.0 °C · crit 100.0 °C",)  # no cores to name: the limits instead
    unread = gauge_specs(_rows([temp("pkg", CPU, None, label="CPU package")]), fahrenheit=False)
    assert [(s.title, s.value, s.unit) for s in unread] == [("CPU temperature", "—", "")]  # kept, not dropped
    assert gauge_specs([], fahrenheit=False) == []


def test_overview_shows_gauges_and_a_heat_map_in_the_cpu_card(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [machine()])
    window.show()
    window.resize(1300, 900)
    window.refresh()
    QApplication.processEvents()
    assert [g.spec.title for g in window.gauges.gauges if g.spec] == [
        "CPU temperature",
        "GPU temperature",
        "CPU load",
        "CPU + GPU power",
    ]
    assert window.gauges.isVisible()
    cpu_card, gpu_card = window.sections[CPU], window.sections[GPU]
    heat_map = cpu_card.view.heat_map
    assert heat_map.isVisible() and heat_map.core_count() == 4
    assert gpu_card.view is None  # a GPU card is its list
    assert heat_map.height() == heat_map.heightForWidth(heat_map.width())

    window.gauges.gauges[1].clicked.emit("gpu")  # what a click on the GPU gauge sends
    assert window.selected_key == "gpu"
    qtbot.mouseClick(window.gauges.gauges[2], Qt.MouseButton.LeftButton)
    assert window.selected_key == "load"

    tile = next(t for t in heat_map._layout_tiles() if t.name == "E1")
    qtbot.mouseClick(heat_map, Qt.MouseButton.LeftButton, pos=tile.shape.boundingRect().center().toPoint())
    assert window.selected_key == "core/E-core 1" and heat_map.selected_key == "core/E-core 1"
    window.grab()  # paints gauges and tiles, including the selected one


def test_overview_steps_aside_while_filtering(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [machine()])
    window.show()
    window.refresh()
    heat_map = window.sections[CPU].view.heat_map
    window.filter.setText("P-core")
    assert not window.gauges.isVisible() and not heat_map.isVisible()
    window.filter.setText("")
    assert window.gauges.isVisible() and heat_map.isVisible()


def test_heat_map_needs_two_cores_and_ignores_renames(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [machine(cores={"Core 0": 45.0})])
    window.show()
    window.refresh()
    assert not window.sections[CPU].view.heat_map.isVisible()  # one core is no map

    other = make_window(qtbot, settings, [machine()])
    other.show()
    other.rename_sensor("core/P-core 1", "Hot one")
    other.refresh()
    heat_map = other.sections[CPU].view.heat_map
    assert heat_map.core_count() == 4  # still found by its original name
    tiles = {t.name: t for t in heat_map._layout_tiles()}
    assert tiles["P1"].label == "Hot one"  # the tooltip uses the name you gave it


def test_heat_map_wraps_into_a_honeycomb_when_narrow(qtbot) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.overview import CoreHeatMap, core_groups

    rows = _rows([temp(f"c{n}", CPU, 40.0 + n, label=f"Core {n}") for n in range(12)])
    heat_map = CoreHeatMap()
    qtbot.addWidget(heat_map)
    heat_map.set_cores(core_groups(rows), fahrenheit=False)
    tile_w = theme.px(CoreHeatMap.TILE_W)
    wide, narrow = heat_map.heightForWidth(2000), heat_map.heightForWidth(tile_w * 5)
    assert narrow > wide
    heat_map.resize(tile_w * 5, narrow)
    tiles = heat_map._layout_tiles()
    lines = sorted({t.shape.boundingRect().top() for t in tiles})
    assert len(lines) == 3  # 12 cores, 4 to a line
    first, second = (
        sorted(t.shape.boundingRect().left() for t in tiles if t.shape.boundingRect().top() == y)[0] for y in lines[:2]
    )
    assert second > first  # every other line shifted to nest
    assert all(t.shape.boundingRect().right() <= heat_map.width() for t in tiles)
    shapes = [t.shape.boundingRect() for t in tiles]
    assert not any(a.intersects(b) and a != b and a.top() == b.top() for a in shapes for b in shapes)


def test_gauge_strip_balances_its_lines_and_never_squeezes_a_gauge(qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from corewatch.gui.overview import GaugeStrip, gauge_specs

    host = QWidget()  # the strip lives in a layout, as in the window, so resizing reflows it
    qtbot.addWidget(host)
    QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
    strip = GaugeStrip()
    host.layout().addWidget(strip)
    strip.set_specs(gauge_specs(_rows(machine()), fahrenheit=False))
    host.show()
    need = max(g.needed_width() for g in strip.gauges)
    spacing = strip.grid.spacing()

    def columns_at(width: int) -> list[tuple[int, int]]:
        host.resize(width, 600)
        qtbot.waitUntil(lambda: strip.width() == width)
        QApplication.processEvents()
        for gauge in strip.gauges:  # every gauge gets the room its title and value need
            assert gauge.width() >= gauge.needed_width(), (width, gauge.spec and gauge.spec.title)
        return [strip.grid.getItemPosition(strip.grid.indexOf(g))[:2] for g in strip.gauges]

    assert columns_at(need * 4 + spacing * 3) == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert columns_at(need * 4 + spacing * 3 - 1) == [(0, 0), (0, 1), (1, 0), (1, 1)]  # room for 3: 2 x 2
    assert columns_at(need * 2 + spacing) == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert columns_at(need * 2 + spacing - 1) == [(0, 0), (1, 0), (2, 0), (3, 0)]
    three = gauge_specs(_rows([r for r in machine() if r.kind is not Kind.POWER]), fahrenheit=False)
    strip.set_specs(three)
    assert len(strip.gauges) == 3
    assert columns_at(need * 4 + spacing * 3) == [(0, 0), (0, 1), (0, 2)]


def test_gauge_width_holds_a_long_value_and_its_unit(qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QFontMetrics

    from corewatch.gui.overview import Gauge, GaugeSpec

    gauge = Gauge()
    qtbot.addWidget(gauge)
    short = GaugeSpec("CPU load", "k", "7", "%", "", (), ())
    gauge.set_spec(short)
    steady = gauge.needed_width()
    gauge.set_spec(GaugeSpec("CPU load", "k", "100", "%", "", (), ()))
    assert gauge.needed_width() == steady  # a reading gaining digits doesn't reflow the strip
    _, value_font, unit_font, _ = Gauge._fonts()
    gauge.set_spec(GaugeSpec("CPU + GPU power", "k", "1,234.5", "W", "", (), ()))
    room = gauge.needed_width() - theme.px(18 + Gauge.RING + 18 + 14)
    assert room >= QFontMetrics(value_font).horizontalAdvance("1,234.5") + QFontMetrics(unit_font).horizontalAdvance(
        "W"
    )


def test_power_never_counts_integrated_graphics_twice() -> None:
    from corewatch.gui.overview import gauge_specs, primary_gpu

    def power_of(readings):  # type: ignore[no-untyped-def]
        return next(s for s in gauge_specs(_rows(readings), fahrenheit=False) if s.title.endswith("power"))

    package = _reading("rapl/0", CPU, "Package power", Kind.POWER, 20.0)
    igpu = "GPU · Intel UHD Graphics 770"
    uncore = _reading("rapl/0:1", igpu, "Power draw", Kind.POWER, 5.0, integrated=True)
    igpu_clock = _reading("ig/clock", igpu, "Graphics clock", Kind.CLOCK, 300.0, integrated=True)
    spec = power_of([package, uncore, igpu_clock])
    assert (spec.title, spec.value, spec.key) == ("CPU power", "20.0", "rapl/0")  # the uncore is inside it

    apu = "GPU · AMD Rembrandt"
    ppt = _reading("hw/apu/power1", apu, "Power draw (CPU and GPU)", Kind.POWER, 28.0, integrated=True)
    edge = temp("hw/apu/temp1", apu, 45.0, label="GPU temperature")
    edge = _reading(edge.key, apu, edge.label, Kind.TEMPERATURE, 45.0, integrated=True)
    spec = power_of([package, ppt, edge])
    assert (spec.title, spec.value) == ("CPU power", "20.0")  # not 48 W
    spec = power_of([ppt, edge])  # no RAPL: the APU's whole-chip figure is the CPU's power
    assert (spec.title, spec.value, spec.details) == ("CPU power", "28.0", ("CPU and integrated graphics",))

    rtx = "GPU · NVIDIA GeForce RTX 4060 Laptop GPU"
    laptop = [
        ppt,
        edge,
        temp("nv/temp", rtx, 78.0, label="GPU temperature"),
        _reading("nv/power", rtx, "Power draw", Kind.POWER, 90.0, cap=115.0),
    ]
    assert primary_gpu(_rows(laptop)) == rtx  # the card of its own, though the APU comes first
    specs = {s.title: s for s in gauge_specs(_rows(laptop), fahrenheit=False)}
    assert (specs["GPU temperature"].key, specs["GPU temperature"].value) == ("nv/temp", "78.0")
    spec = specs["CPU + GPU power"]
    assert (spec.value, spec.details) == ("118.0", ("CPU 28.0 W", "GPU 90.0 W"))


def test_two_cards_take_temperature_and_power_from_the_same_one() -> None:
    from corewatch.gui.overview import gauge_specs

    arc, rtx = "GPU · Intel Arc A770", "GPU · NVIDIA GeForce RTX 4070"
    readings = [
        _reading("arc/power", arc, "Power draw", Kind.POWER, 40.0, cap=190.0),  # Arc first, no temperature
        temp("nv/temp", rtx, 50.0, label="GPU temperature"),
        _reading("nv/power", rtx, "Power draw", Kind.POWER, 120.0, cap=200.0),
    ]
    specs = {s.title: s for s in gauge_specs(_rows(readings), fahrenheit=False)}
    assert specs["GPU temperature"].key == "nv/temp"
    assert (specs["GPU power"].key, specs["GPU power"].caption) == ("nv/power", "of 200 W")


def test_power_gauge_without_a_cap_or_with_a_reading_missing() -> None:
    from corewatch.gui.overview import power_gauge

    package = _reading("rapl/0", CPU, "Package power", Kind.POWER, 65.0)
    alone = power_gauge(_rows([package]), None, fahrenheit=False)
    assert alone is not None and (alone.title, alone.caption, alone.arcs, alone.details) == ("CPU power", "", (), ())

    asleep = _reading("nv/power", GPU, "Power draw", Kind.POWER, None, cap=100.0)  # a sleeping laptop GPU
    spec = power_gauge(_rows([package, asleep]), GPU, fahrenheit=False)
    assert spec is not None and spec.title == "CPU + GPU power"  # stays put rather than flipping titles
    assert (spec.value, spec.details, spec.arcs) == ("65.0", ("CPU 65.0 W", "GPU —"), ())  # the CPU's still known
    assert power_gauge(_rows([]), None, fahrenheit=False) is None


def test_heat_map_tooltip_keyboard_and_an_unread_core(qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QToolTip

    from corewatch.gui.overview import CoreHeatMap, core_groups

    rows = _rows(
        [
            temp("c0", CPU, 45.0, label="Core 0"),
            temp("c1", CPU, None, label="Core 1"),
            temp("c2", CPU, 71.0, label="Core 2"),
        ]
    )
    heat_map = CoreHeatMap()
    qtbot.addWidget(heat_map)
    heat_map.set_cores(core_groups(rows), fahrenheit=False, labels={"c1": "Under the cooler"})
    heat_map.resize(600, heat_map.heightForWidth(600))
    heat_map.show()
    assert heat_map.accessibleName() == "Core heat map"
    assert heat_map.accessibleDescription() == "C0 45.0 °C, C1 —, C2 71.0 °C"
    heat_map.grab()  # paints the unread core as "—" without failing

    tiles = heat_map._layout_tiles()
    center = tiles[1].shape.boundingRect().center().toPoint()
    QApplication.sendEvent(heat_map, QHelpEvent(QEvent.Type.ToolTip, center, heat_map.mapToGlobal(center)))
    assert QToolTip.text() == "Under the cooler: —"

    chosen = []
    heat_map.selected.connect(chosen.append)
    heat_map.setFocus()
    for key in (Qt.Key.Key_Right, Qt.Key.Key_Right, Qt.Key.Key_Right, Qt.Key.Key_Return):
        qtbot.keyClick(heat_map, key)
    assert chosen == ["c2"]  # stops at the last core
    qtbot.keyClick(heat_map, Qt.Key.Key_Home)
    qtbot.keyClick(heat_map, Qt.Key.Key_Space)
    assert chosen == ["c2", "c0"]
    heat_map.grab()  # paints the keyboard focus ring


def test_gauges_open_with_the_keyboard(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [machine()])
    window.show()
    window.refresh()
    gauge = window.gauges.gauges[1]
    gauge.setFocus()
    qtbot.keyClick(gauge, Qt.Key.Key_Return)
    assert window.selected_key == "gpu"
    window.grab()  # paints the focused gauge


def test_heat_map_relayouts_only_when_its_cores_change(qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.overview import CoreHeatMap, core_groups

    heat_map = CoreHeatMap()
    qtbot.addWidget(heat_map)
    calls = []
    monkeypatch.setattr(heat_map, "updateGeometry", lambda: calls.append(1))
    first = _rows([temp(f"c{n}", CPU, 40.0, label=f"Core {n}") for n in range(4)])
    heat_map.set_cores(core_groups(first), fahrenheit=False)
    later = _rows([temp(f"c{n}", CPU, 60.0, label=f"Core {n}") for n in range(4)])  # new readings, same cores
    heat_map.set_cores(core_groups(later), fahrenheit=True)
    assert len(calls) == 1
    heat_map.set_cores(core_groups(later[:3]), fahrenheit=True)  # a core went away
    assert len(calls) == 2


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT])
def test_heat_colours_are_readable(palette) -> None:  # type: ignore[no-untyped-def]
    assert len(palette.heat) == 4
    for edge, tint, name in palette.heat:
        assert _contrast(name, tint) >= 4.5  # the core's name
        assert _contrast(palette.text, tint) >= 4.5  # its temperature
        assert _contrast(name, palette.surface) >= 4.5  # a gauge's value, coloured by heat
        assert _contrast(edge, palette.surface) >= 3  # the outline and the ring


def test_gauges_fit_the_narrowest_window(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [machine()])
    window.show()
    window.refresh()
    width = window.minimumSizeHint().width()
    window.resize(width, 800)
    qtbot.waitUntil(lambda: window.width() == width)
    for _ in range(3):
        QApplication.processEvents()
    viewport = window.list_area.viewport().width()
    assert window.gauges.width() <= viewport  # nothing clipped off the right
    assert all(g.width() >= g.needed_width() for g in window.gauges.gauges)


def test_power_adds_zenpowers_core_and_soc_rails() -> None:
    from corewatch.gui.overview import power_gauge

    core = _reading("zp/core", CPU, "Core power", Kind.POWER, 20.0)
    soc = _reading("zp/soc", CPU, "SoC power", Kind.POWER, 8.0)
    spec = power_gauge(_rows([core, soc]), None, fahrenheit=False)
    assert spec is not None and (spec.title, spec.value, spec.key) == ("CPU power", "28.0", "zp/core")
    unread = power_gauge(_rows([core, _reading("zp/soc", CPU, "SoC power", Kind.POWER, None)]), None, False)
    assert unread is not None and unread.value == "—"  # half a sum would read low
    package = _reading("rapl/0", CPU, "Package power", Kind.POWER, 31.0)
    spec = power_gauge(_rows([core, soc, package]), None, fahrenheit=False)
    assert spec is not None and (spec.value, spec.key) == ("31.0", "rapl/0")  # RAPL's package already has both


def test_heat_map_up_and_down_go_to_the_core_above_or_below(qtbot) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.overview import CoreHeatMap, core_groups

    rows = _rows(
        [temp(f"p{n}", CPU, 40.0, label=f"P-core {n}") for n in range(3)]
        + [temp(f"e{n}", CPU, 40.0, label=f"E-core {n}") for n in range(8)]
    )
    heat_map = CoreHeatMap()
    qtbot.addWidget(heat_map)
    heat_map.set_cores(core_groups(rows), fahrenheit=False)
    heat_map.resize(2000, heat_map.heightForWidth(2000))
    heat_map.show()
    names = [t.name for t in heat_map._layout_tiles()]

    def press(*keys: Qt.Key) -> str:
        for key in keys:
            qtbot.keyClick(heat_map, key)
        return names[heat_map.focus_index]

    heat_map.focus_index = names.index("P2")
    assert press(Qt.Key.Key_Down) == "E2"  # the nearer of the two nested below, leaning right
    assert press(Qt.Key.Key_Up) == "P2"  # and back
    heat_map.focus_index = names.index("E7")
    assert press(Qt.Key.Key_Up) == "P2"  # the nearest across, not a line's length back through the list
    assert press(Qt.Key.Key_Up) == "P2"  # nowhere further up
    heat_map.focus_index = names.index("E5")
    assert press(Qt.Key.Key_Down) == "E5"  # nowhere further down


def test_focus_rings_show_for_the_keyboard_not_for_clicks(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [machine()])
    with qtbot.waitActive(window):
        window.show()
        window.activateWindow()
    window.refresh()
    gauge = window.gauges.gauges[0]
    qtbot.mouseClick(gauge, Qt.MouseButton.LeftButton)
    assert gauge.hasFocus() and not gauge.keyboard_focus
    gauge.clearFocus()
    gauge.setFocus(Qt.FocusReason.TabFocusReason)
    assert gauge.keyboard_focus
    window.grab()

    heat_map = window.sections[CPU].view.heat_map
    tile = heat_map._layout_tiles()[0]
    qtbot.mouseClick(heat_map, Qt.MouseButton.LeftButton, pos=tile.shape.boundingRect().center().toPoint())
    assert heat_map.hasFocus() and not heat_map.keyboard_focus
    qtbot.keyClick(heat_map, Qt.Key.Key_Right)  # arrows after a click: now it's the keyboard's turn
    assert heat_map.keyboard_focus
    window.grab()


# ----- device cards: each opens on its own view, the full list on demand ---------------------


def _view_data(rows, everything=None, labels=None, fahrenheit=False):  # type: ignore[no-untyped-def]
    from corewatch.gui.cards import ViewData

    return ViewData(rows, everything if everything is not None else rows, labels or {}, fahrenheit)


def _gathered(key: str, origin: str, label: str, kind: Kind, value: float | None, **extra):  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    device = "Storage" if origin.startswith(("NVMe", "Disk")) else "Fans"
    return Reading(key, device, label, kind, value, origin=origin, **extra)


def test_fan_view_pairs_each_fan_with_how_hard_it_is_driven() -> None:
    from corewatch.gui.cards import FanView
    from corewatch.model import Row, Stats

    rows = [
        Row(_gathered("mb", "Motherboard · Z790", "Fan 2", Kind.FAN, 1500.0)),
        Row(_gathered("mb-pwm", "Motherboard · Z790", "Fan control 2", Kind.FAN_DUTY, 58.0, companion="mb")),
        # Two graphics cards, both with a "Fan 1": the Fans card names them apart.
        Row(_gathered("rtx", "GPU · RTX 4070", "GPU fan 1 (RTX 4070)", Kind.FAN, 2320.0)),
        Row(_gathered("amd-pct", "GPU · AMD", "GPU fan 1 speed (AMD)", Kind.FAN_DUTY, 40.0)),
        Row(_gathered("rtx-pct", "GPU · RTX 4070", "GPU fan 1 speed (RTX 4070)", Kind.FAN_DUTY, 73.0)),
        Row(_gathered("amd", "GPU · AMD", "GPU fan 1 (AMD)", Kind.FAN, 900.0)),
        Row(_gathered("free", "Motherboard · Z790", "Fan 3", Kind.FAN, 900.0), Stats(maximum=1800.0)),
        Row(_gathered("still", "Motherboard · Z790", "Fan 4", Kind.FAN, 0.0)),
        Row(_gathered("unread", "Motherboard · Z790", "Fan 5", Kind.FAN, None)),
        # A driver that reports how hard a fan is driven but not its speed.
        Row(_gathered("only-pct", "GPU · GTX 1080", "GPU fan 2 speed", Kind.FAN_DUTY, 35.0)),
        # A pump header: its control names a fan input the board doesn't have.
        Row(_gathered("pump", "Motherboard · Z790", "Fan control 6", Kind.FAN_DUTY, 100.0, companion="gone")),
    ]
    view = FanView()
    view.set_data(_view_data(rows, labels={"mb": "Front intake"}))
    got = {i.key: (i.name, i.rpm, i.unit, i.share, i.where, i.spinning, i.driven) for i in view.items}  # type: ignore[attr-defined]
    assert list(got) == ["mb", "rtx", "amd", "free", "still", "unread", "only-pct", "pump"]  # in order
    assert got == {
        "mb": ("Front intake", "1,500", "RPM", 0.58, "board header · set to 58 %", True, True),  # renamed
        "rtx": ("GPU fan 1 (RTX 4070)", "2,320", "RPM", 0.73, "RTX 4070 · set to 73 %", True, True),
        "amd": ("GPU fan 1 (AMD)", "900", "RPM", 0.40, "AMD · set to 40 %", True, True),
        "free": ("Fan 3", "900", "RPM", 0.5, "board header", True, False),  # against its own fastest
        "still": ("Fan 4", "0", "RPM", 0.0, "board header", False, False),
        "unread": ("Fan 5", "—", "RPM", None, "board header", False, False),
        "only-pct": ("GPU fan 2 speed", "35", "%", 0.35, "GTX 1080", True, True),
        "pump": ("Fan control 6", "100", "%", 1.0, "board header", True, True),
    }
    assert view.items[0].tooltip == "Front intake: 1,500 RPM"
    assert view.accessibleName() == "Fans"
    assert view.accessibleDescription().startswith("Front intake; GPU fan 1 (RTX 4070); GPU fan 1 (AMD); Fan 3")
    before = view.accessibleDescription()
    view.set_data(
        _view_data([Row(replace(r.reading, value=1.0), r.stats) for r in rows], labels={"mb": "Front intake"})
    )
    assert view.accessibleDescription() == before  # the readings changed, the description didn't


def test_storage_view_has_a_line_per_drive() -> None:
    from corewatch.gui.cards import StorageView
    from corewatch.model import Row, Status

    samsung = "NVMe nvme0 · Samsung SSD 980"
    rows = [
        Row(_gathered("sata", "Disk · ST4000", "ST4000 Temperature", Kind.TEMPERATURE, 61.0, high=60.0)),
        Row(_gathered("n10", "NVMe nvme10 · WD", "nvme10 Composite", Kind.TEMPERATURE, 30.0)),
        Row(_gathered("n10s1", "NVMe nvme10 · WD", "nvme10 Sensor 1", Kind.TEMPERATURE, 29.0)),  # cooler
        Row(_gathered("n0", samsung, "nvme0 Composite", Kind.TEMPERATURE, 34.0, high=81.8, crit=84.8)),
        Row(_gathered("n0s1", samsung, "nvme0 Sensor 1", Kind.TEMPERATURE, 38.9)),
        Row(_gathered("n0s2", samsung, "nvme0 Sensor 2", Kind.TEMPERATURE, 72.0, high=70.0)),
        Row(_gathered("n2", "NVMe nvme2 · Kingston", "nvme2 Composite", Kind.TEMPERATURE, None)),
        Row(_gathered("n2s1", "NVMe nvme2 · Kingston", "nvme2 Sensor 1", Kind.TEMPERATURE, 41.0)),
    ]
    view = StorageView()
    view.set_data(_view_data(rows))
    got = [(i.key, i.model, i.name, i.temperature, i.high, i.crit, i.hottest, i.status) for i in view.items]  # type: ignore[attr-defined]
    assert got == [  # by name, numbers as numbers
        ("n0", "Samsung SSD 980", "nvme0", 34.0, 81.8, 84.8, 72.0, Status.WARNING),  # its hot sensor shows
        ("n2", "Kingston", "nvme2", None, None, None, 41.0, Status.OK),  # unread bar: the other still shows
        ("n10", "WD", "nvme10", 30.0, None, None, None, Status.OK),  # a cooler sensor isn't worth a mention
        ("sata", "ST4000", "", 61.0, 60.0, None, None, Status.WARNING),  # a model is its own short name
    ]
    view.set_data(_view_data(rows, fahrenheit=True))
    assert view.items[0].tooltip == "Samsung SSD 980 (nvme0): 93.2 °F"


def test_board_view_shows_its_named_voltage_rails() -> None:
    from corewatch.gui.cards import RailView

    board = "Motherboard · Z790"
    rows = _rows(
        [
            _reading("vcore", board, "CPU core (Vcore)", Kind.VOLTAGE, 1.152),
            _reading("in3", board, "in3", Kind.VOLTAGE, 0.9),
            _reading("v4", board, "Voltage 4", Kind.VOLTAGE, 1.0),
            _reading("v12", board, "+12V", Kind.VOLTAGE, 12.0),
            _reading("vin", board, "Vin 2", Kind.VOLTAGE, None),
            temp("t", board, 30.0, label="Temperature 1"),
        ]
    )
    view = RailView()
    view.set_data(_view_data(rows, labels={"v12": "12 V rail"}))
    assert [(t.title, t.value, t.unit) for t in view.items] == [  # type: ignore[attr-defined]
        ("CPU core (Vcore)", "1.152", "V"),
        ("12 V rail", "12.000", "V"),
        ("Vin 2", "—", ""),
    ]
    view.set_data(_view_data(rows[1:3]))
    assert view.is_empty()  # only unnamed rails: the card shows its list instead


def test_cpu_tiles_show_the_headline_numbers() -> None:
    from corewatch.gui.cards import CpuTileView

    cpu_rows = [r for r in _rows(machine()) if r.reading.device == CPU]
    board_vcore = _rows([_reading("vcore", "Motherboard · Z790", "CPU core (Vcore)", Kind.VOLTAGE, 1.136)])
    view = CpuTileView()
    view.set_data(_view_data(cpu_rows, everything=cpu_rows + board_vcore))
    assert [(t.title, t.value, t.unit, t.key) for t in view.items] == [  # type: ignore[attr-defined]
        ("Package power", "74.8", "W", "pkgw"),
        ("Cores power", "64.1", "W", "corew"),
        ("Fastest clock", "5,300", "MHz", "clock/p0"),
        ("Vcore", "1.136", "V", "vcore"),  # from the board's chip
        ("Load", "7", "%", "load"),
        ("Hottest", "E1 72.0", "°C", "core/E-core 1"),
    ]
    zen = _rows(
        [_reading("zv", CPU, "Core voltage", Kind.VOLTAGE, 1.2), _reading("x", CPU, "CPU load", Kind.LOAD, 3.0)]
    )
    view.set_data(_view_data(zen, fahrenheit=True))
    assert [(t.title, t.value) for t in view.items] == [("Vcore", "1.200"), ("Load", "3")]  # type: ignore[attr-defined]
    hot = _rows([temp("c0", CPU, 40.0, label="Core 0"), temp("c1", CPU, 50.0, label="Core 1")])
    view.set_data(_view_data(hot, fahrenheit=True))
    assert [(t.title, t.value, t.unit) for t in view.items] == [("Hottest", "C1 122.0", "°F")]  # type: ignore[attr-defined]


def test_network_view_shows_traffic_and_its_temperatures() -> None:
    from corewatch.gui.cards import NetworkView

    nic = "Network · enp7s0"
    rows = _rows(
        [
            _reading("rx", nic, "Download", Kind.THROUGHPUT, 12_300.0),
            _reading("tx", nic, "Upload", Kind.THROUGHPUT, 0.0),
            temp("phy", nic, 50.0, label="PHY Temperature"),
            temp("mac", nic, 51.0, label="MAC temperature"),
            _reading("other", nic, "Link use", Kind.LOAD, 4.0),
        ]
    )
    view = NetworkView()
    view.set_data(_view_data(rows, labels={"tx": "Sent"}))
    assert [(i.key, i.title, i.value) for i in view.items] == [  # type: ignore[attr-defined]
        ("rx", "Download", "12.3 KB/s"),
        ("tx", "Sent", "0 B/s"),
    ]
    assert view.temperatures() == "PHY 50.0 °C · MAC 51.0 °C"
    with_footer = view.heightForWidth(400)
    view.set_data(_view_data(rows[:2] + rows[4:]))
    assert view.temperatures() == "" and view.footer_height() == 0
    assert view.heightForWidth(400) < with_footer


def full_machine() -> list:  # type: ignore[type-arg]
    """``machine()`` plus the cards that open on a view of their own: fans, drives, a board."""
    from corewatch.model import Reading

    board = "Motherboard · Z790"
    return [
        *machine(),
        Reading("fan2", board, "Fan 2", Kind.FAN, 1500.0),
        Reading("fan7", board, "Fan 7", Kind.FAN, 3000.0),
        Reading("vcore", board, "CPU core (Vcore)", Kind.VOLTAGE, 1.136),
        Reading("v12", board, "+12V", Kind.VOLTAGE, 12.0),
        temp("n0", "NVMe nvme0 · Samsung SSD 980", 34.0, label="Composite", high=81.8),
        temp("n1", "NVMe nvme1 · Samsung SSD 970", 30.0, label="Composite", high=84.8),
        temp("acpi", "Motherboard · ACPI thermal zone", 27.8, label="Temperature 1"),
    ]


def test_cards_open_on_their_view_with_the_full_list_on_demand(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [full_machine()])
    window.resize(1300, 900)
    window.show()
    window.refresh()
    assert list(window.sections) == [
        CPU,
        GPU,
        "Fans",
        "Storage",
        "Motherboard · Z790",
        "Motherboard · ACPI thermal zone",
    ]
    cpu, gpu, acpi = window.sections[CPU], window.sections[GPU], window.sections["Motherboard · ACPI thermal zone"]
    assert cpu.view.isVisible() and not cpu.grid.isVisible() and cpu.table_button.isVisible()
    assert gpu.view is None and gpu.grid.isVisible() and not gpu.table_button.isVisible()
    assert acpi.view.is_empty() and acpi.grid.isVisible() and not acpi.table_button.isVisible()  # no named rails
    assert [i.key for i in window.sections["Storage"].view.items] == ["n0", "n1"]

    cpu.table_button.click()
    assert cpu.view.isVisible() and cpu.grid.isVisible() and cpu.table_button.isChecked()
    assert json.loads(settings.value("expanded_cards")) == [CPU]

    window.filter.setText("package")  # while filtering, a card lists what matches
    assert not cpu.view.isVisible() and cpu.grid.isVisible() and not cpu.table_button.isVisible()
    window.filter.clear()
    assert cpu.view.isVisible() and cpu.table_button.isVisible()

    again = make_window(qtbot, settings, [full_machine()])
    again.show()
    again.refresh()
    assert again.sections[CPU].grid.isVisible() and again.sections[CPU].table_button.isChecked()
    assert not again.sections["Fans"].grid.isVisible()
    again.sections[CPU].table_button.click()
    assert not again.sections[CPU].grid.isVisible()
    assert json.loads(settings.value("expanded_cards")) == []


def test_half_width_cards_pair_up_when_both_fit(qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QWidget

    from corewatch.gui.cards import CardDeck

    deck = CardDeck()
    qtbot.addWidget(deck)
    wide, a, b, c = QWidget(), QWidget(), QWidget(), QWidget()
    deck.set_cards([(a, False), (wide, True), (b, False), (c, False)])  # placed before the deck shows
    places = lambda width: [(r, col, cs) for _, r, col, _, cs in deck.arrangement(width)]  # noqa: E731
    assert places(1000) == [(0, 0, 1), (1, 0, 2), (2, 0, 1), (2, 1, 1)]  # a wide card starts a line
    assert places(851) == [(0, 0, 1), (1, 0, 1), (2, 0, 1), (3, 0, 1)]  # 2 x 420 + 12 doesn't fit
    assert places(852) == places(1000)
    b.hide()
    assert places(1000) == [(0, 0, 1), (1, 0, 2), (2, 0, 1)]  # a hidden card leaves no hole
    deck.set_need(c, 600)  # a card showing its full list needs more room beside another
    assert deck.columns_for(1000) == 1
    deck.set_need(c, 420)
    assert deck.columns_for(1000) == 2
    deck.set_cards([(wide, True)])
    assert deck.columns_for(500) == 1  # no half cards: nothing to pair


def test_the_deck_is_as_tall_as_its_cards_and_no_taller(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [full_machine()])
    window.resize(1300, 700)
    window.show()
    window.refresh()
    QApplication.processEvents()
    fans, storage = window.sections["Fans"], window.sections["Storage"]
    assert fans.geometry().top() == storage.geometry().top()  # side by side
    page = window.list_area.widget()
    assert page.height() > window.list_area.viewport().height()  # it scrolls, so its height is its own
    content = window.deck.geometry().bottom()
    assert page.height() - content < 20  # not a long blank tail sized for one card per line


def test_compact_views_open_a_sensor_by_click_or_keyboard(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [full_machine()])
    window.resize(1300, 900)
    window.show()
    window.refresh()
    QApplication.processEvents()
    fans = window.sections["Fans"].view
    second = fans.place(fans.width())[1]
    qtbot.mouseClick(fans, Qt.MouseButton.LeftButton, pos=second.center().toPoint())
    assert window.selected_key == "fan7" and fans.selected_key == "fan7"
    assert window.sections["Storage"].view.selected_key == "fan7"  # one highlight across the cards

    storage = window.sections["Storage"].view
    storage.setFocus(Qt.FocusReason.TabFocusReason)
    for key in (Qt.Key.Key_End, Qt.Key.Key_Return):
        qtbot.keyClick(storage, key)
    assert window.selected_key == "n1"
    for key in (Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_Right, Qt.Key.Key_Left, Qt.Key.Key_Enter):
        qtbot.keyClick(storage, key)
    assert window.selected_key == "n0"
    tiles = window.sections[CPU].view.tiles
    first = tiles.place(tiles.width())[0]
    qtbot.mouseClick(tiles, Qt.MouseButton.LeftButton, pos=first.center().toPoint())
    assert window.selected_key == "pkgw"
    window.grab()  # paints every view, the selected tile included


def test_no_card_ever_opens_as_a_window_of_its_own(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.sensors import CategorySection

    class Watch(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.windows: list[str] = []

        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            if event.type() == QEvent.Type.Show and isinstance(watched, CategorySection) and watched.isWindow():
                self.windows.append(watched.device)
            return False

    watch = Watch()
    QApplication.instance().installEventFilter(watch)
    try:
        window = make_window(qtbot, settings, [full_machine()])
        window.refresh()  # the first cards are built before the window shows
        window.show()
        QApplication.processEvents()
    finally:
        QApplication.instance().removeEventFilter(watch)
    assert watch.windows == []
    assert all(window.deck.grid.indexOf(section) >= 0 for section in window.sections.values())


def _painted_colours(widget) -> set[str]:  # type: ignore[no-untyped-def]
    image = widget.grab().toImage()
    return {image.pixelColor(x, y).name() for x in range(image.width()) for y in range(image.height())}


def test_amber_on_a_fan_bar_means_driven_hard_not_at_its_own_top_speed(qtbot) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.cards import FanView
    from corewatch.model import Row

    view = FanView()
    qtbot.addWidget(view)
    view.resize(400, 80)
    warning = QColor(theme.current_theme().warning).name()
    view.set_data(_view_data([Row(_gathered("f", "Motherboard · Z790", "Fan 3", Kind.FAN, 600.0))]))
    assert view.items[0].share == 1.0  # type: ignore[attr-defined]  # its fastest yet: a full bar...
    assert warning not in _painted_colours(view)  # ...but nothing says it's working hard
    view.set_data(_view_data([Row(_gathered("p", "GPU · GTX 1080", "GPU fan speed", Kind.FAN_DUTY, 95.0))]))
    assert warning in _painted_colours(view)


def test_tile_arrows_move_by_line_and_let_the_page_scroll_at_the_ends(qtbot) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.cards import RailView

    rails = _rows([_reading(f"v{n}", "Motherboard · Z790", f"+{n}V", Kind.VOLTAGE, float(n)) for n in range(7)])
    view = RailView()
    qtbot.addWidget(view)
    view.resize(500, 300)  # three to a line
    view.set_data(_view_data(rails))
    assert view.columns() == 3
    view.setFocus(Qt.FocusReason.TabFocusReason)
    moves = []
    for key in (Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Up, Qt.Key.Key_Up):
        qtbot.keyClick(view, key)
        moves.append(view.focus_index)
    assert moves == [3, 6, 6, 3, 0, 0]  # down a line, past the last item stays put, up a line
    qtbot.keyClick(view, Qt.Key.Key_Left)
    assert view.focus_index == 0
    from PySide6.QtGui import QKeyEvent

    cases = [
        (0, Qt.Key.Key_Up, 0, True),  # first line: up goes to the page
        (0, Qt.Key.Key_Down, 3, False),
        (4, Qt.Key.Key_Down, 6, False),  # the last line is short: down to its last item
        (5, Qt.Key.Key_Down, 6, False),
        (6, Qt.Key.Key_Down, 6, True),  # last line: down goes to the page
    ]
    for index, key, lands, passed_on in cases:
        view.focus_index = index
        event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        view.keyPressEvent(event)
        assert (view.focus_index, event.isAccepted()) == (lands, not passed_on), (index, key)


def test_keyboard_scrolls_the_focused_item_into_sight(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [full_machine()])
    window.resize(1300, 520)
    window.show()
    window.refresh()
    QApplication.processEvents()
    storage = window.sections["Storage"].view
    viewport = window.list_area.viewport()
    assert window.list_area.verticalScrollBar().value() == 0
    assert storage.mapTo(viewport, QPoint(0, 0)).y() > viewport.height()  # starts out of sight
    storage.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.keyClick(storage, Qt.Key.Key_End)
    QApplication.processEvents()
    rect = storage.place(storage.width())[-1]
    top = storage.mapTo(viewport, rect.topLeft().toPoint()).y()
    assert top >= 0 and top + rect.height() <= viewport.height()


def test_a_reading_alone_never_relayouts_a_view(qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.cards import CpuTileView, NetworkView

    calls = []
    for cls in (CpuTileView, NetworkView):
        monkeypatch.setattr(cls, "updateGeometry", lambda self: calls.append(type(self).__name__))
    tiles = CpuTileView()
    qtbot.addWidget(tiles)
    tiles.set_data(_view_data(_rows(machine())))
    assert calls == ["CpuTileView"]
    hot = machine(cores={"P-core 0": 90.0, "P-core 1": 56.0, "E-core 0": 38.0, "E-core 1": 72.0})
    tiles.set_data(_view_data(_rows(hot)))  # the hottest core changes, the tiles don't
    assert tiles.items[-1].value == "P0 90.0"  # type: ignore[attr-defined]
    assert calls == ["CpuTileView"]

    nic = "Network · enp7s0"
    traffic = [_reading("rx", nic, "Download", Kind.THROUGHPUT, 0.0)]
    network = NetworkView()
    qtbot.addWidget(network)
    network.set_data(_view_data(_rows(traffic)))
    network.set_data(_view_data(_rows(traffic)))
    assert calls == ["CpuTileView", "NetworkView"]
    network.set_data(_view_data(_rows([*traffic, temp("phy", nic, 50.0, label="PHY Temperature")])))
    assert calls == ["CpuTileView", "NetworkView", "NetworkView"]  # a footer turned up: taller


def test_cpu_view_puts_its_tiles_under_the_heat_map_when_narrow(qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QBoxLayout

    from corewatch.gui.cards import CpuView

    view = CpuView()
    qtbot.addWidget(view)
    view.set_data(_view_data(_rows(machine())))
    view.show()
    view.resize(819, 400)
    assert view.box.direction() == QBoxLayout.Direction.TopToBottom
    view.resize(900, 400)
    assert view.box.direction() == QBoxLayout.Direction.LeftToRight
    view.resize(819, 400)
    assert view.box.direction() == QBoxLayout.Direction.TopToBottom
    view.resize(820, 400)
    assert view.box.direction() == QBoxLayout.Direction.LeftToRight


def test_cards_are_never_paired_narrower_than_they_can_go(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QWidget

    from corewatch.gui.cards import CardDeck
    from corewatch.model import Reading

    deck = CardDeck()
    qtbot.addWidget(deck)
    a, b = QWidget(), QWidget()
    deck.set_cards([(a, False), (b, False)])
    assert deck.columns_for(1000) == 2
    b.setMinimumWidth(600)
    assert deck.columns_for(1000) == 1 and deck.columns_for(1212) == 2

    board = "Motherboard · ROG STRIX Z790-A GAMING WIFI"
    readings = [
        *machine(),
        Reading("fan2", board, "Fan 2", Kind.FAN, 1500.0),
        Reading("vcore", board, "CPU core (Vcore)", Kind.VOLTAGE, 1.136),
        temp("n0", "NVMe nvme0 · Samsung SSD 980", 34.0, label="Composite"),
        Reading("rx", "Network · enp7s0", "Download", Kind.THROUGHPUT, 0.0),
    ]
    window = make_window(qtbot, settings, [readings])
    window.resize(1100, 900)
    window.show()
    window.refresh()
    QApplication.processEvents()
    title = window.sections[board].title
    assert window.deck.columns_for(window.deck.width()) == 2  # a long board name doesn't stop pairing
    assert title.shown_text().endswith("…") and title.toolTip() == board  # it's cut, and whole on hover
    assert window.sections["Fans"].title.toolTip() == ""
    for width in range(1300, 850, -50):
        window.resize(width, 900)
        qtbot.waitUntil(lambda w=width: window.width() == max(w, window.minimumSizeHint().width()))
        QApplication.processEvents()
        squeezed = [d for d, s in window.sections.items() if s.isVisible() and s.width() < s.minimumSizeHint().width()]
        assert squeezed == [], width


def test_a_card_showing_its_list_needs_the_lists_width_beside_another(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [full_machine()])
    window.resize(1300, 900)
    window.show()
    window.refresh()
    fans = window.sections["Fans"]
    assert window.deck.needs[id(fans)] == theme.px(420)
    fans.table_button.click()
    assert window.deck.needs[id(fans)] == max(theme.px(420), fans.grid.cell_min_width() + 28)
    assert window.deck.needs[id(fans)] > theme.px(420)  # the list is the wider of the two here


def test_side_by_side_cards_line_their_titles_up(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [full_machine()])
    window.resize(1300, 900)
    window.show()
    window.refresh()
    QApplication.processEvents()
    board, acpi = window.sections["Motherboard · Z790"], window.sections["Motherboard · ACPI thermal zone"]
    assert board.table_button.isVisible() and not acpi.table_button.isVisible()
    assert board.geometry().top() == acpi.geometry().top()
    centre = lambda label: label.mapTo(window, QPoint(0, label.height() // 2)).y()  # noqa: E731  # text sits mid-label
    assert centre(board.title) == centre(acpi.title)
    assert board.header.height() == acpi.header.height()


def test_the_deck_forgets_the_widths_of_cards_it_no_longer_has(qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QWidget

    from corewatch.gui.cards import CardDeck

    deck = CardDeck()
    qtbot.addWidget(deck)
    a, b = QWidget(), QWidget()
    deck.set_cards([(a, False), (b, False)])
    deck.set_need(a, 500)
    deck.set_need(b, 600)
    deck.set_cards([(b, False)])
    assert deck.needs == {id(b): 600}


# ----- the focus view: one sensor across the whole window --------------------------------------


def test_dial_scale_words_and_headroom() -> None:
    from corewatch.gui.focus import dial_full_scale, headroom, reading_word, trend_word
    from corewatch.model import Reading

    def r(kind: Kind, value: float | None, **extra: float | None) -> Reading:
        return Reading("k", CPU, "x", kind, value, **extra)

    assert [
        dial_full_scale(r(Kind.TEMPERATURE, 50.0, crit=100.0)),
        dial_full_scale(r(Kind.TEMPERATURE, 50.0, crit=105.0)),
        dial_full_scale(r(Kind.TEMPERATURE, 50.0, high=80.0)),  # never under 100 °C
        dial_full_scale(r(Kind.TEMPERATURE, 50.0)),
        dial_full_scale(r(Kind.LOAD, 5.0)),
        dial_full_scale(r(Kind.FAN_DUTY, 5.0)),
        dial_full_scale(r(Kind.POWER, 53.9, cap=150.0)),
    ] == [110.0, 115.0, 100.0, 100.0, 100.0, 100.0, 150.0]
    no_scale = [r(Kind.POWER, 53.9), r(Kind.POWER, 5.0, cap=0.0), r(Kind.VOLTAGE, 1.2), r(Kind.FAN, 900.0)]
    assert [dial_full_scale(reading) for reading in no_scale] == [None] * 4  # a cap of 0 is no scale

    assert [reading_word(r(Kind.TEMPERATURE, v, high=80.0, crit=100.0)) for v in (35.0, 45.0, 55.0, 72.0)] == [
        "cool",
        "mild",
        "warm",
        "hot",
    ]
    assert reading_word(r(Kind.TEMPERATURE, 85.0, high=80.0, crit=100.0)) == "past its limit"
    assert reading_word(r(Kind.TEMPERATURE, 101.0, high=80.0, crit=100.0)) == "critical"
    assert reading_word(r(Kind.LOAD, 50.0)) == "" and reading_word(r(Kind.TEMPERATURE, None)) == ""

    assert headroom(r(Kind.TEMPERATURE, 55.0, high=80.0, crit=100.0), False) == ("25.0 °C", "to the high limit")
    assert headroom(r(Kind.TEMPERATURE, 55.0, high=80.0), True) == (
        "45.0 °F",
        "to the high limit",
    )  # a gap, not a temperature
    assert headroom(r(Kind.TEMPERATURE, 85.0, high=80.0), False) == ("5.0 °C", "past the high limit")
    assert headroom(r(Kind.TEMPERATURE, 60.0, crit=100.0), False) == ("40.0 °C", "to critical")
    assert headroom(r(Kind.POWER, 53.9, cap=100.0), False) == ("46.1 W", "to its limit")
    assert headroom(r(Kind.VOLTAGE, 3.3, low=3.0), False) == ("0.300 V", "above the low limit")
    assert headroom(r(Kind.CLOCK, 5300.0), False) == ("—", "no limit reported")
    assert headroom(r(Kind.TEMPERATURE, None, high=80.0), False) == ("—", "no reading")

    assert [trend_word(t, Kind.TEMPERATURE) for t in ("↑ +2.1 °C/min", "↓ -0.4 °C/min", "→ steady", "—")] == [
        "warming",
        "cooling",
        "steady",
        "",
    ]
    assert [trend_word(t, Kind.FAN) for t in ("↑ +40 RPM/min", "↓ -40 RPM/min")] == ["rising", "falling"]


def test_tiles_even_out_their_lines_and_the_average_follows_the_last_minute() -> None:
    from corewatch.gui.focus import balanced
    from corewatch.gui.widgets import rolling_average

    assert [balanced(n, fits) for n, fits in ((8, 7), (16, 7), (3, 7), (8, 8), (9, 4), (5, 1), (0, 5), (4, 0))] == [
        4,  # 4 and 4, not 7 and 1
        6,  # 6, 6 and 4
        3,
        8,
        3,
        1,
        1,
        1,
    ]
    points = [(0.0, 10.0), (30.0, 20.0), (61.0, 30.0), (90.0, 40.0)]
    assert rolling_average(points, 60.0) == [(0.0, 10.0), (30.0, 15.0), (61.0, 25.0), (90.0, 30.0)]
    assert rolling_average([], 60.0) == []


def test_focus_stats_say_what_each_number_means() -> None:
    from corewatch.gui.focus import focus_stats
    from corewatch.model import Reading, Row, Stats

    stats = Stats()
    for t, v in ((100.0, 41.0), (101.0, 62.0), (102.0, 50.0)):
        stats.add(v, t)
    row = Row(Reading("pkg", CPU, "CPU package", Kind.TEMPERATURE, 50.0, high=80.0, crit=100.0), stats)
    shown = focus_stats(row, 102.0, False, lambda t: t)
    assert [(s.title, s.value) for s in shown] == [
        ("Lowest", "41.0 °C"),
        ("Highest", "62.0 °C"),
        ("Session average", "51.0 °C"),
        ("Last minute", "51.0 °C"),
        ("Last 5 min", "51.0 °C"),
        ("Variation", "± 8.6 °C"),
        ("Past a limit", "0 s"),
        ("At critical", "0 s"),
    ]
    assert shown[0].note.startswith("at ") and shown[2].note.startswith("since ")
    assert (shown[6].note, shown[7].note) == ("never past 80.0 °C", "never at 100.0 °C")
    stats.seconds_warning, stats.seconds_critical = 12.0, 3.0
    hot = focus_stats(row, 102.0, True, lambda t: t)
    assert (hot[6].value, hot[6].note) == ("15 s", "past the high limit")
    assert (hot[7].value, hot[7].note) == ("3 s", "at or above 212.0 °F")
    bare = Row(Reading("clk", CPU, "P-core 0 clock", Kind.CLOCK, 5300.0), stats)
    assert [(s.value, s.note) for s in focus_stats(bare, 102.0, False, lambda t: t)[6:]] == [
        ("no limit", "none reported"),
        ("no limit", "none reported"),
    ]
    only_crit = Row(Reading("t", CPU, "x", Kind.TEMPERATURE, 50.0, crit=100.0), Stats())
    assert focus_stats(only_crit, 102.0, False, lambda t: t)[6].note == "never at 100.0 °C"


def test_the_focus_chart_keeps_the_limits_in_view(qtbot) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.widgets import HistoryChart
    from corewatch.model import Reading, Row, Stats

    stats = Stats()
    for t in range(10):
        stats.add(40.0 + t % 3, float(t))
    row = Row(Reading("pkg", CPU, "CPU package", Kind.TEMPERATURE, 41.0, high=80.0, crit=100.0), stats)
    plain, focus = HistoryChart(), HistoryChart(focus=True)
    for chart in (plain, focus):
        qtbot.addWidget(chart)
        chart.resize(600, 300)
        chart.set_data(row, 60.0, False, 5.0, 9.0)
    points = stats.window(60.0, 9.0)
    assert plain.scale(points)[1] < 80.0  # the overview's chart zooms in on the readings
    low, high = focus.scale(points)
    assert low <= 40.0 and high >= 100.0  # the focus chart shows how far off the limits are
    focus.grab()
    focus.hover_x = 300.0
    focus.grab()  # paints the zone, the named limits, the average and the hover bubble


def test_the_dial_names_its_limits_and_rings_only_what_has_a_scale(qtbot) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.focus import Dial
    from corewatch.model import Reading

    dial = Dial()
    qtbot.addWidget(dial)
    dial.resize(dial.sizeHint())
    dial.set_reading(Reading("pkg", CPU, "CPU package", Kind.TEMPERATURE, 55.0, high=80.0, crit=100.0), False)
    assert dial.legend() == [("warning", "high 80.0 °C"), ("critical", "critical 100.0 °C")]
    assert dial.accessibleName() == "Now 55.0 °C warm"
    ringed = _painted_colours(dial)
    dial.set_reading(Reading("v", CPU, "Core voltage", Kind.VOLTAGE, 1.2, high=1.5), False)
    assert dial.legend() == []  # no ring, so nothing on it to name
    assert dial.accessibleName() == "Now 1.200 V"
    edge = QColor(theme.current_theme().edge).name()
    assert edge in ringed and edge not in _painted_colours(dial)  # the ring's track is gone


def _open_window(qtbot, settings, script=None):  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, script or [full_machine()])
    window.resize(1300, 900)
    window.show()
    window.refresh()
    window.refresh()
    QApplication.processEvents()
    return window


def test_focus_view_opens_from_the_detail_panel_and_esc_goes_back(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = _open_window(qtbot, settings)
    focus = window.focus
    assert window.pages.currentWidget() is window.splitter and window.focus_key is None
    assert window.detail.focus_button.accessibleName() == "Focus view"
    window.detail.focus_button.click()  # the selected sensor: the CPU package
    assert window.pages.currentWidget() is focus and window.focus_key == "pkg"
    assert (focus.device.text(), focus.title.text()) == ("CPU · I7", "CPU package")
    assert focus.cores_panel.isVisible() and len(focus.cores.items) == 4  # every core, on one scale
    assert [(i.name, i.value, i.note) for i in focus.cores.items] == [  # type: ignore[attr-defined]
        ("P-core 0", "41°", "max 41° · 5,300 MHz"),
        ("P-core 1", "56°", "max 56° · 2,100 MHz"),
        ("E-core 0", "38°", "max 38°"),  # no clock reported for it
        ("E-core 1", "72°", "max 72°"),
    ]
    assert focus.cores.selected_key == "pkg"
    assert [s.title for s in focus.outlook.stats] == ["Headroom", "Trend"]
    assert focus.outlook.stats[0].value == "25.0 °C"
    QApplication.processEvents()
    assert focus.back_button.hasFocus()
    qtbot.keyClick(focus.back_button, Qt.Key.Key_Escape)
    assert window.pages.currentWidget() is window.splitter and window.focus_key is None

    grid = window.sections[GPU].grid
    cell = next(c for c in grid.cells() if c.key == "hot")
    qtbot.mouseDClick(grid, Qt.MouseButton.LeftButton, pos=cell.rect.center())
    assert window.focus_key == "hot" and window.selected_key == "hot"
    assert focus.title.text() == "Hotspot temperature" and not focus.cores_panel.isVisible()  # not a CPU sensor
    focus.back_button.click()
    assert window.pages.currentWidget() is window.splitter
    window.close_focus()  # already closed: nothing happens
    assert window.focus_key is None


def test_double_click_opens_the_focus_view_from_every_view(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = _open_window(qtbot, settings)
    gauge = window.gauges.gauges[1]
    qtbot.mouseDClick(gauge, Qt.MouseButton.LeftButton)
    assert window.focus_key == "gpu"
    window.close_focus()

    heat_map = window.sections[CPU].view.heat_map
    tile = next(t for t in heat_map._layout_tiles() if t.name == "P1")
    qtbot.mouseDClick(heat_map, Qt.MouseButton.LeftButton, pos=tile.shape.boundingRect().center().toPoint())
    assert window.focus_key == "core/P-core 1"
    window.close_focus()

    tiles = window.sections[CPU].view.tiles
    qtbot.mouseDClick(tiles, Qt.MouseButton.LeftButton, pos=tiles.place(tiles.width())[0].center().toPoint())
    assert window.focus_key == "pkgw"
    window.close_focus()

    fans = window.sections["Fans"].view
    qtbot.mouseDClick(fans, Qt.MouseButton.LeftButton, pos=fans.place(fans.width())[1].center().toPoint())
    assert window.focus_key == "fan7"
    window.close_focus()
    window.open_focus("not-a-sensor")
    assert window.focus_key is None and window.pages.currentWidget() is window.splitter


def test_focus_view_shares_the_chart_window_with_the_detail_panel(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = _open_window(qtbot, settings)
    window.open_focus("pkg")
    focus = window.focus
    assert focus.window_seconds == window.detail.window_seconds == 300.0
    focus.window_buttons.buttons()[2].click()  # 15 min
    assert window.detail.window_seconds == 900.0 and focus.window_seconds == 900.0
    assert settings.value("detail_window", type=float) == 900.0
    assert focus.chart.window_seconds == 900.0 and focus.cores.window_seconds == 900.0
    assert focus.chart_title.text() == "LAST 15 MINUTES" and focus.cores_note.text() == "same 15 minutes, same scale"
    window.detail.set_window(60.0)
    assert focus.window_seconds == 60.0 and focus.window_buttons.buttons()[0].isChecked()
    assert focus.chart_title.text() == "LAST 1 MINUTE"


def test_focus_view_renames_pins_and_steps_to_another_core(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    window = _open_window(qtbot, settings)
    made: list[FakeTray] = []
    window.attach_tray(lambda: made.append(FakeTray()) or made[-1])  # type: ignore[arg-type,func-returns-value]
    window.open_focus("pkg")
    focus = window.focus
    monkeypatch.setattr(window, "_ask_name", lambda current: "Package")
    focus.rename_button.click()
    assert focus.title.text() == "Package" and window.names == {"pkg": "Package"}
    assert focus.pin_button.isEnabled() and not focus.pin_button.isChecked()
    focus.pin_button.click()
    assert "pkg" in window.trays and focus.pin_button.text() == "Pinned to tray"
    window.set_pinned("pkg", False)
    window.refresh()
    assert not focus.pin_button.isChecked() and focus.pin_button.text() == "Pin to tray"

    strip = focus.cores
    index = [i.key for i in strip.items].index("core/E-core 1")
    qtbot.mouseClick(strip, Qt.MouseButton.LeftButton, pos=strip.place(strip.width())[index].center().toPoint())
    assert window.focus_key == "core/E-core 1" and focus.title.text() == "E-core 1"
    assert window.selected_key == "core/E-core 1" and strip.selected_key == "core/E-core 1"
    assert window.pages.currentWidget() is focus


def test_focus_view_closes_for_a_search_or_a_sensor_that_goes_away(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    gone = [r for r in full_machine() if r.key != "hot"]
    window = _open_window(qtbot, settings, [full_machine()] * 3 + [gone])  # the window reads once on its own
    window.open_focus("pkg")
    window.filter.setText("gpu")
    assert window.focus_key is None and window.pages.currentWidget() is window.splitter
    window.filter.clear()
    window.open_focus("hot")
    assert window.focus_key == "hot" and window.pages.currentWidget() is window.focus
    window.refresh()  # the hotspot stops reporting
    assert window.focus_key is None and window.pages.currentWidget() is window.splitter


def test_focus_view_fits_the_narrowest_window(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QBoxLayout

    window = _open_window(qtbot, settings)
    window.open_focus("pkg")
    narrowest = window.minimumSizeHint().width()
    assert narrowest <= 720  # the focus view and its button don't widen the window
    window.resize(narrowest, 900)
    qtbot.waitUntil(lambda: window.width() == narrowest)
    QApplication.processEvents()
    focus = window.focus
    assert focus.top.direction() == QBoxLayout.Direction.TopToBottom  # the dial over the chart
    for widget in (focus.back_button, focus.rename_button, focus.pin_button, *focus.window_buttons.buttons()):
        assert widget.isVisible() and widget.width() >= widget.minimumSizeHint().width()
    window.resize(1300, 900)
    qtbot.waitUntil(lambda: window.width() == 1300)
    assert focus.top.direction() == QBoxLayout.Direction.LeftToRight


def test_headroom_and_notes_know_which_side_of_a_limit_a_sensor_is() -> None:
    from corewatch.gui.focus import dial_full_scale, focus_stats, headroom
    from corewatch.model import Reading, Row, Stats

    def r(kind: Kind, value: float | None, **extra: float | None) -> Reading:
        return Reading("k", CPU, "x", kind, value, **extra)

    assert headroom(r(Kind.FAN, 0.0, low=300.0), False) == ("300 RPM", "below the low limit")  # a stopped fan
    rail = {"low": 11.4, "high": 12.6}
    assert headroom(r(Kind.VOLTAGE, 11.2, **rail), False) == ("0.200 V", "below the low limit")
    assert headroom(r(Kind.VOLTAGE, 11.6, **rail), False) == ("0.200 V", "above the low limit")  # the nearer one
    assert headroom(r(Kind.VOLTAGE, 12.4, **rail), False) == ("0.200 V", "to the high limit")
    assert dial_full_scale(r(Kind.TEMPERATURE, 1.0, crit=0.0, high=95.0)) == 100.0  # a critical of 0 still counts

    def past(reading: Reading, seconds: float) -> tuple[str, str]:
        stats = Stats()
        stats.seconds_warning = seconds
        stat = focus_stats(Row(reading, stats), 0.0, False, lambda t: t)[6]
        return stat.value, stat.note

    assert past(r(Kind.TEMPERATURE, 50.0, crit=84.8), 30.0) == ("30 s", "at critical")  # an NVMe drive
    assert past(r(Kind.VOLTAGE, 12.0, **rail), 0.0) == ("0 s", "never outside its limits")
    assert past(r(Kind.VOLTAGE, 12.0, **rail), 5.0) == ("5 s", "outside its limits")
    assert past(r(Kind.FAN, 900.0, low=300.0), 0.0) == ("0 s", "never below 300 RPM")
    assert past(r(Kind.FAN, 900.0, low=300.0), 5.0) == ("5 s", "below the low limit")


def test_esc_goes_back_wherever_the_keyboard_is(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = _open_window(qtbot, settings)
    window.activateWindow()
    qtbot.waitActive(window)
    assert not window.close_focus_shortcut.isEnabled()  # Esc is left alone until the focus view opens
    window.open_focus("pkg")
    assert window.close_focus_shortcut.isEnabled()
    window.fahrenheit_button.setFocus()  # the keyboard has left the focus view
    qtbot.keyClick(window.fahrenheit_button, Qt.Key.Key_Escape)
    assert window.focus_key is None and not window.close_focus_shortcut.isEnabled()


def test_hiding_the_focused_sensor_goes_back(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    idle = Reading("idle", CPU, "Unused input", Kind.TEMPERATURE, 0.0, unused=True)
    window = _open_window(qtbot, settings, [[*machine(), idle]])
    window.set_show_unused(True)
    window.open_focus("idle")
    assert window.focus_key == "idle"
    window.set_show_unused(False)
    assert window.focus_key is None and window.pages.currentWidget() is window.splitter


def test_a_cut_title_shows_its_whole_name_until_the_text_changes(qtbot) -> None:  # type: ignore[no-untyped-def]
    from corewatch.gui.widgets import ElidedLabel

    label = ElidedLabel()
    qtbot.addWidget(label)
    label.resize(100, 30)
    label.show()
    label.setText("Motherboard · ROG STRIX Z790-A GAMING WIFI")
    assert label.toolTip() == "Motherboard · ROG STRIX Z790-A GAMING WIFI"
    label.setText("Short")
    assert label.toolTip() == ""


def test_a_card_header_click_hides_nothing(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = _open_window(qtbot, settings)
    fans, gpu = window.sections["Fans"], window.sections[GPU]
    for section in (fans, gpu):
        qtbot.mouseClick(section, Qt.MouseButton.LeftButton, pos=section.header.geometry().center())
    assert fans.view.isVisible() and gpu.grid.isVisible()  # no folding: "All sensors" is the one toggle
    assert "Expand all" not in [a.text() for a in window.settings_button.menu().actions()]
