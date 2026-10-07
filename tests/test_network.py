from pathlib import Path

from conftest import write_tree

from corewatch.model import Kind, format_rate, format_value
from corewatch.sources.network import NetworkSource


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def interface(root: Path, name: str, rx: int, tx: int, carrier: str = "1", physical: bool = True) -> None:
    write_tree(
        root,
        {f"{name}/carrier": carrier, f"{name}/statistics/rx_bytes": str(rx), f"{name}/statistics/tx_bytes": str(tx)},
    )
    if physical:
        (root / "devices" / name).mkdir(parents=True, exist_ok=True)
        if not (root / name / "device").exists():
            (root / name / "device").symlink_to(root / "devices" / name)


def test_rates_for_connected_physical_interfaces_only(tmp_path: Path) -> None:
    root = tmp_path / "net"
    interface(root, "enp7s0", 1_000_000, 50_000)
    interface(root, "eno2", 0, 0, carrier="0")  # unplugged
    interface(root, "docker0", 0, 0, physical=False)
    clock = Clock()
    source = NetworkSource(root=root, clock=clock)
    clock.now += 2.0
    interface(root, "enp7s0", 3_000_000, 70_000)
    readings = {r.key: r for r in source.sample()}
    assert set(readings) == {"net/enp7s0/down", "net/enp7s0/up"}
    down, up = readings["net/enp7s0/down"], readings["net/enp7s0/up"]
    assert (down.value, up.value) == (1_000_000.0, 10_000.0)
    assert (down.device, down.label, down.kind) == ("Network · enp7s0", "Download", Kind.THROUGHPUT)


def test_counter_reset_and_new_interfaces_give_no_rate_yet(tmp_path: Path) -> None:
    root = tmp_path / "net"
    interface(root, "enp7s0", 5_000_000, 5_000_000)
    clock = Clock()
    source = NetworkSource(root=root, clock=clock)
    clock.now += 1.0
    interface(root, "enp7s0", 100, 100)  # driver reset its counters
    interface(root, "wlo1", 10, 10)  # just connected
    values = {r.key: r.value for r in source.sample()}
    assert values == {"net/enp7s0/down": None, "net/enp7s0/up": None, "net/wlo1/down": None, "net/wlo1/up": None}
    clock.now += 1.0
    interface(root, "enp7s0", 1100, 100)
    assert {r.key: r.value for r in source.sample()}["net/enp7s0/down"] == 1000.0


def test_missing_root_yields_nothing(tmp_path: Path) -> None:
    assert NetworkSource(root=tmp_path / "absent").sample() == []


def test_rate_formatting() -> None:
    assert [format_rate(v) for v in (0, 999, 1_500, 953_900, 12_345_678, 2e9)] == [
        "0 B/s",
        "999 B/s",
        "1.5 KB/s",
        "953.9 KB/s",
        "12.3 MB/s",
        "2.0 GB/s",
    ]
    assert format_value(Kind.THROUGHPUT, 1_500.0) == "1.5 KB/s"
    assert format_rate(-2_000, signed=True) == "-2.0 KB/s"


def test_units_are_chosen_after_rounding() -> None:
    from corewatch.model import format_short, format_trend

    assert format_rate(999_960) == "1.0 MB/s" and format_rate(999.6) == "1.0 KB/s"
    assert format_short(Kind.THROUGHPUT, 999_600) == "1.0M" and format_short(Kind.THROUGHPUT, 9_960) == "10K"
    assert format_short(Kind.FAN, 999.6) == "1.0k"
    assert format_trend(Kind.THROUGHPUT, 12_345.0) == "↑ +12.3 KB/s per min"
