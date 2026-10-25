from pathlib import Path

from conftest import write_tree

from corewatch.model import Kind
from corewatch.sources.programs import KERNEL, ProgramSource, discharge_watts, parse_pid_stat, program_name


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def pid_stat(name: str, ticks: tuple[int, int], started: int = 500, flags: int = 0) -> str:
    """A /proc/<pid>/stat line: the flags are field 9, utime and stime fields 14 and 15, the
    start time field 22."""
    after = ["S", "1", "1", "1", "0", "-1", str(flags), "0", "0", "0", "0", str(ticks[0]), str(ticks[1])]
    after += ["0", "0", "20", "0", "1", "0", str(started), "1000", "10"]
    return f"1 ({name}) " + " ".join(after)


def process(
    proc: Path, pid: int, cmdline: str, ticks: tuple[int, int], comm: str = "x", started: int = 500, flags: int = 0
) -> None:
    write_tree(proc, {f"{pid}/stat": pid_stat(comm, ticks, started, flags), f"{pid}/comm": comm})
    (proc / str(pid) / "cmdline").write_bytes(cmdline.encode())


def cpu_stat(proc: Path, busy: int, idle: int) -> None:
    write_tree(proc, {"stat": f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\n"})


def battery(root: Path, status: str = "Discharging", power: str = "-20000000") -> Path:
    supplies = root / "power_supply"
    write_tree(supplies, {"BAT0/type": "Battery", "BAT0/status": status, "BAT0/power_now": power})
    return supplies


def test_parse_pid_stat_counts_from_the_last_bracket() -> None:
    assert parse_pid_stat(pid_stat("Web Content (x)", (7, 3))) == 10
    assert parse_pid_stat("1 (truncated) S 1") is None


def test_program_names_group_a_programs_processes(tmp_path: Path) -> None:
    app = tmp_path / "opt" / "Some App" / "app"
    write_tree(tmp_path / "opt", {"Some App/app": ""})
    process(tmp_path, 10, "/usr/lib64/firefox/firefox\0-contentproc\0", (0, 0))
    process(tmp_path, 11, "sshd: me@pts/0", (0, 0))  # rewritten into one string
    process(tmp_path, 2, "", (0, 0), comm="kthreadd", flags=0x00208040)  # PF_KTHREAD set
    process(tmp_path, 12, str(app), (0, 0))  # one argument with spaces, and it's there
    process(tmp_path, 13, "Z:\\home\\me\\Steam\\common\\Elden Ring\\eldenring.exe\0", (0, 0))
    process(tmp_path, 14, "C:\\Program Files\\Game\\game.exe", (0, 0))
    process(tmp_path, 15, "", (0, 0), comm="make")  # exiting: its command line is gone
    names = {
        pid: program_name(tmp_path / str(pid), (tmp_path / str(pid) / "stat").read_text())
        for pid in (10, 11, 2, 12, 13, 14, 15)
    }
    assert names == {
        10: "firefox",
        11: "sshd",
        2: KERNEL,
        12: "app",
        13: "eldenring.exe",
        14: "game.exe",
        15: "make",
    }
    assert program_name(tmp_path / "99", pid_stat("gone", (0, 0))) is None  # gone before its command line


def test_top_programs_share_the_batterys_draw_by_cpu_time(tmp_path: Path) -> None:
    proc, clock = tmp_path / "proc", Clock()
    supplies = battery(tmp_path)
    process(proc, 10, "/usr/bin/firefox", (100, 0))
    process(proc, 11, "/usr/bin/firefox", (50, 0))  # a second process of the same program
    process(proc, 20, "/usr/bin/code", (10, 0))
    cpu_stat(proc, 1000, 9000)
    source = ProgramSource(proc_root=proc, power_supply=supplies, clock=clock)
    assert source.sample() == []  # nothing to compare with yet
    clock.now += 1
    process(proc, 10, "/usr/bin/firefox", (130, 0))  # +30
    process(proc, 11, "/usr/bin/firefox", (60, 0))  # +10
    process(proc, 20, "/usr/bin/code", (30, 0))  # +20
    process(proc, 30, "/usr/bin/new", (5, 5), started=900)  # started since: all of its 10 count
    cpu_stat(proc, 1080, 9720)  # 80 busy of 800
    readings = {r.key: (r.device, r.label, r.kind, r.value) for r in source.sample()}
    assert readings == {
        "programs/firefox/cpu": ("Battery · programs", "firefox CPU", Kind.LOAD, 5.0),  # 40 of 800
        "programs/firefox/power": ("Battery · programs", "firefox power (est.)", Kind.POWER, 10.0),  # 40/80 of 20 W
        "programs/code/cpu": ("Battery · programs", "code CPU", Kind.LOAD, 2.5),
        "programs/code/power": ("Battery · programs", "code power (est.)", Kind.POWER, 5.0),
        "programs/new/cpu": ("Battery · programs", "new CPU", Kind.LOAD, 1.25),
        "programs/new/power": ("Battery · programs", "new power (est.)", Kind.POWER, 2.5),
    }
    assert [key for key in readings][:2] == ["programs/firefox/cpu", "programs/firefox/power"]  # busiest first


def test_only_the_top_ten_over_the_window(tmp_path: Path) -> None:
    proc, clock = tmp_path / "proc", Clock()
    supplies = battery(tmp_path)
    for n in range(12):
        process(proc, 100 + n, f"/usr/bin/p{n:02}", (0, 0))
    cpu_stat(proc, 0, 0)
    source = ProgramSource(proc_root=proc, power_supply=supplies, clock=clock)
    source.sample()
    clock.now += 1
    for n in range(12):
        process(proc, 100 + n, f"/usr/bin/p{n:02}", (n + 1, 0))
    cpu_stat(proc, 78, 1000)
    names = [r.label.removesuffix(" CPU") for r in source.sample() if r.kind is Kind.LOAD]
    assert names == [f"p{n:02}" for n in range(11, 1, -1)]
    clock.now += 11  # past the window, with nothing new: the old samples are dropped
    assert source.sample() == []


def test_plugged_in_programs_get_no_watts(tmp_path: Path) -> None:
    proc, clock = tmp_path / "proc", Clock()
    supplies = battery(tmp_path, status="Charging", power="30000000")
    process(proc, 10, "/usr/bin/firefox", (0, 0))
    cpu_stat(proc, 0, 0)
    source = ProgramSource(proc_root=proc, power_supply=supplies, clock=clock)
    source.sample()
    clock.now += 1
    process(proc, 10, "/usr/bin/firefox", (10, 0))
    cpu_stat(proc, 10, 90)
    readings = {r.key: r.value for r in source.sample()}
    assert readings == {"programs/firefox/cpu": 10.0, "programs/firefox/power": None}


def test_a_reused_pid_counts_from_its_new_process_start(tmp_path: Path) -> None:
    proc, clock = tmp_path / "proc", Clock()
    supplies = battery(tmp_path)
    process(proc, 10, "/usr/bin/old", (500, 0), started=100)
    cpu_stat(proc, 0, 0)
    source = ProgramSource(proc_root=proc, power_supply=supplies, clock=clock)
    clock.now += 1
    (proc / "10" / "cmdline").unlink()
    process(proc, 10, "/usr/bin/new", (30, 0), started=900)  # the old one ended; its pid is reused
    cpu_stat(proc, 30, 70)
    assert {r.key: r.value for r in source.sample()}["programs/new/cpu"] == 30.0  # not 30 - 500


def test_one_sample_after_starting_already_has_programs(tmp_path: Path) -> None:
    """corewatch dump samples once, a moment after the sources start."""
    proc, clock = tmp_path / "proc", Clock()
    supplies = battery(tmp_path)
    process(proc, 10, "/usr/bin/firefox", (0, 0))
    cpu_stat(proc, 0, 0)
    source = ProgramSource(proc_root=proc, power_supply=supplies, clock=clock)
    clock.now += 0.5
    process(proc, 10, "/usr/bin/firefox", (10, 0))
    cpu_stat(proc, 10, 90)
    assert {r.key: r.value for r in source.sample()}["programs/firefox/cpu"] == 10.0


def test_no_battery_no_programs(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    process(proc, 10, "/usr/bin/firefox", (0, 0))
    cpu_stat(proc, 0, 0)
    source = ProgramSource(proc_root=proc, power_supply=tmp_path / "none", clock=Clock())
    assert source.sample() == [] and source.sample() == []


def test_discharge_watts(tmp_path: Path) -> None:
    supplies = tmp_path / "power_supply"
    write_tree(
        supplies,
        {
            "BAT0/type": "Battery",
            "BAT0/status": "Discharging",
            "BAT0/current_now": "-1500000",  # no power_now: current times voltage
            "BAT0/voltage_now": "12000000",
            "BAT1/type": "Battery",
            "BAT1/status": "Discharging",
            "BAT1/power_now": "6000000",
            "AC/type": "Mains",
        },
    )
    assert discharge_watts(supplies) == 24.0
    write_tree(supplies, {"BAT0/status": "Full", "BAT1/status": "Not charging"})
    assert discharge_watts(supplies) is None
    write_tree(supplies, {"BAT1/status": "Discharging", "BAT1/power_now": "?"})
    assert discharge_watts(supplies) is None  # how fast isn't known


def test_processes_are_read_at_most_once_a_second(tmp_path: Path) -> None:
    proc, clock = tmp_path / "proc", Clock()
    supplies = battery(tmp_path)
    process(proc, 10, "/usr/bin/firefox", (0, 0))
    cpu_stat(proc, 0, 0)
    source = ProgramSource(proc_root=proc, power_supply=supplies, clock=clock)
    source.sample()
    clock.now += 1
    process(proc, 10, "/usr/bin/firefox", (10, 0))
    cpu_stat(proc, 10, 90)
    first = source.sample()
    clock.now += 0.85  # short of MIN_SECONDS
    process(proc, 10, "/usr/bin/firefox", (90, 0))  # not read yet
    cpu_stat(proc, 90, 110)
    assert source.sample() == first
    clock.now += 0.1  # a timer firing a moment before the second is up still reads
    assert {r.key: r.value for r in source.sample()}["programs/firefox/cpu"] == 45.0  # 90 of 200 since the start
