"""CPU power draw from the RAPL energy counters (Intel, and AMD Zen).

The kernel exposes a cumulative energy counter in microjoules per power domain;
power is the change in energy divided by the elapsed time. Since 2020 these files
are readable by root only (CVE-2020-8694), so this source usually needs a udev
rule — see the README.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from corewatch.model import Kind, Reading
from corewatch.sources.base import cpu_device_name, read_int, read_text

MAX_PLAUSIBLE_WATTS = 2000.0

ZONE_DIR = re.compile(r"^intel-rapl:\d+(:\d+)?$")

ZONE_LABELS = {
    "package": "Package power",
    "core": "Cores power",
    "uncore": "Integrated graphics power",
    "dram": "Memory power",
    "psys": "Whole platform power",
}


@dataclass
class Zone:
    path: Path
    label: str
    max_range: int | None
    previous: tuple[int, float] | None = None


def _label(name: str) -> str:
    base, _, index = name.partition("-")
    label = ZONE_LABELS.get(base, f"{name} power")
    return f"{label} (CPU {index})" if base == "package" and index not in ("", "0") else label


class RaplSource:
    name = "rapl"

    def __init__(
        self,
        root: Path = Path("/sys/class/powercap"),
        proc_root: Path = Path("/proc"),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = cpu_device_name(proc_root)
        self.clock = clock
        self.zones: list[Zone] = []
        self.unreadable: list[Path] = []
        try:
            entries = sorted(p for p in root.iterdir() if ZONE_DIR.match(p.name))
        except OSError:
            entries = []
        for entry in entries:
            name = read_text(entry / "name")
            if name is None:
                continue
            try:
                (entry / "energy_uj").read_text()
            except PermissionError:
                self.unreadable.append(entry / "energy_uj")
                continue
            except OSError:
                continue
            self.zones.append(Zone(entry, _label(name), read_int(entry / "max_energy_range_uj")))
        self.sample()  # prime the counters

    def sample(self) -> list[Reading]:
        readings = []
        for zone in self.zones:
            energy = read_int(zone.path / "energy_uj")
            now = self.clock()
            value = None
            if energy is not None and zone.previous is not None:
                last_energy, last_time = zone.previous
                delta = energy - last_energy
                if delta < 0 and zone.max_range:  # counter wrapped around
                    delta += zone.max_range
                elapsed = now - last_time
                if delta >= 0 and elapsed > 0:
                    value = delta / elapsed / 1_000_000
                    # A counter reset (e.g. across suspend) looks like a wrap and yields an
                    # absurd spike; no desktop CPU draws anywhere near this.
                    if value > MAX_PLAUSIBLE_WATTS:
                        value = None
            zone.previous = (energy, now) if energy is not None else None
            readings.append(
                Reading(
                    key=f"rapl/{zone.path.name}", device=self.device, label=zone.label, kind=Kind.POWER, value=value
                )
            )
        return readings

    def notes(self) -> list[str]:
        if not self.unreadable:
            return []
        return [
            "CPU power draw is hidden because the kernel only lets root read the energy counters. "
            "See the README for a one-line udev rule that makes them readable."
        ]

    def close(self) -> None:
        pass
