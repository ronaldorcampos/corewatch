"""Intel graphics, integrated and discrete (Arc): clock speed, how much of the time it's
awake, and a discrete card's power draw.

i915 and xe publish a hwmon chip only for discrete cards; its temperatures, fans and voltage
are read by the hwmon source under the same card name (see ``intel_gpu_name``). The clock and
the sleep counter live in the DRM sysfs files, read here for every Intel GPU. The card's
power comes as an energy counter, which the hwmon source doesn't read, so it's worked out here.

Integrated graphics has no temperature sensor of its own (its heat shows in the CPU package
temperature), and its power draw comes from RAPL (see rapl.py), which files it under its card.

Busy time per engine would need the i915/xe perf counters, which Ubuntu closes to normal
users (perf_event_paranoid). The sleep-state counter is open to everyone: the share of time
the GPU wasn't asleep is how much it was used.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from corewatch.model import Kind, Reading
from corewatch.sources.base import MAX_PLAUSIBLE_WATTS, read_int, read_text
from corewatch.sources.hwmon import (
    INTEL_GPU_DRIVERS,
    INTEL_GPU_POWER,
    INTEL_INTEGRATED_SLOT,
    INTEL_VENDOR,
    METADATA_SECONDS,
    PCI_IDS,
    _natural_key,
    intel_gpu_name,
    unused_gpu,
)

CARD_DIR = re.compile(r"^card\d+$")  # not the connectors (card1-HDMI-A-1)
HWMON_DIR = re.compile(r"^hwmon\d+$")


@dataclass
class EnergyCounter:
    number: int  # energyN_input: 1 is the whole card, 2 the GPU chip (xe only)
    label: str
    cap_file: Path  # powerN_max, the sustained power limit, shown for reference


@dataclass
class IntelGpu:
    slot: str  # PCI address, which keeps each card's readings apart
    pci: Path
    driver: str
    device: str  # the card's name in corewatch, e.g. "GPU · Intel UHD Graphics 770"
    integrated: bool
    clock_file: Path  # the actual clock in MHz; 0 while asleep
    max_clock_file: Path
    asleep_ms_file: Path  # cumulative milliseconds spent asleep (RC6 / gt-idle)
    hwmon: Path | None = None  # discrete cards only
    energy: list[EnergyCounter] = field(default_factory=list)


def _counter_files(card: Path, pci: Path, driver: str) -> tuple[Path, Path, Path]:
    """The first GT's actual clock, maximum clock and time-asleep counter."""
    if driver == "xe":
        gt = pci / "tile0" / "gt0"
        return gt / "freq0" / "act_freq", gt / "freq0" / "rp0_freq", gt / "gtidle" / "idle_residency_ms"
    gt = card / "gt" / "gt0"
    if gt.is_dir():
        return gt / "rps_act_freq_mhz", gt / "rps_RP0_freq_mhz", gt / "rc6_residency_ms"
    # i915 before kernel 5.19 has no per-GT folder; the card-wide files are the same GT.
    return card / "gt_act_freq_mhz", card / "gt_RP0_freq_mhz", card / "power" / "rc6_residency_ms"


def _hwmon_of(pci: Path) -> Path | None:
    try:
        chips = sorted(
            (p for p in (pci / "hwmon").iterdir() if HWMON_DIR.match(p.name)), key=lambda p: _natural_key(p.name)
        )
    except OSError:
        return None
    return chips[0] if chips else None


def _energy_counters(hwmon: Path) -> list[EnergyCounter]:
    counters = []
    for number in (1, 2):
        if not (hwmon / f"energy{number}_input").exists():
            continue
        if (hwmon / f"power{number}_input").exists():
            continue  # the driver reports this power directly, and the hwmon source shows it
        label = read_text(hwmon / f"energy{number}_label")
        counters.append(
            EnergyCounter(number, INTEL_GPU_POWER.get(label or "card", label or ""), hwmon / f"power{number}_max")
        )
    return counters


def find_intel_gpus(drm_root: Path = Path("/sys/class/drm"), pci_ids: tuple[Path, ...] = PCI_IDS) -> list[IntelGpu]:
    try:
        cards = sorted((p for p in drm_root.iterdir() if CARD_DIR.match(p.name)), key=lambda p: _natural_key(p.name))
    except OSError:
        return []
    gpus = []
    for card in cards:
        pci = card / "device"
        try:
            slot, driver = pci.resolve().name, (pci / "driver").resolve().name
        except OSError:
            continue
        if driver not in INTEL_GPU_DRIVERS or read_text(pci / "vendor") != INTEL_VENDOR:
            continue
        hwmon = _hwmon_of(pci)
        gpus.append(
            IntelGpu(
                slot,
                pci,
                driver,
                intel_gpu_name(pci, pci_ids),
                slot == INTEL_INTEGRATED_SLOT,
                *_counter_files(card, pci, driver),
                hwmon=hwmon,
                energy=_energy_counters(hwmon) if hwmon else [],
            )
        )
    _number_twins(gpus)
    # Discrete cards first, so their cards are listed before the integrated GPU's.
    return sorted(gpus, key=lambda gpu: gpu.integrated)


def _number_twins(gpus: list[IntelGpu]) -> None:
    """Two cards of the same model get "(2)" on the second, numbered the way the hwmon source
    numbers their chips (in hwmonN order), so each card's readings stay on one card."""
    by_name: dict[str, list[IntelGpu]] = {}
    for gpu in gpus:
        by_name.setdefault(gpu.device, []).append(gpu)
    for twins in by_name.values():
        twins.sort(key=lambda g: (g.hwmon is None, _natural_key(g.hwmon.name) if g.hwmon else ("", 0), g.slot))
        for index, gpu in enumerate(twins[1:], start=2):
            gpu.device = f"{gpu.device} ({index})"


def find_integrated_gpu(
    drm_root: Path = Path("/sys/class/drm"), pci_ids: tuple[Path, ...] = PCI_IDS
) -> IntelGpu | None:
    return next((gpu for gpu in find_intel_gpus(drm_root, pci_ids) if gpu.integrated), None)


class IntelGpuSource:
    name = "intel-gpu"

    def __init__(
        self,
        drm_root: Path = Path("/sys/class/drm"),
        pci_ids: tuple[Path, ...] = PCI_IDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.drm_root, self.pci_ids, self.clock = drm_root, pci_ids, clock
        self.gpus = find_intel_gpus(drm_root, pci_ids)
        self._found_at = clock()
        # Last reading of each energy counter, as (counter value, time).
        self._previous: dict[str, tuple[int, float]] = {}
        # The same for the time-asleep counters, plus how much of the value was assumed rather
        # than read (see _active_percent).
        self._asleep: dict[str, tuple[int, float, int]] = {}
        # Fixed limits (maximum clock, power limits), remembered from when the card was awake.
        self._limits: dict[str, float | None] = {}
        self.sample()  # prime the counters so the first real sample has values

    def sample(self) -> list[Reading]:
        if self.clock() - self._found_at >= METADATA_SECONDS:
            # Cards can come and go (driver reloaded, card rebound), like hwmon chips.
            self.gpus, self._found_at = find_intel_gpus(self.drm_root, self.pci_ids), self.clock()
        readings = []
        for gpu in self.gpus:
            key = f"intel-gpu/{gpu.slot}"
            # A GPU nothing is using is left alone (see unused_gpu): it runs at 0 MHz, doing nothing.
            idle = unused_gpu(gpu.pci)
            clock = 0 if idle else read_int(gpu.clock_file)
            active = self._active_percent(gpu, idle)
            # i915 reports its clock limits without waking the GPU (checked on a UHD 770); xe may not.
            max_clock = self._limit(f"{key}/clock", gpu.max_clock_file, 1, idle and gpu.driver != "i915")
            integrated = gpu.integrated
            readings.append(
                Reading(
                    f"{key}/clock",
                    gpu.device,
                    "Graphics clock",
                    Kind.CLOCK,
                    clock,
                    cap=max_clock,
                    integrated=integrated,
                )
            )
            readings.append(
                Reading(f"{key}/active", gpu.device, "Active time", Kind.LOAD, active, integrated=integrated)
            )
            for counter in gpu.energy:
                name = f"{key}/power{counter.number}"
                # µW; shown for reference, never a warning
                power_limit = self._limit(name, counter.cap_file, 1_000_000, idle)
                # Left blank while idle: a powered-down card still draws a little, and how much
                # isn't known without reading the counter, which would keep it awake.
                watts = None if idle else self._watts(gpu, counter)
                readings.append(
                    Reading(name, gpu.device, counter.label, Kind.POWER, watts, cap=power_limit, integrated=integrated)
                )
        return readings

    def _limit(self, name: str, file: Path, divisor: int, asleep: bool) -> float | None:
        if not asleep:
            raw = read_int(file)
            self._limits[name] = raw / divisor if raw else None  # 0 means "not reported"
        return self._limits.get(name)

    def _rate(self, name: str, value: int | None) -> tuple[int, float] | None:
        """How much a counter grew since the last sample, and over how many seconds; None when
        there's no earlier reading or the counter went backwards (driver reload, suspend). While
        the GPU is powered down it isn't read, and the last reading stays the starting point."""
        now = self.clock()
        previous = self._previous.pop(name, None)
        if value is None:
            return None
        self._previous[name] = (value, now)
        if previous is None or value < previous[0] or now <= previous[1]:
            return None
        return value - previous[0], now - previous[1]

    def _active_percent(self, gpu: IntelGpu, idle: bool) -> float | None:
        """The share of the time since the last sample the GPU spent awake.

        The time-asleep counter is only read while something is using the GPU (see unused_gpu).
        Otherwise it did nothing, and the starting point moves on as if the counter had been
        read and found the GPU asleep the whole time."""
        name = gpu.slot
        now = self.clock()
        previous = self._asleep.get(name)
        if idle:
            if previous is not None:
                last_ms, last_time, assumed_ms = previous
                slept_ms = round((now - last_time) * 1000)
                self._asleep[name] = (last_ms + slept_ms, now, assumed_ms + slept_ms)
            return 0.0
        value = read_int(gpu.asleep_ms_file)
        if value is None:
            self._asleep.pop(name, None)
            return None
        self._asleep[name] = (value, now, 0)
        if previous is None or now <= previous[1]:
            return None
        last_ms, last_time, assumed_ms = previous
        if value < last_ms - assumed_ms:  # further back than any assumption: the driver reset it
            return None
        elapsed_ms = (now - last_time) * 1000
        # Below the starting point when it did some work between samples that found it unused:
        # that work shows up now. The two counters also tick separately, hence the clamp both ways.
        asleep_ms = min(max(value - last_ms, 0), elapsed_ms)
        return (1.0 - asleep_ms / elapsed_ms) * 100

    def _watts(self, gpu: IntelGpu, counter: EnergyCounter) -> float | None:
        change = self._rate(
            f"{gpu.slot}/energy{counter.number}",
            read_int(gpu.hwmon / f"energy{counter.number}_input") if gpu.hwmon else None,
        )
        if change is None:
            return None
        microjoules, seconds = change
        watts = microjoules / seconds / 1_000_000
        return watts if watts <= MAX_PLAUSIBLE_WATTS else None

    def notes(self) -> list[str]:
        return []

    def close(self) -> None:
        pass
