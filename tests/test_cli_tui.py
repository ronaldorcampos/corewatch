import io
import json

import pytest
from fakes import FakeSource, load, temp
from textual.widgets import DataTable

from corewatch.cli import build_parser, write_dump
from corewatch.monitor import Monitor
from corewatch.tui import CorewatchApp


def sample_rows():  # type: ignore[no-untyped-def]
    source = FakeSource(
        "s",
        [
            [
                temp("gpu", "GPU · RTX", 41.0, label="GPU temperature"),
                temp("pkg", "CPU · i7", 57.0, label="CPU package", high=80.0, crit=100.0),
                load("cpu-load", "CPU · i7", 5.0, label="CPU load"),
            ]
        ],
    )
    return Monitor([source]).sample()


def test_parser_validates_interval() -> None:
    parser = build_parser()
    assert parser.parse_args(["tui", "-i", "0.5"]).interval == 0.5
    assert parser.parse_args([]).mode == "gui"
    for bad in ("0.1", "61", "fast"):
        with pytest.raises(SystemExit):
            parser.parse_args(["-i", bad])


def test_text_dump_groups_devices_cpu_first() -> None:
    out = io.StringIO()
    write_dump(sample_rows(), ["Load the driver"], out, as_json=False, fahrenheit=False)
    assert out.getvalue().splitlines() == [
        "CPU · i7",
        "  CPU package       57.0 °C  high 80.0 °C · crit 100.0 °C",
        "  CPU load              5 %",
        "",
        "GPU · RTX",
        "  GPU temperature       41.0 °C",
        "",
        "Note: Load the driver",
    ]


def test_json_dump() -> None:
    out = io.StringIO()
    write_dump(sample_rows(), [], out, as_json=True, fahrenheit=True)
    data = json.loads(out.getvalue())
    assert [s["key"] for s in data["sensors"]] == ["pkg", "cpu-load", "gpu"]
    assert data["sensors"][0] | {} == {
        "key": "pkg",
        "device": "CPU · i7",
        "label": "CPU package",
        "kind": "temperature",
        "value": 57.0,  # JSON stays in Celsius regardless of the display unit
        "low": None,
        "high": 80.0,
        "crit": 100.0,
        "status": "ok",
        "min": 57.0,
        "max": 57.0,
        "cap": None,
        "unused": False,
    }
    assert "·" in out.getvalue()  # not escaped to ·


def cell_texts(table: DataTable) -> list[list[str]]:  # type: ignore[type-arg]
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


async def test_tui_shows_grouped_rows_and_toggles_units() -> None:
    source = FakeSource(
        "s",
        [
            [temp("pkg", "CPU · i7", 50.0, label="CPU package")],
            [temp("pkg", "CPU · i7", 70.0, label="CPU package")],
        ],
        notes=["Load the driver"],
    )
    app = CorewatchApp(Monitor([source]), interval=60)
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        rows = cell_texts(table)
        assert rows[0][0] == "CPU · i7"
        assert rows[1][:5] == ["  CPU package", "50.0 °C", "50.0 °C", "50.0 °C", "50.0 °C"]
        assert "updating every 60 s" in app.sub_title

        await pilot.press("f")  # same reading shown in °F: no extra sample
        assert source.calls == 1
        rows = cell_texts(table)
        assert rows[1][1:5] == ["122.0 °F", "122.0 °F", "122.0 °F", "122.0 °F"]
        app.refresh_readings()
        assert cell_texts(table)[1][1:5] == ["158.0 °F", "122.0 °F", "158.0 °F", "140.0 °F"]

        await pilot.press("r")
        assert cell_texts(table)[1][2] == "—"  # statistics start over at the next reading

        await pilot.press("minus")
        assert app.interval == 60  # already the slowest step
        await pilot.press("plus")
        assert app.interval == 30


async def test_tui_rebuilds_when_sensors_appear() -> None:
    source = FakeSource(
        "s",
        [[temp("a", "CPU", 50.0)], [temp("a", "CPU", 50.0), temp("b", "GPU", 40.0)]],
    )
    app = CorewatchApp(Monitor([source]), interval=60)
    async with app.run_test():
        assert app.query_one(DataTable).row_count == 2
        app.refresh_readings()
        assert [r[0] for r in cell_texts(app.query_one(DataTable))] == ["CPU", "  a", "GPU", "  b"]


async def test_tui_unused_toggle() -> None:
    from corewatch.model import Kind, Reading

    source = FakeSource(
        "s",
        [[temp("a", "CPU", 50.0), Reading("fan1", "Motherboard", "Fan 1", Kind.FAN, 0.0, empty_if_idle=True)]],
    )
    app = CorewatchApp(Monitor([source]), interval=60)
    async with app.run_test() as pilot:
        assert [r[0] for r in cell_texts(app.query_one(DataTable))] == ["CPU", "  a"]
        await pilot.press("u")
        assert [r[0] for r in cell_texts(app.query_one(DataTable))] == ["CPU", "  a", "Fans", "  Fan 1"]


async def test_tui_reset_takes_no_reading_and_cursor_follows_the_sensor() -> None:
    source = FakeSource(
        "s",
        [
            [temp("b", "CPU", 50.0, label="[b]bold?[/b]")],
            [temp("a", "CPU", 40.0), temp("b", "CPU", 50.0, label="[b]bold?[/b]")],
        ],
    )
    app = CorewatchApp(Monitor([source]), interval=60)
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        assert cell_texts(table)[1][0] == "  [b]bold?[/b]"  # shown literally, not as markup
        table.move_cursor(row=1)
        app.refresh_readings()  # "a" appears above "b"
        assert table.cursor_row == 2
        calls = source.calls
        await pilot.press("r")
        assert source.calls == calls
        assert cell_texts(table)[2][2] == "—"


def test_json_marks_unused_rows_and_keeps_the_cap() -> None:
    from corewatch.model import Kind, Reading

    source = FakeSource(
        "s",
        [
            [
                Reading("fan1", "Motherboard", "Fan 1", Kind.FAN, 0.0, empty_if_idle=True),
                Reading("p", "GPU", "Power", Kind.POWER, 90.0, cap=100.0),
            ]
        ],
    )
    monitor = Monitor([source])
    rows = monitor.sample()
    out = io.StringIO()
    write_dump(rows, [], out, as_json=True, fahrenheit=False, unused_keys=monitor.unused_rows(rows))
    sensors = {s["key"]: s for s in json.loads(out.getvalue())["sensors"]}
    assert sensors["fan1"]["unused"] is True and sensors["p"]["unused"] is False
    assert sensors["p"]["cap"] == 100.0


async def test_tui_gathers_fans_into_one_group() -> None:
    from corewatch.model import Kind, Reading

    source = FakeSource(
        "s",
        [[temp("t", "CPU", 50.0), Reading("g", "GPU · RTX", "Fan 1", Kind.FAN, 2320.0)]],
    )
    app = CorewatchApp(Monitor([source]), interval=60)
    async with app.run_test():
        assert [r[0] for r in cell_texts(app.query_one(DataTable))] == ["CPU", "  t", "Fans", "  GPU fan 1"]


def test_minimized_flag() -> None:
    assert build_parser().parse_args(["--minimized"]).minimized is True
    assert build_parser().parse_args([]).minimized is False
