"""Which programs use the battery most: each one's share of the CPU's time, and an estimate of
the power that share stands for.

Linux keeps no count of the energy each program uses, only of the CPU time it gets
(``/proc/<pid>/stat``). So a program's power is estimated as its share of the time the CPUs were
busy, times what the battery gives out. That leaves out what no program is charged for (the
display, Wi-Fi, the chip idling) and work on the GPU, so it overstates busy programs and misses
graphics-heavy ones: it ranks them well, but the watts are a rough guide. Only while running on
the battery; plugged in, the battery's figure is what charges it, so programs get their CPU share
alone.
"""

import ntpath
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from corewatch.model import Kind, Reading
from corewatch.sources.base import is_battery, read_int, read_text
from corewatch.sources.cpu import parse_proc_stat

DEVICE = "Battery · programs"
TOP = 10
# Ranked, and averaged, over this much time: per sample, a program near tenth place would come
# and go every second.
WINDOW_SECONDS = 10.0
KERNEL = "Linux kernel"  # kernel threads
PF_KTHREAD = 0x00200000  # in a process's flags (/proc/<pid>/stat field 9): a kernel thread
# Reading every process takes ~10-20 ms of CPU: at the shortest update intervals, the readings
# from the last read stand until this much time has passed, so watching the battery doesn't drain
# it. A tenth short of it, so a timer firing a moment early at a 1 s interval still reads.
MIN_SECONDS = 0.9
WINDOWS_PATH = re.compile(r"^[A-Za-z]:\\")  # a Wine or Proton program: C:\Program Files\...


@dataclass(frozen=True)
class Window:
    """One sample's share of the window: CPU ticks per program, every CPU's busy and total ticks,
    and the battery's draw (None when it isn't running on the battery)."""

    at: float
    ticks: dict[str, int]
    busy: int
    total: int
    watts: float | None


def parse_pid_stat(text: str) -> int | None:
    """User plus system ticks from ``/proc/<pid>/stat``. The name in brackets may hold spaces
    and brackets of its own, so the fields are counted from the last ``)``."""
    fields = text.rpartition(")")[2].split()
    try:
        return int(fields[11]) + int(fields[12])  # utime and stime, fields 14 and 15
    except (IndexError, ValueError):
        return None


def program_name(proc: Path, stat: str) -> str | None:
    """What a process is called, grouped by program: its command's file name, so all of a
    browser's processes are one ``firefox``. ``stat`` is its /proc/<pid>/stat. None when it has
    gone."""
    fields = stat.rpartition(")")[2].split()
    if len(fields) > 6 and fields[6].isdigit() and int(fields[6]) & PF_KTHREAD:
        return KERNEL
    try:
        raw = (proc / "cmdline").read_bytes()
    except OSError:
        return None
    arguments = raw.rstrip(b"\0").split(b"\0")
    argv0 = arguments[0].decode(errors="replace")
    if not argv0:  # a process that has exited, or is exiting: the name the kernel keeps
        return stat[stat.find("(") + 1 : stat.rfind(")")] or None
    if WINDOWS_PATH.match(argv0):
        return ntpath.basename(argv0) or argv0
    # Some programs rewrite their command line into one string ("sshd: user@pts/0"); a path with
    # spaces in it ("/opt/Some App/app") is one too, but it's there.
    if len(arguments) == 1 and " " in argv0 and not os.path.exists(argv0):
        argv0 = argv0.split(" ", 1)[0].rstrip(":")
    return os.path.basename(argv0) or argv0


def discharge_watts(power_supply: Path) -> float | None:
    """What the machine's batteries give out, while it runs on them; else None."""
    try:
        batteries = [entry for entry in sorted(power_supply.iterdir()) if is_battery(entry)]
    except OSError:
        return None
    total, discharging = 0.0, False
    for battery in batteries:
        if read_text(battery / "status") != "Discharging":
            continue
        power = read_int(battery / "power_now")
        if power is None:
            current, voltage = read_int(battery / "current_now"), read_int(battery / "voltage_now")
            power = current * voltage // 1_000_000 if current is not None and voltage is not None else None
        if power is None:
            return None  # discharging, but how fast isn't known
        total += abs(power) / 1_000_000
        discharging = True
    return total if discharging else None


class ProgramSource:
    """The ``TOP`` programs by CPU time over the last ``WINDOW_SECONDS``, on machines with a
    battery. Each has its CPU share (of every core, like "CPU load (all cores)") and its
    estimated power, as two readings under ``DEVICE``."""

    name = "programs"

    def __init__(
        self,
        proc_root: Path = Path("/proc"),
        power_supply: Path = Path("/sys/class/power_supply"),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.proc_root = proc_root
        self.power_supply = power_supply
        self.clock = clock
        self._names: dict[tuple[int, str], str] = {}  # (pid, its start time) -> program
        self._previous: tuple[dict[tuple[int, str], int], int, int] | None = None
        self._window: deque[Window] = deque()
        self._last: tuple[float, list[Reading]] | None = None
        # A first read to compare the next with, so even a single sample (corewatch dump) has them.
        self.sample()
        self._last = None

    def _has_battery(self) -> bool:
        try:
            return any(is_battery(entry) for entry in self.power_supply.iterdir())
        except OSError:
            return False

    def _processes(self) -> dict[tuple[int, str], tuple[str, int]]:
        """Every process's program and CPU ticks so far, by its pid and start time (a pid is reused
        once its process ends). Hundreds of files each sample, so read with plain strings and
        open(): Path and read_text cost several times as much here."""
        found = {}
        root = str(self.proc_root)
        try:
            entries = os.listdir(root)
        except OSError:
            return {}
        live = set()
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"{root}/{entry}/stat", "rb") as file:
                    text = file.read().decode(errors="replace")
            except OSError:  # gone since the listing
                continue
            ticks = parse_pid_stat(text)
            if ticks is None:
                continue
            started = text.rpartition(")")[2].split()[19:20]
            identity = (int(entry), started[0] if started else "")
            live.add(identity)
            name = self._names.get(identity)
            if name is None:
                name = program_name(self.proc_root / entry, text)
                if name is None:
                    continue
                self._names[identity] = name
            found[identity] = (name, ticks)
        for gone in self._names.keys() - live:
            del self._names[gone]
        return found

    def sample(self) -> list[Reading]:
        if not self._has_battery():
            return []
        now = self.clock()
        if self._last is not None and now - self._last[0] < MIN_SECONDS:
            return self._last[1]
        processes = self._processes()
        cpu = parse_proc_stat(read_text(self.proc_root / "stat") or "").get("cpu")
        current = {identity: ticks for identity, (_, ticks) in processes.items()}
        if cpu is None:
            return []
        previous, self._previous = self._previous, (current, cpu.busy, cpu.total)
        if previous is not None:
            before, busy_before, total_before = previous
            ticks: dict[str, int] = {}
            for identity, (name, after) in processes.items():
                spent = after - before.get(identity, 0)  # a process new since then: from its start
                if spent > 0:
                    ticks[name] = ticks.get(name, 0) + spent
            busy, total = cpu.busy - busy_before, cpu.total - total_before
            if total > 0 and busy >= 0:
                self._window.append(Window(now, ticks, busy, total, discharge_watts(self.power_supply)))
        while self._window and self._window[0].at <= now - WINDOW_SECONDS:
            self._window.popleft()
        readings = self._readings()
        self._last = (now, readings)
        return readings

    def _readings(self) -> list[Reading]:
        if not self._window:
            return []
        ticks: dict[str, int] = {}
        for window in self._window:
            for name, spent in window.ticks.items():
                ticks[name] = ticks.get(name, 0) + spent
        busy = sum(window.busy for window in self._window)
        total = sum(window.total for window in self._window)
        # Each sample's draw counts for its share of the window's busy time, so a burst of work
        # is paired with the power drawn while it ran. Unknown if any sample ran off the battery.
        draws = [window.watts for window in self._window]
        watts = (
            sum(w * window.busy for w, window in zip(draws, self._window, strict=True) if w is not None) / busy
            if busy and all(w is not None for w in draws)
            else None
        )
        top = sorted(ticks.items(), key=lambda item: (-item[1], item[0]))[:TOP]
        readings = []
        for name, spent in top:
            share = spent / busy if busy else 0.0
            power = watts * min(share, 1.0) if watts is not None else None
            cpu = spent * 100 / total
            readings.append(Reading(f"programs/{name}/cpu", DEVICE, f"{name} CPU", Kind.LOAD, cpu, transient=True))
            readings.append(
                Reading(f"programs/{name}/power", DEVICE, f"{name} power (est.)", Kind.POWER, power, transient=True)
            )
        return readings

    def notes(self) -> list[str]:
        return []

    def close(self) -> None:
        pass
