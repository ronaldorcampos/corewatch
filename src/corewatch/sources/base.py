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


# Power worked out from an energy counter: a counter reset (driver reload, suspend) looks like
# a huge jump, and no desktop part draws anywhere near this.
MAX_PLAUSIBLE_WATTS = 2000.0


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


def read_hex(path: Path) -> int | None:
    """A register the kernel prints in hex: ``0x00000000612f0250``."""
    text = read_text(path)
    try:
        return int(text, 16) if text else None
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
    # ARM's cpuinfo names no model. An Apple Mac's device tree names the machine, chip included.
    if (model := machine_model(proc_root)) and (chip := APPLE_CHIP.search(model)):
        return f"Apple {chip.group(1)}"
    return None


# "Apple MacBook Pro (16-inch, M1 Pro, 2021)", "Apple Mac mini (M2, 2023)"
APPLE_CHIP = re.compile(r"^Apple .*\b(M\d+(?: Pro| Max| Ultra)?)\b")


def machine_model(proc_root: Path = Path("/proc")) -> str | None:
    """The machine's name from its device tree (ARM boards, Apple Macs), or None (x86, which has
    DMI instead). The kernel ends it with a NUL."""
    text = read_text(proc_root / "device-tree" / "model")
    model = text.rstrip("\0").strip() if text else None
    return model or None


def is_battery(device: Path) -> bool:
    """Whether a sysfs device is the machine's own battery in the power supply class: not a
    mouse's or a keyboard's, which the kernel scopes to their device."""
    return (
        device.resolve().parent.name == "power_supply"
        and read_text(device / "type") == "Battery"
        and read_text(device / "scope") != "Device"
    )


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
    # Only where core IDs restart in each cluster (Apple's, many ARM chips'): what tells apart
    # cores that share a core ID, the cluster or, where clusters can't, the core's first CPU.
    # None where core IDs are unique in their package, as on x86.
    cluster: int | None = None

    @property
    def key(self) -> str:
        """What the core's sensor keys end in: ``0/4``, or ``0/1/0`` where clusters repeat IDs."""
        if self.cluster is None:
            return f"{self.package}/{self.core_id}"
        return f"{self.package}/{self.cluster}/{self.core_id}"


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
    get sequential names instead. On hybrid CPUs, performance and efficiency cores are counted
    separately: ``P-core 0`` ... ``E-core 0`` ... Intel says which are which; on ARM (Apple's
    M chips among them) the efficiency cores are the ones the kernel rates less capable.
    """
    # A core is the CPUs sharing a core ID. Where core IDs restart in each cluster (Apple's, many
    # ARM chips'), CPUs sharing one are told apart by the threads the kernel lists for each: a
    # sibling list only ever splits a core ID, never joins two (AMD's Bulldozer lists a compute
    # unit's two cores as siblings). On x86 a package's core IDs are unique, so nothing splits.
    found: dict[tuple[int, int, frozenset[int] | None], tuple[list[int], list[int]]] = {}
    capacity: dict[int, int] = {}
    part: dict[int, int] = {}
    try:
        entries = list(sys_cpu.iterdir())
    except OSError:
        return []
    for entry in entries:
        match = CPU_DIR.match(entry.name)
        if not match:
            continue
        topology = entry / "topology"
        package = read_int(topology / "physical_package_id")
        core_id = read_int(topology / "core_id")
        if package is None or core_id is None:  # offline CPUs have no topology
            continue
        cpu = int(match.group(1))
        siblings = parse_cpu_list(
            read_text(topology / "core_cpus_list") or read_text(topology / "thread_siblings_list")
        )
        group = (package, core_id, frozenset(siblings | {cpu}) if siblings else None)
        threads, clusters = found.setdefault(group, ([], []))
        threads.append(cpu)
        if (cluster := read_int(topology / "cluster_id")) is not None:
            clusters.append(cluster)
        if (value := read_int(entry / "cpu_capacity")) is not None:
            capacity[cpu] = value
        if (midr := read_hex(entry / "regs" / "identification" / "midr_el1")) is not None:
            part[cpu] = (midr >> 24 & 0xFF) << 12 | (midr >> 4 & 0xFFF)  # implementer and part number
    ids = [(package, core_id) for package, core_id, _ in found]
    split = len(ids) != len(set(ids))
    # Where core IDs are split, the cluster joins the key: unless a cluster isn't reported, is the
    # kernel's "none" (-1, or one value every CPU shares), or doesn't tell the cores apart. Then
    # each core's first CPU does, for every core, so keys stay unique and in hardware order.
    tags: dict[tuple[int, int, frozenset[int] | None], int | None] = dict.fromkeys(found)
    if split:
        by_cluster = {group: min(clusters, default=-1) for group, (_, clusters) in found.items()}
        keys = [(group[0], by_cluster[group], group[1]) for group in found]
        if min(by_cluster.values()) < 0 or len(keys) != len(set(keys)):
            by_cluster = {group: min(threads) for group, (threads, _) in found.items()}
        tags = dict(by_cluster)

    devices = sys_cpu.parent.parent  # /sys/devices
    performance = parse_cpu_list(read_text(devices / "cpu_core" / "cpus"))
    efficiency = parse_cpu_list(read_text(devices / "cpu_atom" / "cpus"))
    if not (performance and efficiency):
        online = {cpu for threads, _ in found.values() for cpu in threads}
        performance, efficiency = by_capacity(capacity, part, online)
    hybrid = bool(performance and efficiency)
    multi_package = len({package for package, _ in ids}) > 1

    def order(group: tuple[int, int, frozenset[int] | None]) -> tuple[int, ...]:
        package, core_id, _ = group
        tag = tags[group]
        return (package, tag if tag is not None else -1, core_id, min(found[group][0]))

    counters: dict[tuple[int, str], int] = {}
    cores = []
    for group in sorted(found, key=order):
        package, core_id, _ = group
        threads = found[group][0]
        kind = "Core"
        if hybrid:
            kind = "E-core" if set(threads) <= efficiency else "P-core"
        number = counters.get((package, kind), 0)
        counters[(package, kind)] = number + 1
        name = f"CPU {package} {kind.lower()} {number}" if multi_package else f"{kind} {number}"
        cores.append(Core(package, core_id, tuple(sorted(threads)), name, tags[group]))
    return cores


def by_capacity(capacity: dict[int, int], part: dict[int, int], online: set[int]) -> tuple[set[int], set[int]]:
    """Performance and efficiency CPUs on ARM, from how capable the kernel rates each one, or two
    empty sets. Only with exactly two ratings (big.LITTLE): with three, as in phone chips, the middle
    ones are neither. Every online CPU must have one, or which is which isn't known. Where the CPUs
    name their design (MIDR), the two groups must be different designs too: one design whose
    clusters top out at different clocks is rated twice, but has one kind of core."""
    if len(set(capacity.values())) != 2 or set(capacity) != online:
        return set(), set()
    top = max(capacity.values())
    efficiency = {cpu for cpu, value in capacity.items() if value < top}
    performance = online - efficiency
    if set(part) == online and {part[cpu] for cpu in efficiency} & {part[cpu] for cpu in performance}:
        return set(), set()
    return performance, efficiency
