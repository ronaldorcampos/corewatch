from dataclasses import replace

import pytest

from corewatch.model import (
    Kind,
    Reading,
    Stats,
    Status,
    format_delta,
    format_duration,
    format_limit,
    format_trend,
    format_value,
)


def reading(value: float | None, **limits: float | None) -> Reading:
    return Reading("k", "dev", "label", Kind.TEMPERATURE, value, **limits)


@pytest.mark.parametrize(
    ("value", "limits", "expected"),
    [
        (50.0, {"high": 80.0, "crit": 100.0}, Status.OK),
        (80.0, {"high": 80.0, "crit": 100.0}, Status.WARNING),
        (100.0, {"high": 80.0, "crit": 100.0}, Status.CRITICAL),
        (None, {"high": 80.0}, Status.OK),
        (0.9, {"low": 1.0, "high": 1.5}, Status.WARNING),
        (1.2, {"low": 1.0, "high": 1.5}, Status.OK),
    ],
)
def test_status(value: float | None, limits: dict[str, float], expected: Status) -> None:
    assert reading(value, **limits).status is expected


def test_stats_ignore_missing_values_and_average() -> None:
    stats = Stats()
    for value in (50.0, None, 40.0, 60.0):
        stats.add(value)
    assert (stats.minimum, stats.maximum, stats.average, stats.count) == (40.0, 60.0, 50.0, 3)


def test_empty_stats_have_no_average() -> None:
    assert Stats().average is None


@pytest.mark.parametrize(
    ("kind", "value", "fahrenheit", "expected"),
    [
        (Kind.TEMPERATURE, 45.04, False, "45.0 °C"),
        (Kind.TEMPERATURE, 100.0, True, "212.0 °F"),
        (Kind.VOLTAGE, 1.2125, True, "1.212 V"),  # °F never touches non-temperatures
        (Kind.FAN, 1184.0, False, "1,184 RPM"),
        (Kind.CLOCK, 4900.0, False, "4,900 MHz"),
        (Kind.POWER, 29.66, False, "29.7 W"),
        (Kind.LOAD, 5.4, False, "5 %"),
        (Kind.FAN, None, False, "—"),
    ],
)
def test_format_value(kind: Kind, value: float | None, fahrenheit: bool, expected: str) -> None:
    assert format_value(kind, value, fahrenheit) == expected


def test_format_limit() -> None:
    assert format_limit(reading(50.0, high=80.0, crit=100.0)) == "high 80.0 °C · crit 100.0 °C"
    assert format_limit(Reading("k", "d", "l", Kind.VOLTAGE, 1.0, low=1.0, high=1.5)) == "min 1.000 V · max 1.500 V"
    assert format_limit(reading(50.0)) == ""


def test_stats_timestamps_spread_and_history() -> None:
    stats = Stats()
    for now, value in ((100.0, 50.0), (101.0, 40.0), (102.0, 60.0), (103.0, 50.0)):
        stats.add(value, now)
    assert (stats.minimum_at, stats.maximum_at, stats.started_at) == (101.0, 102.0, 100.0)
    assert stats.stddev == pytest.approx(7.0710678)  # population std dev of 50, 40, 60, 50
    assert stats.window_average(1.5, now=103.0) == 55.0  # only t=102 and t=103
    assert len(stats.history) == 4


def test_stats_history_is_trimmed_to_fifteen_minutes() -> None:
    stats = Stats()
    stats.add(1.0, 0.0)
    stats.add(2.0, 15 * 60 + 1)
    assert list(stats.history) == [(901.0, 2.0)]
    assert stats.minimum == 1.0  # session extremes survive trimming


def test_time_above_limits_is_charged_to_the_previous_state_and_ignores_gaps() -> None:
    stats = Stats()
    stats.add(85.0, 0.0, Status.WARNING)
    stats.add(101.0, 2.0, Status.CRITICAL)  # the 2 s before this sample were "warning"
    stats.add(70.0, 5.0, Status.OK)  # the 3 s before were "critical"
    stats.add(85.0, 6.0, Status.WARNING)
    stats.add(85.0, 6.0 + 600, Status.WARNING)  # suspend-sized gap: not counted
    assert (stats.seconds_warning, stats.seconds_critical) == (2.0, 3.0)


def test_missing_value_resets_the_state_clock() -> None:
    stats = Stats()
    stats.add(90.0, 0.0, Status.WARNING)
    stats.add(None, 1.0, Status.WARNING)
    stats.add(90.0, 2.0, Status.WARNING)
    assert stats.seconds_warning == 1.0  # only the interval before the gap


def test_trend_per_minute() -> None:
    rising = Stats()
    for second in range(0, 31, 5):  # 30 s of data, past the 20 s minimum
        rising.add(40.0 + second * 0.1, float(second))  # +0.1 per second
    assert rising.trend_per_minute(now=30.0) == pytest.approx(6.0)
    too_short = Stats()
    for second in range(0, 16, 5):  # only 15 s of data
        too_short.add(float(second), float(second))
    assert too_short.trend_per_minute(now=15.0) is None


@pytest.mark.parametrize(
    ("kind", "per_minute", "fahrenheit", "expected"),
    [
        (Kind.TEMPERATURE, 2.06, False, "↑ +2.1 °C/min"),
        (Kind.TEMPERATURE, 10.0, True, "↑ +18.0 °F/min"),  # no +32 on a difference
        (Kind.FAN, -40.0, False, "↓ -40 RPM/min"),
        (Kind.TEMPERATURE, 0.1, False, "→ steady"),
        (Kind.LOAD, None, False, "—"),
    ],
)
def test_format_trend(kind: Kind, per_minute: float | None, fahrenheit: bool, expected: str) -> None:
    assert format_trend(kind, per_minute, fahrenheit) == expected


def test_format_delta_and_duration() -> None:
    assert format_delta(Kind.TEMPERATURE, 5.0, fahrenheit=True, signed=False) == "9.0 °F"
    assert format_delta(Kind.VOLTAGE, None) == "—"
    assert [format_duration(s) for s in (0, 44.6, 200, 7500)] == ["0 s", "45 s", "3 min 20 s", "2 h 05 min"]


def test_clear_resets_in_place_and_window_walks_back_from_newest() -> None:
    stats = Stats()
    for t in range(10):
        stats.add(float(t), float(t), Status.WARNING)
    held = stats  # a Row elsewhere holds this same object
    assert stats.window(3.0, now=9.0) == [(6.0, 6.0), (7.0, 7.0), (8.0, 8.0), (9.0, 9.0)]
    stats.clear()
    assert held.count == 0 and held.minimum is None and not held.history and held.seconds_warning == 0


def test_cap_is_shown_but_never_a_warning() -> None:
    capped = Reading("k", "d", "Power draw", Kind.POWER, 120.0, cap=100.0)
    assert capped.status is Status.OK
    assert format_limit(capped) == "limit 100.0 W"
    assert format_limit(replace(capped, kind=Kind.CLOCK, cap=3255.0)) == "max 3,255 MHz"  # a clock's top speed
