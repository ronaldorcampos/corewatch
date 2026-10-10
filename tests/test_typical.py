from corewatch.model import Kind, Reading, Status, format_typical
from corewatch.typical import with_typical

GPU = "GPU · NVIDIA GeForce RTX 4070"
DRIVE = "NVMe nvme0 · Samsung SSD 980 PRO 2TB"
BOARD = "Motherboard · ROG STRIX Z790-A GAMING WIFI"


def typical(*readings: Reading) -> dict[str, tuple[float | None, float | None]]:
    return {r.key: (r.typical_low, r.typical_high) for r in with_typical(readings)}


def test_loads_and_fan_controls_top_out_at_100() -> None:
    got = typical(
        Reading("load", "CPU", "P-core 0 load", Kind.LOAD, 4.0),
        Reading("pwm", BOARD, "Fan control 2", Kind.FAN_DUTY, 58.0),
    )
    assert got == {"load": (None, 100.0), "pwm": (None, 100.0)}


def test_named_rails_get_the_atx_five_percent_and_unnamed_ones_nothing() -> None:
    got = typical(
        Reading("v12", BOARD, "+12V", Kind.VOLTAGE, 12.1),
        Reading("v33", BOARD, "+3.3V standby", Kind.VOLTAGE, 3.4),
        Reading("avcc", BOARD, "AVCC", Kind.VOLTAGE, 3.4),
        Reading("v9", BOARD, "Voltage 9", Kind.VOLTAGE, 1.0),
        Reading("bat", BOARD, "CMOS battery", Kind.VOLTAGE, 3.2),
    )
    low, high = got["v12"]
    assert (round(low or 0, 3), round(high or 0, 3)) == (11.4, 12.6)
    assert got["v33"] == got["avcc"] != (None, None)
    assert got["v9"] == got["bat"] == (None, None)


def test_a_cards_or_drives_other_temperatures_share_its_main_ones_limit() -> None:
    got = typical(
        Reading("core", GPU, "GPU temperature", Kind.TEMPERATURE, 31.0, high=94.0, crit=99.0),
        Reading("hot", GPU, "Hotspot temperature", Kind.TEMPERATURE, 35.0),
        Reading("comp", DRIVE, "Composite", Kind.TEMPERATURE, 35.0, crit=84.8),
        Reading("s1", DRIVE, "Sensor 1", Kind.TEMPERATURE, 40.0),
        Reading("pch", BOARD, "PCH_CHIP_TEMP", Kind.TEMPERATURE, 51.0),
        Reading("sys", BOARD, "SYSTIN", Kind.TEMPERATURE, 30.0, high=80.0),
    )
    assert got["hot"] == (None, 99.0)  # the card's critical: a hotspot runs hotter than the core by design
    assert got["s1"] == (None, 84.8)  # the drive's critical
    assert got["pch"] == (None, None)  # a board's sensors measure unrelated parts
    assert got["core"] == got["comp"] == got["sys"] == (None, None)  # reported limits stay as they are


def test_only_a_main_temperature_named_as_such_lends_its_limit() -> None:
    got = typical(
        Reading("pcie", "GPU · Arc", "PCIe link temperature", Kind.TEMPERATURE, 50.0),
        Reading("mctrl", "GPU · Arc", "Memory controller temperature", Kind.TEMPERATURE, 60.0, high=105.0),
        Reading("core", GPU, "GPU temperature", Kind.TEMPERATURE, 40.0, high=90.0),  # a high only
        Reading("mem", GPU, "Memory temperature", Kind.TEMPERATURE, 45.0),
    )
    assert got["pcie"] == (None, None)  # its card's other sensor is no main one
    assert got["mem"] == (None, 90.0)  # no critical: the high


def test_rails_are_matched_without_case_or_a_trailing_voltage() -> None:
    got = typical(
        Reading("v12", BOARD, "+12V Voltage", Kind.VOLTAGE, 12.0), Reading("v5", BOARD, "+5v", Kind.VOLTAGE, 5.0)
    )
    assert got["v12"] != (None, None) and got["v5"] != (None, None)


def test_a_sensor_with_any_reported_limit_gets_no_typical_one() -> None:
    capped = Reading("clk", GPU, "Graphics clock", Kind.CLOCK, 210.0, cap=3255.0)
    rail = Reading("v12", BOARD, "+12V", Kind.VOLTAGE, 12.0, low=10.8)
    assert typical(capped, rail) == {"clk": (None, None), "v12": (None, None)}


def test_a_typical_limit_never_warns() -> None:
    [full] = with_typical([Reading("load", "CPU", "CPU load", Kind.LOAD, 100.0)])
    [sagging] = with_typical([Reading("v12", BOARD, "+12V", Kind.VOLTAGE, 11.0)])
    assert full.status is sagging.status is Status.OK


def test_typical_limits_say_they_are_typical() -> None:
    [rail] = with_typical([Reading("v12", BOARD, "+12V", Kind.VOLTAGE, 12.0)])
    [load] = with_typical([Reading("load", "CPU", "CPU load", Kind.LOAD, 4.0)])
    hot = Reading("hot", GPU, "Hotspot", Kind.TEMPERATURE, 35.0, typical_high=94.0)
    assert format_typical(rail) == "typical 11.400–12.600 V"
    assert format_typical(load) == "full at 100 %"
    assert format_typical(hot) == "typical up to 94.0 °C"
    assert format_typical(hot, fahrenheit=True) == "typical up to 201.2 °F"
    assert format_typical(Reading("f", BOARD, "Fan 2", Kind.FAN, 900.0)) == ""
