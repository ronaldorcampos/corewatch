import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from fakes import FakeSource, load, temp
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

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
    window.reset_button.click()
    assert texts(window, "pkg")[2] == "—"  # statistics start over at the next reading


def test_filter_opens_collapsed_cards_without_forgetting_they_were_collapsed(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    section = window.sections["GPU · RTX"]
    section.chevron.click()
    assert section.collapsed and settings.value("collapsed", type=list) == ["GPU · RTX"]
    window.filter.setText("gpu")
    assert not section.collapsed
    window.filter.clear()
    assert section.collapsed
    assert settings.value("collapsed", type=list) == ["GPU · RTX"]


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
        return [Reading("f", "Motherboard", "Fan 2", Kind.FAN, rpm)]

    window = make_window(qtbot, settings, [fan(900.0), fan(12_345.0), fan(900.0)])
    grid = window.sections["Motherboard"].grid
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


def test_tray_icon_draws_status_colour() -> None:
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

    script = [[temp("t", "CPU · i7", 50.0), Reading("fan1", "Motherboard", "Fan 1", Kind.FAN, 0.0)]]
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
    window.reset_button.click()
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

    def setIcon(self, icon) -> None:  # type: ignore[no-untyped-def]
        self.icons += 1

    def setToolTip(self, text: str) -> None:
        self.tooltips.append(text)

    def show(self) -> None:
        self.visible = True

    def isVisible(self) -> bool:
        return self.visible


def test_tray_updates_only_when_something_changes(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, [SCRIPT[0]])
    tray = FakeTray()
    window.attach_tray(tray)  # type: ignore[arg-type]
    assert tray.icons == 1 and "CPU 50.0 °C · 5% load" in tray.tooltips[-1]
    window.refresh()
    window.refresh()
    assert tray.icons == 1  # same reading, nothing sent
    window.set_fahrenheit(True)
    assert tray.icons == 2


def test_close_to_tray_hides_and_quit_really_closes(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    window = make_window(qtbot, settings, SCRIPT)
    tray = FakeTray()
    window.attach_tray(tray)  # type: ignore[arg-type]
    window.tray_action.setChecked(True)
    window.show()
    monkeypatch.setattr(QApplication, "quit", lambda *a: None)
    window.close()
    assert window.isHidden() and window._thread.isRunning()  # still sampling in the tray
    window.toggle_visible()
    assert window.isVisible()
    window.quit()
    assert window.isHidden() and not window._thread.isRunning()
    assert settings.value("geometry") is not None


def test_close_to_tray_steps_aside_when_the_session_ends(qtbot, settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    window = make_window(qtbot, settings, SCRIPT)
    window.attach_tray(FakeTray())  # type: ignore[arg-type]
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
    assert window.min_max_button.isChecked() and grid.shown_stats() == ("value", "min", "max", "avg")
    with_columns = grid.cell_min_width()
    window.min_max_button.click()
    assert grid.shown_stats() == ("value", "avg")
    assert grid.cell_min_width() < with_columns  # the space goes back to the cell
    parts = grid._parts(grid.cells()[0].rect)
    assert "min" not in parts and "max" not in parts
    assert settings.value("show_min_max", type=bool) is False
    reopened = make_window(qtbot, settings, SCRIPT)
    assert not reopened.min_max_button.isChecked()
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


def test_folding_a_card_while_filtering_lasts_until_the_filter_clears(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    window = make_window(qtbot, settings, SCRIPT)
    section = window.sections["GPU · RTX"]
    window.filter.setText("gpu")
    section.chevron.click()
    window.refresh()
    assert section.collapsed  # doesn't spring open on the next tick
    assert settings.value("collapsed") is None  # and isn't remembered
    window.filter.clear()
    assert not section.collapsed


def test_selection_moves_off_a_sensor_that_becomes_hidden(qtbot, settings) -> None:  # type: ignore[no-untyped-def]
    from corewatch.model import Reading

    script = [
        [temp("t", "CPU · i7", 50.0, label="CPU package"), Reading("fan1", "Motherboard", "Fan 1", Kind.FAN, 0.0)]
    ]
    window = make_window(qtbot, settings, script)
    window.unused_action.setChecked(True)
    window._select("fan1")
    window.unused_action.setChecked(False)
    assert window.selected_key == "t"
    assert window.detail.title.text() == "CPU package"
