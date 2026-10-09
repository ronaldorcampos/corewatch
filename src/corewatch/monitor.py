"""Polls every source and keeps min / max / average per sensor."""

import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from corewatch.model import Kind, Reading, Row, Stats, Status
from corewatch.sources.base import Source

# Within a device, show sensors in this order regardless of which source produced them.
KIND_ORDER = [
    Kind.TEMPERATURE,
    Kind.LOAD,
    Kind.CLOCK,
    Kind.POWER,
    Kind.FAN,
    Kind.FAN_DUTY,
    Kind.VOLTAGE,
    Kind.CURRENT,
    Kind.THROUGHPUT,
]
# Devices are listed by these name prefixes; anything else goes last, in discovery order.
DEVICE_ORDER = ["CPU", "Fans", "GPU", "Motherboard", "Memory", "NVMe", "Disk", "Network", "Wi-Fi"]
FANS = "Fans"
STORAGE = "Storage"
STORAGE_PREFIXES = ("NVMe", "Disk")


@dataclass(frozen=True)
class Collected:
    """One round of readings from every source, not yet folded into the statistics."""

    at: float
    results: list[tuple[str, list[Reading] | None, str | None]]  # (source, readings or None, error)
    notes: list[str]


class Monitor:
    def __init__(
        self,
        sources: Sequence[Source],
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """``clock`` times every interval and history point; it must never jump, so it is
        monotonic. ``wall_clock`` is only used to show when something happened."""
        self.sources = list(sources)
        self.clock = clock
        self.wall_clock = wall_clock
        self.last_sample_at: float | None = None
        self._stats: dict[str, Stats] = {}
        # Inputs that have reported "nothing attached" at least once this session. Floating
        # inputs only glitch to the placeholder now and then, so one sighting is enough.
        self.unused_keys: set[str] = set()
        # Fans seen spinning at least once. Unlike the statistics, Reset doesn't clear this:
        # a fan that spun and then stopped must never be mistaken for an empty header.
        self.spun_keys: set[str] = set()
        self._errors: dict[str, str] = {}
        self._notes: list[str] = []

    def collect(self) -> Collected:
        """Read every source. Touches only the sources, never the statistics, so the GUI runs
        this on a worker thread: kernel drivers can take tens of milliseconds to answer."""
        now = self.clock()
        results: list[tuple[str, list[Reading] | None, str | None]] = []
        notes: list[str] = []
        for source in self.sources:
            try:
                results.append((source.name, source.sample(), None))
            except Exception as error:  # one broken sensor must not stop the others
                results.append((source.name, None, f"Could not read {source.name} sensors: {error}"))
            with contextlib.suppress(Exception):  # a hint is never worth stopping the readings for
                notes.extend(source.notes())
        return Collected(now, results, notes)

    def ingest(self, collected: Collected) -> list[Row]:
        """Fold a collection into the statistics. Must run on the thread that displays them."""
        rows: list[Row] = []
        self.last_sample_at = collected.at
        self._notes = collected.notes
        for name, readings, error in collected.results:
            if readings is None:
                self._errors[name] = error or f"Could not read {name} sensors"
                continue
            self._errors.pop(name, None)
            for reading in readings:
                if reading.unused:
                    self.unused_keys.add(reading.key)
                if reading.kind is Kind.FAN and reading.value:
                    self.spun_keys.add(reading.key)
                stats = self._stats.setdefault(reading.key, Stats())
                stats.add(reading.value, collected.at, reading.status)
                rows.append(Row(reading, stats))
        return rows

    def sample(self) -> list[Row]:
        """Collect and ingest in one go, on the calling thread."""
        return self.ingest(self.collect())

    def visible_rows(self, rows: Sequence[Row], show_unused: bool) -> list[Row]:
        """``rows`` without the inputs that have nothing attached, unless asked to show them."""
        return visible_rows(rows, show_unused, self.unused_keys, self.spun_keys)

    def unused_rows(self, rows: Sequence[Row]) -> set[str]:
        """Keys of the rows that would be hidden as unused."""
        shown = {row.reading.key for row in self.visible_rows(rows, False)}
        return {row.reading.key for row in rows} - shown

    def to_wall(self, timestamp: float) -> float:
        """Convert a sample timestamp to wall-clock time for display."""
        return timestamp + (self.wall_clock() - self.clock())

    def notes(self) -> list[str]:
        """Hints from the sources and read errors, as of the last ingested collection."""
        return self._notes + list(self._errors.values())

    def reset(self) -> None:
        """Forget the recorded minimum, maximum, average and history of every sensor."""
        for stats in self._stats.values():
            stats.clear()

    def close(self) -> None:
        for source in self.sources:
            source.close()


def group_rows(rows: Sequence[Row]) -> list[tuple[str, list[Row]]]:
    """Group rows by device (CPU first, see DEVICE_ORDER) and sort each device's sensors by kind."""
    groups: dict[str, list[Row]] = {}
    for row in rows:
        groups.setdefault(row.reading.device, []).append(row)

    def rank(device: str) -> int:
        return next((i for i, prefix in enumerate(DEVICE_ORDER) if device.startswith(prefix)), len(DEVICE_ORDER))

    return [
        (device, sorted(device_rows, key=lambda r: KIND_ORDER.index(r.reading.kind)))
        for device, device_rows in sorted(groups.items(), key=lambda item: rank(item[0]))
    ]


def headline(rows: Sequence[Row]) -> Row | None:
    """The single temperature that best summarises the machine: the CPU's own reading (Intel's
    package, AMD's die temperature), else the hottest CPU sensor, else the hottest of all."""
    temps = [r for r in rows if r.reading.kind is Kind.TEMPERATURE and r.reading.value is not None]
    cpu_temps = [r for r in temps if r.reading.device.startswith("CPU")]
    for wanted in ("cpu package", "cpu temperature"):  # exact names first: not "(fan control)"
        for row in cpu_temps:
            if row.reading.label.lower() == wanted:
                return row
    for row in cpu_temps:
        if row.reading.label.lower().startswith(("cpu package", "package", "tdie", "tctl", "cpu temperature")):
            return row
    pool = cpu_temps or temps
    return max(pool, key=lambda r: r.reading.value or 0.0) if pool else None


def is_unused(row: Row, by_key: dict[str, Row], unused_keys: set[str], spun_keys: set[str]) -> bool:
    """Inputs with nothing attached: open thermistors, fan headers that never spun, and their controls.

    A fan only counts as empty if it hasn't spun since corewatch started (Reset doesn't
    change that), and nothing in a warning state is ever hidden: a stopped fan or an
    out-of-range reading is exactly what you need to see.
    """
    reading = row.reading
    if reading.unused or reading.key in unused_keys:  # known "nothing attached" placeholder
        return True
    if reading.status is not Status.OK:
        return False
    if reading.kind is Kind.FAN:
        return reading.empty_if_idle and reading.value == 0 and reading.key not in spun_keys
    if reading.companion is not None:
        companion = by_key.get(reading.companion)
        return companion is not None and is_unused(companion, by_key, unused_keys, spun_keys)
    return False


def visible_rows(
    rows: Sequence[Row], show_unused: bool, unused_keys: set[str] | None = None, spun_keys: set[str] | None = None
) -> list[Row]:
    if show_unused:
        return list(rows)
    by_key = {row.reading.key: row for row in rows}
    return [row for row in rows if not is_unused(row, by_key, unused_keys or set(), spun_keys or set())]


def gather_fans(rows: Sequence[Row]) -> list[Row]:
    """Move every fan speed and fan control row into one "Fans" card.

    Labels gain their source where it isn't obvious ("GPU fan 1"); motherboard headers
    keep their own names. If two devices would still end up with the same label (two
    motherboard chips both with a "Fan 1"), the device name is added to tell them apart.
    Keys and statistics are untouched, and ``origin`` keeps the real device.
    """
    labels: dict[str, str] = {}
    devices_by_label: dict[str, set[str]] = {}
    for row in rows:
        reading = row.reading
        if reading.kind not in (Kind.FAN, Kind.FAN_DUTY) or reading.device == FANS:
            continue
        source = reading.device.split(" · ")[0]
        label = reading.label
        if source != "Motherboard" and not label.startswith(source):
            label = f"{source} {label[:1].lower()}{label[1:]}"
        labels[reading.key] = label
        devices_by_label.setdefault(label, set()).add(reading.device)
    gathered = []
    for row in rows:
        reading = row.reading
        if reading.key not in labels:
            gathered.append(row)
            continue
        fan_label = labels[reading.key]
        if len(devices_by_label[fan_label]) > 1:
            fan_label = f"{fan_label} ({reading.device.split(' · ', 1)[-1]})"
        gathered.append(Row(replace(reading, device=FANS, label=fan_label, origin=reading.device), row.stats))
    return gathered


def drive_name(device: str) -> str:
    """The short name a drive goes by in the Storage card: ``nvme0`` for an NVMe drive, its
    model for a SATA one (``Disk · ST4000DM004`` -> ``ST4000DM004``)."""
    head, _, model = device.partition(" · ")
    if head.startswith("NVMe "):
        return head.removeprefix("NVMe ")
    return model or head


def gather_storage(rows: Sequence[Row]) -> list[Row]:
    """Move every drive's sensors into one "Storage" card, like the fans. Labels gain the
    drive's short name ("nvme0 Composite"), since every drive has a "Composite"; keys and
    statistics are untouched, and ``origin`` keeps the real device."""
    gathered = []
    for row in rows:
        reading = row.reading
        if reading.device.startswith(STORAGE_PREFIXES):
            label = f"{drive_name(reading.device)} {reading.label}"
            row = Row(replace(reading, device=STORAGE, label=label, origin=reading.device), row.stats)
        gathered.append(row)
    return gathered
