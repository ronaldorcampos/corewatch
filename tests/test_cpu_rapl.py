from pathlib import Path

from conftest import write_tree

from corewatch.model import Kind
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
            "cpu1/cpufreq/cpuinfo_max_freq": "5400000",  # cpu0 has none: the core's is what's known
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
    assert {r.label: r.cap for r in source.sample() if r.kind is Kind.CLOCK} == {"Core 0 clock": 5400.0}
    (sys_cpu / "cpu1/cpufreq/cpuinfo_max_freq").unlink()
    assert {r.label: r.cap for r in source.sample() if r.kind is Kind.CLOCK} == {"Core 0 clock": None}


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


def test_rapl_package_limit_is_the_higher_of_its_sustained_and_boost_limits(tmp_path: Path, proc_root: Path) -> None:
    root = tmp_path / "powercap"
    zone = {
        "intel-rapl:0/name": "package-0",
        "intel-rapl:0/energy_uj": "0",
        "intel-rapl:0/constraint_0_name": "long_term",
        "intel-rapl:0/constraint_0_power_limit_uw": "125000000",
        "intel-rapl:0/constraint_1_name": "short_term",
        "intel-rapl:0/constraint_1_power_limit_uw": "253000000",
        "intel-rapl:0/constraint_2_name": "peak_power",  # a current safeguard, never held
        "intel-rapl:0/constraint_2_power_limit_uw": "380000000",
        "intel-rapl:0:0/name": "core",
        "intel-rapl:0:0/energy_uj": "0",
        "intel-rapl:0:0/constraint_0_name": "long_term",
        "intel-rapl:0:0/constraint_0_power_limit_uw": "90000000",
    }
    write_tree(root, zone)
    source = RaplSource(root=root, proc_root=proc_root, clock=FakeClock())
    assert {r.label: r.cap for r in source.sample()} == {"Package power": 253.0, "Cores power": None}
    write_tree(root, {"intel-rapl:0/constraint_1_power_limit_uw": "4095875000"})  # "no limit"
    assert {r.label: r.cap for r in source.sample()}["Package power"] is None
    write_tree(root, {"intel-rapl:0/constraint_1_power_limit_uw": "65000000"})  # a profile lowered it
    assert {r.label: r.cap for r in source.sample()}["Package power"] == 125.0


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


def apple_topology(tmp_path: Path, capacities: tuple[int, ...] = (485, 485, *(1024,) * 8)) -> Path:
    """An M1 Pro's layout: core IDs restart in each cluster (2 E-cores, then two clusters of 4
    P-cores), and the kernel rates the E-cores less capable."""
    sys_cpu = tmp_path / "devices" / "system" / "cpu"
    files = {}
    for cpu, (cluster, core_id) in enumerate(
        [(0, 0), (0, 1), *((1, n) for n in range(4)), *((2, n) for n in range(4))]
    ):
        files[f"cpu{cpu}/topology/physical_package_id"] = "0"
        files[f"cpu{cpu}/topology/core_id"] = str(core_id)
        files[f"cpu{cpu}/topology/cluster_id"] = str(cluster)
        files[f"cpu{cpu}/topology/core_cpus_list"] = str(cpu)  # one thread per core
        files[f"cpu{cpu}/regs/identification/midr_el1"] = "0x00000000612f0240" if cluster == 0 else "0x00000000612f0250"
        if cpu < len(capacities):
            files[f"cpu{cpu}/cpu_capacity"] = str(capacities[cpu])
    write_tree(sys_cpu, files)
    return sys_cpu


def test_apple_cores_sharing_a_core_id_across_clusters_stay_apart(tmp_path: Path, proc_root: Path) -> None:
    from corewatch.sources.base import discover_cores

    cores = discover_cores(apple_topology(tmp_path))
    assert [(c.name, c.threads, c.key) for c in cores] == [
        ("E-core 0", (0,), "0/0/0"),
        ("E-core 1", (1,), "0/0/1"),
        *((f"P-core {n}", (2 + n,), f"0/1/{n}") for n in range(4)),
        *((f"P-core {4 + n}", (6 + n,), f"0/2/{n}") for n in range(4)),
    ]
    write_tree(proc_root, {"stat": STAT_1})
    keys = [r.key for r in CpuSource(sys_cpu=apple_topology(tmp_path), proc_root=proc_root).sample()]
    assert len(keys) == len(set(keys)) == 11  # all-cores load and one per core


def test_core_kinds_from_capacity_need_exactly_two_and_every_cpu(tmp_path: Path) -> None:
    from corewatch.sources.base import discover_cores

    three = apple_topology(tmp_path / "three", (300, 300, *(700,) * 4, *(1024,) * 4))  # phone-like tiers
    assert {c.name.split()[0] for c in discover_cores(three)} == {"Core"}
    partial = apple_topology(tmp_path / "partial", (485, 485, 1024))  # most CPUs report none
    assert {c.name.split()[0] for c in discover_cores(partial)} == {"Core"}
    same = apple_topology(tmp_path / "same", (1024,) * 10)
    assert [c.name for c in discover_cores(same)][:2] == ["Core 0", "Core 1"]


def test_x86_keys_ignore_shared_clusters(tmp_path: Path) -> None:
    """Intel's E-cores share a cluster four at a time with unique core IDs: keys stay as before."""
    from test_hwmon import hybrid_topology

    from corewatch.sources.base import discover_cores

    sys_cpu = hybrid_topology(tmp_path)
    write_tree(
        sys_cpu, {f"cpu{cpu}/topology/cluster_id": str(cluster) for cpu, cluster in enumerate((0, 0, 8, 8, 16, 16))}
    )
    assert [(c.name, c.key, c.cluster) for c in discover_cores(sys_cpu)] == [
        ("P-core 0", "0/0", None),
        ("P-core 1", "0/4", None),
        ("E-core 0", "0/32", None),
        ("E-core 1", "0/33", None),
    ]


def test_apple_chip_name_from_the_device_tree(tmp_path: Path) -> None:
    mac = tmp_path / "mac"
    write_tree(mac, {"cpuinfo": "processor\t: 0\nCPU part\t: 0x024\n"})
    (mac / "device-tree").mkdir()
    (mac / "device-tree" / "model").write_text("Apple MacBook Pro (16-inch, M1 Pro, 2021)\0")
    assert cpu_model_name(mac) == "Apple M1 Pro"
    (mac / "device-tree" / "model").write_text("Apple Mac mini (M2, 2023)\0")
    assert cpu_model_name(mac) == "Apple M2"
    (mac / "device-tree" / "model").write_text("Raspberry Pi 4 Model B Rev 1.4\0")  # a board, not a chip
    assert cpu_model_name(mac) is None


def test_threads_the_kernel_lists_together_are_one_core_whatever_their_clusters(tmp_path: Path) -> None:
    """Two threads of a core reporting different cluster IDs (as under some hypervisors) stay one
    core, keyed as before: only core IDs that repeat across separate cores bring in the cluster."""
    from corewatch.sources.base import discover_cores

    sys_cpu = tmp_path / "cpu"
    files = {}
    for core in range(2):
        for thread in range(2):
            cpu = core * 2 + thread
            files[f"cpu{cpu}/topology/physical_package_id"] = "0"
            files[f"cpu{cpu}/topology/core_id"] = str(core)
            files[f"cpu{cpu}/topology/cluster_id"] = str(cpu)
            files[f"cpu{cpu}/topology/thread_siblings_list"] = f"{core * 2}-{core * 2 + 1}"
    write_tree(sys_cpu, files)
    assert [(c.name, c.threads, c.key) for c in discover_cores(sys_cpu)] == [
        ("Core 0", (0, 1), "0/0"),
        ("Core 1", (2, 3), "0/1"),
    ]


def test_one_core_design_rated_twice_has_no_e_cores(tmp_path: Path) -> None:
    """Two clusters of one design topping out at different clocks get two capacities, but the
    CPUs name one design: no E-cores."""
    from corewatch.sources.base import discover_cores

    sys_cpu = apple_topology(tmp_path)
    write_tree(sys_cpu, {f"cpu{cpu}/regs/identification/midr_el1": "0x00000000612f0250" for cpu in range(10)})
    assert {c.name.split()[0] for c in discover_cores(sys_cpu)} == {"Core"}


def test_a_core_id_shared_with_no_cluster_reported_falls_back_to_the_first_cpu(tmp_path: Path) -> None:
    from corewatch.sources.base import discover_cores

    sys_cpu = apple_topology(tmp_path)
    for cpu in range(10):
        (sys_cpu / f"cpu{cpu}/topology/cluster_id").unlink()
    cores = discover_cores(sys_cpu)
    assert [c.key for c in cores] == [
        f"0/{cpu}/{core_id}" for cpu, core_id in enumerate((0, 1, 0, 1, 2, 3, 0, 1, 2, 3))
    ]
    assert [c.name for c in cores][:4] == ["E-core 0", "E-core 1", "P-core 0", "P-core 1"]  # in hardware order


def test_machine_model_needs_a_name(tmp_path: Path) -> None:
    from corewatch.sources.base import machine_model

    (tmp_path / "device-tree").mkdir()
    model = tmp_path / "device-tree" / "model"
    for empty in ("", "\0", " \0"):
        model.write_text(empty)
        assert machine_model(tmp_path) is None
    model.write_text("Apple Mac Studio (M1 Ultra, 2022)\0")
    assert machine_model(tmp_path) == "Apple Mac Studio (M1 Ultra, 2022)"


def amd_topology(tmp_path: Path, core_ids: list[int], siblings: list[str], cluster: str = "65535") -> Path:
    sys_cpu = tmp_path / "cpu"
    files = {}
    for cpu, (core_id, listed) in enumerate(zip(core_ids, siblings, strict=True)):
        files[f"cpu{cpu}/topology/physical_package_id"] = "0"
        files[f"cpu{cpu}/topology/core_id"] = str(core_id)
        files[f"cpu{cpu}/topology/cluster_id"] = cluster
        files[f"cpu{cpu}/topology/thread_siblings_list"] = listed
    write_tree(sys_cpu, files)
    return sys_cpu


def test_a_bulldozer_compute_units_two_cores_stay_two(tmp_path: Path) -> None:
    """An FX-8350 (kernels up to at least 6.1): each core has its own core ID, but the kernel lists
    a compute unit's two cores as siblings. Siblings never join different core IDs."""
    from corewatch.sources.base import discover_cores

    sys_cpu = amd_topology(tmp_path, list(range(8)), [f"{n & ~1}-{n | 1}" for n in range(8)])
    assert [(c.name, c.threads, c.key) for c in discover_cores(sys_cpu)] == [
        (f"Core {n}", (n,), f"0/{n}") for n in range(8)
    ]


def test_clusters_that_cant_tell_cores_apart_give_way_to_cpu_numbers(tmp_path: Path) -> None:
    """A two-node Opteron 6300 (kernels 5.16 to at least 6.1): core IDs restart in each node, and
    every CPU reports the same cluster. Keys must still be unique."""
    from corewatch.sources.base import discover_cores

    core_ids = [0, 0, 1, 1, 0, 0, 1, 1]  # a compute unit's two cores share an ID here
    siblings = ["0-1", "0-1", "2-3", "2-3", "4-5", "4-5", "6-7", "6-7"]
    cores = discover_cores(amd_topology(tmp_path, core_ids, siblings))
    assert [(c.threads, c.key) for c in cores] == [
        ((0, 1), "0/0/0"),
        ((2, 3), "0/2/1"),
        ((4, 5), "0/4/0"),
        ((6, 7), "0/6/1"),
    ]
    negative = discover_cores(amd_topology(tmp_path / "none", core_ids, siblings, cluster="-1"))
    assert len({c.key for c in negative}) == 4
