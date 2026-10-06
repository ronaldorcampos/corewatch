"""Kernel hwmon sensors: CPU, motherboard, disks, network cards and more.

Every hardware-monitoring driver on Linux publishes its sensors under
``/sys/class/hwmon/hwmonN`` using one naming scheme (``temp1_input``,
``fan2_input``, ``in0_input`` ...), so one reader covers all of them.
See https://docs.kernel.org/hwmon/sysfs-interface.html.
"""

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from corewatch.model import Kind, Reading
from corewatch.sources.base import cpu_device_name, discover_cores, read_int, read_text

# sysfs prefix -> (kind, divisor to reach the display unit, default label)
CHANNEL_TYPES = {
    "temp": (Kind.TEMPERATURE, 1000, "Temperature"),
    "fan": (Kind.FAN, 1, "Fan"),
    "pwm": (Kind.FAN_DUTY, 255 / 100, "Fan control"),
    "in": (Kind.VOLTAGE, 1000, "Voltage"),
    "power": (Kind.POWER, 1_000_000, "Power"),
    "curr": (Kind.CURRENT, 1000, "Current"),
    "freq": (Kind.CLOCK, 1_000_000, "Clock"),
}
ORDER = list(CHANNEL_TYPES)

VALUE_FILE = re.compile(r"^(temp|fan|in|power|curr|freq)(\d+)_(input|average)$|^(pwm)(\d+)$")

CPU_DRIVERS = {"coretemp", "k10temp", "zenpower"}
CORETEMP_CORE = re.compile(r"^Core (\d+)$")
MOTHERBOARD_DRIVERS = re.compile(
    r"^(nct\d+|it\d+|w83\w+|f71\w+|asus.?ec.?sensors|asus.?wmi.?sensors|dell_smm|thinkpad)"
)

SUPER_IO_NCT679X = re.compile(r"^nct679\d$")

# Nuvoton NCT679x chips wire these voltage inputs internally, identically on every board.
# The driver already applies the internal scaling. Values are (label, multiplier).
NCT679X_VOLTAGES = {
    2: ("AVCC", 1.0),
    3: ("+3.3V", 1.0),
    7: ("+3.3V standby", 1.0),
    8: ("CMOS battery", 1.0),
}
# ASUS boards feed Vcore to in0 and the +5V / +12V rails to in1 / in4 through 1:5 and 1:12
# resistor dividers, so those readings need multiplying back up.
ASUS_NCT679X_VOLTAGES = {
    0: ("CPU core (Vcore)", 1.0),
    1: ("+5V", 5.0),
    4: ("+12V", 12.0),
}

# Super I/O temperature inputs with nothing attached read one of these.
UNCONNECTED_TEMPS = {127.0, -128.0, 0.0}

# Temperature limits outside this window are placeholders, not real thresholds
# (NVMe drives report e.g. -273.1 °C / +65261.8 °C for "no limit", some chips 0 °C).
PLAUSIBLE_TEMP = (0.0, 200.0)


@dataclass(frozen=True)
class Channel:
    prefix: str
    number: int
    value_file: str


@dataclass(frozen=True)
class ChannelInfo:
    """Everything about a channel except its current value: read once, then cached."""

    key: str
    label: str
    kind: Kind
    value_file: str
    divisor: float
    low: float | None
    high: float | None
    crit: float | None
    companion: str | None


@dataclass(frozen=True)
class ChipInfo:
    device: str
    channels: list[ChannelInfo]
    read_at: float


# Labels and limits rarely change, and on NVMe drives every read is a command sent to the
# drive (~2-5 ms each), so they are re-read only this often. Values are read every sample.
METADATA_SECONDS = 30.0


def _natural_key(name: str) -> tuple[str, int]:
    match = re.match(r"^(\D*)(\d*)$", name)
    if not match:
        return name, 0
    return match.group(1), int(match.group(2) or 0)


class HwmonSource:
    name = "hwmon"

    def __init__(
        self,
        root: Path = Path("/sys/class/hwmon"),
        proc_root: Path = Path("/proc"),
        dmi_root: Path = Path("/sys/class/dmi/id"),
        sys_cpu: Path = Path("/sys/devices/system/cpu"),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.clock = clock
        self._chips: dict[Path, ChipInfo] = {}
        self.sys_cpu = sys_cpu
        self._core_names: dict[tuple[int, int], str] | None = None
        self.proc_root = proc_root
        self.dmi_root = dmi_root
        self._found_kinds: set[Kind] = set()

    def sample(self) -> list[Reading]:
        readings: list[Reading] = []
        used_names: dict[str, int] = {}
        try:
            chips = sorted(
                (p for p in self.root.iterdir() if p.name.startswith("hwmon")), key=lambda p: _natural_key(p.name)
            )
        except OSError:
            chips = []
        now = self.clock()
        for chip in self._chips.keys() - set(chips):
            del self._chips[chip]  # driver unloaded
        for chip in chips:
            info = self._chips.get(chip)
            if info is None or now - info.read_at >= METADATA_SECONDS:
                info = self._chips[chip] = self._describe_chip(chip, now)
            if not info.channels:
                continue
            used_names[info.device] = used_names.get(info.device, 0) + 1
            device = info.device if used_names[info.device] == 1 else f"{info.device} ({used_names[info.device]})"
            for channel in info.channels:
                readings.append(self._read(chip, device, channel))
        self._found_kinds = {r.kind for r in readings}
        return readings

    def _describe_chip(self, chip: Path, now: float) -> ChipInfo:
        chip_id = self._chip_id(chip)
        profile = self._voltage_profile(read_text(chip / "name") or "")
        device = self._device_name(chip)
        channels = [self._describe(chip, chip_id, channel, profile) for channel in self._channels(chip)]
        return ChipInfo(device, channels, now)

    def notes(self) -> list[str]:
        if self._found_kinds & {Kind.FAN, Kind.VOLTAGE}:
            return []
        return [
            "No fan or voltage sensors were found. Your motherboard's sensor driver is probably not loaded. "
            "Run `sudo sensors-detect` to find it, or try `sudo modprobe nct6775` (the chip on most ASUS, MSI "
            "and ASRock boards)."
        ]

    def close(self) -> None:
        pass

    def _channels(self, chip: Path) -> list[Channel]:
        try:
            names = os.listdir(chip)
        except OSError:
            return []
        found: dict[tuple[str, int], Channel] = {}
        for name in names:
            match = VALUE_FILE.match(name)
            if not match:
                continue
            if match.group(4):
                prefix, number = "pwm", int(match.group(5))
            else:
                prefix, number = match.group(1), int(match.group(2))
            # power1_input and power1_average describe the same channel; prefer _input.
            if (prefix, number) in found and name.endswith("_average"):
                continue
            found[(prefix, number)] = Channel(prefix, number, name)
        return sorted(found.values(), key=lambda c: (ORDER.index(c.prefix), c.number))

    def _chip_id(self, chip: Path) -> str:
        name = read_text(chip / "name") or chip.name
        device = chip / "device"
        if device.exists():
            return f"{name}@{device.resolve().name}"
        return f"{name}@{chip.name}"

    def _device_name(self, chip: Path) -> str:
        name = read_text(chip / "name") or chip.name
        device = chip / "device"
        if name in CPU_DRIVERS:
            return cpu_device_name(self.proc_root)
        if name == "nvme":
            model = read_text(device / "model")
            controller = device.resolve().name if device.exists() else ""
            return " · ".join(p for p in ("NVMe " + controller if controller else "NVMe", model) if p)
        if name == "drivetemp":
            model = read_text(device / "model")
            return f"Disk · {model}" if model else "Disk"
        if (device / "net").is_dir():
            interfaces = ", ".join(sorted(os.listdir(device / "net")))
            return f"Network · {interfaces}"
        if name.startswith("iwlwifi"):
            return "Wi-Fi adapter"
        if name.startswith("acpitz"):
            return "Motherboard · ACPI thermal zone"
        if MOTHERBOARD_DRIVERS.match(name):
            board = read_text(self.dmi_root / "board_name")
            return f"Motherboard · {board}" if board else f"Motherboard · {name}"
        if name == "amdgpu":
            return "GPU · AMD"
        if name == "nouveau":
            return "GPU · NVIDIA (nouveau)"
        if name in {"spd5118", "jc42"}:
            return "Memory module"
        return name

    def _core_name(self, chip: Path, core_id: int) -> str | None:
        """coretemp's "Core 32" uses the hardware core ID; show the same name as load and clock rows."""
        if self._core_names is None:
            self._core_names = {(c.package, c.core_id): c.name for c in discover_cores(self.sys_cpu)}
        device = (chip / "device").resolve().name  # coretemp.<package>
        package = int(device.rpartition(".")[2]) if device.rpartition(".")[2].isdigit() else 0
        return self._core_names.get((package, core_id))

    def _voltage_profile(self, name: str) -> dict[int, tuple[str, float]]:
        if not SUPER_IO_NCT679X.match(name):
            return {}
        profile = dict(NCT679X_VOLTAGES)
        vendor = read_text(self.dmi_root / "board_vendor") or ""
        if vendor.upper().startswith("ASUSTEK"):
            profile |= ASUS_NCT679X_VOLTAGES
        return profile

    def _describe(
        self, chip: Path, chip_id: str, channel: Channel, profile: dict[int, tuple[str, float]]
    ) -> ChannelInfo:
        kind, divisor, default_label = CHANNEL_TYPES[channel.prefix]
        base = f"{channel.prefix}{channel.number}"
        label = read_text(chip / f"{base}_label") or f"{default_label} {channel.number}"
        if kind is Kind.VOLTAGE and channel.number in profile:
            label, multiplier = profile[channel.number]
            divisor = divisor / multiplier
        if label.startswith("Package id "):  # coretemp
            package = label.removeprefix("Package id ")
            label = "CPU package" if package == "0" else f"CPU package {package}"
        elif core := CORETEMP_CORE.match(label):
            label = self._core_name(chip, int(core.group(1))) or label

        def limit(suffix: str) -> float | None:
            limit_raw = read_int(chip / f"{base}_{suffix}")
            return limit_raw / divisor if limit_raw is not None else None

        def plausible(temp: float | None) -> float | None:
            return temp if temp is not None and PLAUSIBLE_TEMP[0] < temp < PLAUSIBLE_TEMP[1] else None

        low = high = crit = None
        if kind is Kind.TEMPERATURE:
            high, crit = plausible(limit("max")), plausible(limit("crit"))
        elif kind is Kind.VOLTAGE:
            low, high = limit("min"), limit("max")
            # Many chips leave both at 0 (or max below min) meaning "not configured",
            # and a 0 V minimum on its own is "no minimum" (nct6798's Vcore: min 0, max 1.744).
            # Only exactly 0: a negative rail (-12 V: min -13.2, max -10.8) has a real minimum.
            if low is None or high is None or high <= low:
                low = high = None
            elif low == 0:
                low = None
        elif kind is Kind.FAN:
            fan_min = limit("min")
            low = fan_min if fan_min else None
        return ChannelInfo(
            key=f"hwmon/{chip_id}/{base}",
            label=label,
            kind=kind,
            value_file=channel.value_file,
            divisor=divisor,
            low=low,
            high=high,
            crit=crit,
            companion=f"hwmon/{chip_id}/fan{channel.number}" if kind is Kind.FAN_DUTY else None,
        )

    def _read(self, chip: Path, device: str, info: ChannelInfo) -> Reading:
        raw = read_int(chip / info.value_file)
        value = raw / info.divisor if raw is not None else None
        unused = (
            info.kind is Kind.TEMPERATURE
            and device.startswith("Motherboard")
            and value is not None
            and value in UNCONNECTED_TEMPS
        )
        return Reading(
            key=info.key,
            device=device,
            label=info.label,
            kind=info.kind,
            value=value,
            low=info.low,
            high=info.high,
            crit=info.crit,
            unused=unused,
            companion=info.companion,
        )
