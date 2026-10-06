"""Shared plumbing for sensor sources."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from corewatch.model import Reading


class Source(Protocol):
    """Anything that can produce a list of readings on demand."""

    name: str

    def sample(self) -> list[Reading]: ...

    def notes(self) -> list[str]:
        """Plain-language hints about sensors this source could not read."""
        ...

    def close(self) -> None: ...


def read_text(path: Path) -> str | None:
    """Read a small sysfs file, returning None if it is missing or unreadable."""
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_int(path: Path) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def cpu_model_name(proc_root: Path = Path("/proc")) -> str | None:
    """The CPU marketing name, tidied up: ``Intel Core i7-13700K``."""
    text = read_text(proc_root / "cpuinfo")
    if text is None:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "model name" and value.strip():
            name = re.sub(r"\((R|TM|tm|r)\)", "", value)
            name = re.sub(r"^\d+(st|nd|rd|th) Gen ", "", name.strip())
            name = re.sub(r"\s+\d+-Core Processor$", "", name)
            return re.sub(r"\s+", " ", name).strip()
    return None


def cpu_device_name(proc_root: Path = Path("/proc")) -> str:
    model = cpu_model_name(proc_root)
    return f"CPU · {model}" if model else "CPU"


@dataclass(frozen=True)
class Core:
    """A physical core: its hardware IDs, its logical CPUs, and the name people see."""

    package: int
    core_id: int
    threads: tuple[int, ...]
    name: str


def parse_cpu_list(text: str | None) -> set[int]:
    """Parse the kernel's CPU list format: ``0-15,18,20-21``."""
    cpus: set[int] = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        first, _, last = part.partition("-")
        try:
            cpus.update(range(int(first), int(last or first) + 1))
        except ValueError:
            continue
    return cpus


CPU_DIR = re.compile(r"^cpu(\d+)$")


def discover_cores(sys_cpu: Path = Path("/sys/devices/system/cpu")) -> list[Core]:
    """Physical cores in hardware order, numbered the way people count them.

    The kernel's core IDs have gaps (an i7-13700K reports 0, 4, 8 ... 28, 32-39), so cores
    get sequential names instead. On Intel hybrid CPUs, performance and efficiency cores
    are counted separately: ``P-core 0`` ... ``E-core 0`` ...
    """
    grouped: dict[tuple[int, int], list[int]] = {}
    try:
        entries = list(sys_cpu.iterdir())
    except OSError:
        return []
    for entry in entries:
        match = CPU_DIR.match(entry.name)
        if not match:
            continue
        package = read_int(entry / "topology" / "physical_package_id")
        core_id = read_int(entry / "topology" / "core_id")
        if package is None or core_id is None:  # offline CPUs have no topology
            continue
        grouped.setdefault((package, core_id), []).append(int(match.group(1)))

    devices = sys_cpu.parent.parent  # /sys/devices
    performance = parse_cpu_list(read_text(devices / "cpu_core" / "cpus"))
    efficiency = parse_cpu_list(read_text(devices / "cpu_atom" / "cpus"))
    hybrid = bool(performance and efficiency)
    multi_package = len({package for package, _ in grouped}) > 1
    counters: dict[tuple[int, str], int] = {}
    cores = []
    for (package, core_id), threads in sorted(grouped.items()):
        kind = "Core"
        if hybrid:
            kind = "E-core" if set(threads) <= efficiency else "P-core"
        number = counters.get((package, kind), 0)
        counters[(package, kind)] = number + 1
        name = f"CPU {package} {kind.lower()} {number}" if multi_package else f"{kind} {number}"
        cores.append(Core(package, core_id, tuple(sorted(threads)), name))
    return cores
