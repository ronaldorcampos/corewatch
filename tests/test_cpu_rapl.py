from pathlib import Path

from conftest import write_tree

from corewatch.sources.base import cpu_model_name
from corewatch.sources.cpu import CpuSource, Ticks, load_percent, parse_proc_stat
from corewatch.sources.rapl import RaplSource

STAT_1 = """cpu  100 0 100 800 0 0 0 0 0 0
cpu0 50 0 50 400 0 0 0 0 0 0
cpu1 50 0 50 400 0 0 0 0 0 0
intr 12345
"""
# cpu0 does 90 busy / 100 total, cpu1 10 / 100 -> overall 100 / 200
STAT_2 = """cpu  200 0 100 900 0 0 0 0 0 0
cpu0 140 0 50 410 0 0 0 0 0 0
cpu1 60 0 50 490 0 0 0 0 0 0
"""


def test_parse_proc_stat_counts_iowait_as_idle_and_skips_guest() -> None:
    ticks = parse_proc_stat("cpu0 10 1 5 70 4 2 3 1 99 99\n")
    assert ticks["cpu0"] == Ticks(busy=22, total=96)


def test_load_percent_edge_cases() -> None:
    assert load_percent(None, Ticks(1, 2)) is None
    assert load_percent(Ticks(5, 10), Ticks(5, 10)) is None  # no time passed
    assert load_percent(Ticks(0, 0), Ticks(25, 100)) == 25.0


def test_cpu_model_name_cleanup(proc_root: Path, tmp_path: Path) -> None:
    assert cpu_model_name(proc_root) == "Intel Core i7-13700K"
    amd = tmp_path / "amd"
    write_tree(amd, {"cpuinfo": "model name\t: AMD Ryzen 9 7950X 16-Core Processor\n"})
    assert cpu_model_name(amd) == "AMD Ryzen 9 7950X"
    assert cpu_model_name(tmp_path / "missing") is None


def test_cpu_source_groups_threads_into_cores(tmp_path: Path, proc_root: Path) -> None:
    sys_cpu = tmp_path / "cpu"
    write_tree(
        sys_cpu,
        {
            "cpu0/topology/physical_package_id": "0",
            "cpu0/topology/core_id": "0",
            "cpu0/cpufreq/scaling_cur_freq": "4800000",
            "cpu1/topology/physical_package_id": "0",
            "cpu1/topology/core_id": "0",
            "cpu1/cpufreq/scaling_cur_freq": "5300000",
            "cpu2/online": "0",  # offline: no topology, must be ignored
            "online": "0-1",
        },
    )
    write_tree(proc_root, {"stat": STAT_1})
    source = CpuSource(sys_cpu=sys_cpu, proc_root=proc_root)
    write_tree(proc_root, {"stat": STAT_2})
    readings = {r.label: r.value for r in source.sample()}
    assert readings == {
        "CPU load (all cores)": 50.0,
        "Core 0 load": 50.0,  # average of its two threads (90 % and 10 %)
        "Core 0 clock": 5300.0,  # fastest thread
    }


def test_cpu_source_multi_package_labels(tmp_path: Path, proc_root: Path) -> None:
    sys_cpu = tmp_path / "cpu"
    write_tree(
        sys_cpu,
        {
            "cpu0/topology/physical_package_id": "0",
            "cpu0/topology/core_id": "0",
            "cpu1/topology/physical_package_id": "1",
            "cpu1/topology/core_id": "0",
        },
    )
    write_tree(proc_root, {"stat": STAT_1})
    labels = [r.label for r in CpuSource(sys_cpu=sys_cpu, proc_root=proc_root).sample()]
    assert labels == ["CPU load (all cores)", "CPU 0 core 0 load", "CPU 1 core 0 load"]


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_rapl_power_from_energy_delta_with_wraparound(tmp_path: Path, proc_root: Path) -> None:
    root = tmp_path / "powercap"
    write_tree(
        root,
        {
            "intel-rapl:0/name": "package-0",
            "intel-rapl:0/energy_uj": "999000000",
            "intel-rapl:0/max_energy_range_uj": "1000000000",
            "intel-rapl:0:0/name": "core",
            "intel-rapl:0:0/energy_uj": "0",
            "intel-rapl-mmio:0/name": "package-0",  # not a zone we read
        },
    )
    clock = FakeClock()
    source = RaplSource(root=root, proc_root=proc_root, clock=clock)
    clock.now += 2.0
    write_tree(
        root,
        {
            "intel-rapl:0/energy_uj": "39000000",  # wrapped: +40 J
            "intel-rapl:0:0/energy_uj": "20000000",  # +20 J
        },
    )
    readings = {r.label: r.value for r in source.sample()}
    assert readings == {"Package power": 20.0, "Cores power": 10.0}
    assert source.notes() == []


def test_rapl_permission_denied_becomes_a_note(tmp_path: Path, proc_root: Path) -> None:
    root = tmp_path / "powercap"
    write_tree(root, {"intel-rapl:0/name": "package-0", "intel-rapl:0/energy_uj": "1"})
    (root / "intel-rapl:0" / "energy_uj").chmod(0)
    source = RaplSource(root=root, proc_root=proc_root)
    assert source.sample() == []
    [note] = source.notes()
    assert "udev rule" in note


def test_cpu_source_uses_hybrid_core_names(tmp_path: Path, proc_root: Path) -> None:
    from test_hwmon import hybrid_topology

    sys_cpu = hybrid_topology(tmp_path)
    write_tree(proc_root, {"stat": STAT_1})
    labels = [r.label for r in CpuSource(sys_cpu=sys_cpu, proc_root=proc_root).sample()]
    assert labels == ["CPU load (all cores)", "P-core 0 load", "P-core 1 load", "E-core 0 load", "E-core 1 load"]


def test_parse_cpu_list() -> None:
    from corewatch.sources.base import parse_cpu_list

    assert parse_cpu_list("0-3,8,10-11\n") == {0, 1, 2, 3, 8, 10, 11}
    assert parse_cpu_list(None) == set()
    assert parse_cpu_list("junk,2") == {2}


def test_rapl_counter_reset_spike_is_dropped(tmp_path: Path, proc_root: Path) -> None:
    root = tmp_path / "powercap"
    write_tree(
        root,
        {
            "intel-rapl:0/name": "package-0",
            "intel-rapl:0/energy_uj": "900000000",
            "intel-rapl:0/max_energy_range_uj": "262143328850",
        },
    )
    clock = FakeClock()
    source = RaplSource(root=root, proc_root=proc_root, clock=clock)
    clock.now += 1.0
    write_tree(root, {"intel-rapl:0/energy_uj": "5000000"})  # counter reset across suspend
    [reading] = source.sample()
    assert reading.value is None
