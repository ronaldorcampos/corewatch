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

# amdgpu's driver labels (drivers/gpu/drm/amd/pm/amdgpu_pm.c), named like the NVIDIA card's rows.
AMDGPU_LABELS = {
    "edge": "GPU temperature",
    "junction": "Hotspot temperature",
    "mem": "Memory temperature",
    "vddgfx": "Core voltage",
    "vddnb": "SoC voltage",
    "sclk": "Graphics clock",
    "mclk": "Memory clock",
    "vddboard": "Board voltage",
    "PPT": "Power draw",
    "slowPPT": "Power draw",  # the Steam Deck's name for the same reading
}
# Intel's GPU drivers. Only discrete cards (Arc) get a hwmon chip; integrated graphics has none.
INTEL_GPU_DRIVERS = {"i915", "xe"}
GPU_DRIVERS = {"amdgpu", "radeon", "nouveau", *INTEL_GPU_DRIVERS}
# Drivers whose fan control reads the setting last asked for, not what the fan gets: thinkpad_acpi
# reports 100 % in its default automatic mode while the firmware has stopped a cool laptop's fan
# (Documentation/admin-guide/laptops/thinkpad-acpi.rst). Never used to tell a stalled fan.
SETPOINT_DRIVERS = {"thinkpad"}
# Labels xe gives its sensors (drivers/gpu/drm/xe/xe_hwmon.c); i915 labels none of them.
INTEL_GPU_TEMPERATURES = {
    "pkg": "GPU temperature",
    "vram": "Memory temperature",
    "mctrl": "Memory controller temperature",
    "pcie": "PCIe link temperature",
}
INTEL_GPU_POWER = {"card": "Power draw", "pkg": "GPU chip power"}
INTEL_VRAM_CHANNEL = re.compile(r"^vram_ch_(\d+)$")
INTEL_VENDOR = "0x8086"
# Intel always gives its integrated graphics this PCI slot; a discrete Arc card sits elsewhere.
INTEL_INTEGRATED_SLOT = "0000:00:02.0"
# zenpower on multi-socket systems prefixes every label: "cpu0 Tdie", "cpu1 SVI2_Core".
SOCKET_PREFIX = re.compile(r"^cpu(\d+) (.+)$")
# zenpower (an out-of-tree driver for Zen 1-3) reports the CPU's own voltage regulators.
ZENPOWER_LABELS = {
    "SVI2_Core": "Core voltage",
    "SVI2_SoC": "SoC voltage",
    "SVI2_C_Core": "Core current",
    "SVI2_C_SoC": "SoC current",
    "SVI2_P_Core": "Core power",
    "SVI2_P_SoC": "SoC power",
}
AMD_CPU_DRIVERS = {"k10temp", "zenpower"}
CCD_LABEL = re.compile(r"^Tccd(\d+)$")
PCI_IDS = (Path("/usr/share/hwdata/pci.ids"), Path("/usr/share/misc/pci.ids"))
_pci_names: dict[tuple[str, ...], str | None] = {}


def pci_name(vendor: str, device: str, databases: tuple[Path, ...] = PCI_IDS) -> str | None:
    """A PCI device's marketing name from the system's pci.ids, e.g. "Radeon RX 7900 XT/7900
    XTX" for 1002:744c (the part in brackets, when there is one). Looked up once per device."""
    wanted = (vendor.lower().removeprefix("0x"), device.lower().removeprefix("0x"))
    cache_key = (*wanted, *map(str, databases))
    if cache_key in _pci_names:
        return _pci_names[cache_key]
    name = None
    for database in databases:
        try:
            with database.open(encoding="utf-8", errors="replace") as file:
                in_vendor = False
                for line in file:
                    if line.startswith("#") or not line.strip():  # comments sit inside vendor blocks too
                        continue
                    if not line.startswith("\t"):
                        in_vendor = line[:4].lower() == wanted[0]
                    elif in_vendor and not line.startswith("\t\t") and line[1:5].lower() == wanted[1]:
                        name = line[5:].strip()
                        break
        except OSError:
            continue
        if name is not None:
            break
    if name is not None and (bracket := re.search(r"\[(.+)\]", name)):
        name = bracket.group(1)
    _pci_names[cache_key] = name
    return name


def intel_gpu_name(pci: Path, databases: tuple[Path, ...] = PCI_IDS) -> str:
    """One Intel GPU's card name, e.g. "GPU · Intel Arc A770". The hwmon chip and the Intel
    source both use it, so a discrete card's readings from either land on the same card."""
    # Newer pci.ids entries already say "Intel Arc Graphics"; don't make it "Intel Intel".
    model = (pci_name(INTEL_VENDOR, read_text(pci / "device") or "", databases) or "").removeprefix("Intel ")
    if model:
        return f"GPU · Intel {model}"
    integrated = pci.resolve().name == INTEL_INTEGRATED_SLOT
    return "GPU · Intel integrated graphics" if integrated else "GPU · Intel graphics card"


def unused_gpu(pci: Path) -> bool:
    """Whether nothing is using a GPU: it's powered down, or nobody holds it awake (a program
    drawing or encoding on it, a lit display) and it's only waiting to power down. These two
    files never wake it, while reading most of an Intel GPU's own files does, and each read
    while it's awake restarts its countdown to powering down: read every sample, they would
    keep it awake for good. When something is using it, it's awake anyway and they're free.
    Unknown (no runtime power management) counts as in use."""
    power = pci / "power"
    return read_text(power / "runtime_status") == "suspended" or read_int(power / "runtime_usage") == 0


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
    cap: float | None = None
    # When set, the value is value_file as a percentage of this file (VRAM used of total).
    ratio_of: str | None = None
    empty_if_idle: bool = False
    integrated: bool = False  # an APU's graphics (see Reading.integrated)
    setpoint: bool = False  # a fan control that reads what was asked for (see Reading.setpoint)


@dataclass(frozen=True)
class ChipContext:
    """What a channel's naming may depend on, beyond its own files."""

    name: str = ""
    gpu: bool = False
    has_tdie: bool = False
    is_apu: bool = False


@dataclass(frozen=True)
class ChipInfo:
    device: str
    channels: list[ChannelInfo]
    read_at: float
    # Left alone while nothing uses the GPU: reading it would keep it awake (Intel's GPU drivers).
    sleeps: bool = False


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
        pci_ids: tuple[Path, ...] = PCI_IDS,
    ) -> None:
        self.root = root
        self.pci_ids = pci_ids
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
            asleep = info is not None and info.sleeps and unused_gpu(chip / "device")
            if info is None or (now - info.read_at >= METADATA_SECONDS and not asleep):
                info = self._chips[chip] = self._describe_chip(chip, now)
            if not info.channels:
                continue
            used_names[info.device] = used_names.get(info.device, 0) + 1
            device = info.device if used_names[info.device] == 1 else f"{info.device} ({used_names[info.device]})"
            for channel in info.channels:
                readings.append(self._read(chip, device, channel, asleep))
        self._found_kinds = {r.kind for r in readings}
        return readings

    def _describe_chip(self, chip: Path, now: float) -> ChipInfo:
        chip_id = self._chip_id(chip)
        name = read_text(chip / "name") or ""
        profile = self._voltage_profile(name)
        device = self._device_name(chip)
        found = self._channels(chip)
        labels = {read_text(chip / f"{c.prefix}{c.number}_label") for c in found}
        context = ChipContext(
            name=name,
            gpu=name in GPU_DRIVERS,
            has_tdie=any(label is not None and label.endswith("Tdie") for label in labels),
            is_apu=name == "amdgpu" and "vddnb" in labels,  # only APUs report the northbridge
        )
        channels = [self._describe(chip, chip_id, channel, profile, context) for channel in found]
        if name == "amdgpu":
            channels += self._amdgpu_extras(chip, chip_id, context.is_apu)
        return ChipInfo(device, channels, now, sleeps=name in INTEL_GPU_DRIVERS)

    def _amdgpu_extras(self, chip: Path, chip_id: str, integrated: bool) -> list[ChannelInfo]:
        """GPU load and VRAM use, which amdgpu publishes next to (not inside) its hwmon folder."""
        extras = []
        if (chip / "device" / "gpu_busy_percent").exists():
            extras.append(
                ChannelInfo(
                    key=f"hwmon/{chip_id}/gpu_busy",
                    label="GPU load",
                    kind=Kind.LOAD,
                    value_file="device/gpu_busy_percent",
                    divisor=1,
                    low=None,
                    high=None,
                    crit=None,
                    companion=None,
                    integrated=integrated,
                )
            )
        if (chip / "device" / "mem_info_vram_used").exists() and (chip / "device" / "mem_info_vram_total").exists():
            extras.append(
                ChannelInfo(
                    key=f"hwmon/{chip_id}/vram",
                    label="Video memory used",
                    kind=Kind.LOAD,
                    value_file="device/mem_info_vram_used",
                    divisor=1,
                    low=None,
                    high=None,
                    crit=None,
                    companion=None,
                    ratio_of="device/mem_info_vram_total",
                    integrated=integrated,
                )
            )
        return extras

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
            model = read_text(device / "product_name") or pci_name(
                read_text(device / "vendor") or "", read_text(device / "device") or "", self.pci_ids
            )
            model = (model or "").removeprefix("AMD ")  # "AMD Custom GPU 0405" must not read "AMD AMD"
            return f"GPU · AMD {model}" if model else "GPU · AMD"
        if name == "nouveau":
            return "GPU · NVIDIA (nouveau)"
        if name in INTEL_GPU_DRIVERS:
            return intel_gpu_name(device, self.pci_ids)
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
        self,
        chip: Path,
        chip_id: str,
        channel: Channel,
        profile: dict[int, tuple[str, float]],
        context: ChipContext | None = None,
    ) -> ChannelInfo:
        context = context or ChipContext()
        kind, divisor, default_label = CHANNEL_TYPES[channel.prefix]
        base = f"{channel.prefix}{channel.number}"
        label = read_text(chip / f"{base}_label") or f"{default_label} {channel.number}"
        label = self._friendly_label(label, kind, channel, context)
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

        low = high = crit = cap = None
        if kind is Kind.TEMPERATURE:
            high, crit = plausible(limit("max")), plausible(limit("crit"))
            emergency = plausible(limit("emergency"))
            if emergency is not None and high is None:
                # amdgpu has no _max: its _crit is where the GPU starts throttling (a warning) and
                # _emergency where it shuts down. Chips that also have _max keep their own meaning.
                high, crit = crit, emergency
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
        elif kind is Kind.POWER:
            power_cap = limit("cap")
            if not power_cap and context.name in INTEL_GPU_DRIVERS:
                power_cap = limit("max")  # Intel's sustained power limit, its equivalent
            cap = power_cap if power_cap else None  # shown for reference, never a warning
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
            cap=cap,
            # Any fan header may have nothing plugged in, except a graphics card's own fans,
            # which exist even while resting at 0 RPM.
            empty_if_idle=kind is Kind.FAN and not context.gpu,
            integrated=context.is_apu,
            setpoint=kind is Kind.FAN_DUTY and context.name in SETPOINT_DRIVERS,
        )

    def _friendly_label(self, label: str, kind: Kind, channel: Channel, context: ChipContext) -> str:
        """Readable names for the drivers whose own labels are terse or internal."""
        if context.name == "amdgpu":
            if kind is Kind.POWER and context.is_apu and label in ("PPT", "slowPPT"):
                return "Power draw (CPU and GPU)"  # on an APU the driver reports the whole chip
            if kind is Kind.FAN_DUTY:
                return f"Fan {channel.number} speed"  # like the NVIDIA card's percentage rows
            return AMDGPU_LABELS.get(label, label)
        if context.name in INTEL_GPU_DRIVERS:
            if kind is Kind.TEMPERATURE:
                if vram := INTEL_VRAM_CHANNEL.match(label):
                    return f"Memory channel {vram.group(1)} temperature"
                return INTEL_GPU_TEMPERATURES.get(label, label)
            if kind is Kind.POWER:  # i915 labels nothing: its one power reading is the card's
                return INTEL_GPU_POWER.get(label, "Power draw" if label.startswith("Power ") else label)
            if kind is Kind.VOLTAGE:
                return "Core voltage"  # the GPU's one voltage reading (i915's in0, xe's in1)
            return label
        if context.name in AMD_CPU_DRIVERS:
            if socket := SOCKET_PREFIX.match(label):
                inner = self._friendly_label(socket.group(2), kind, channel, context)
                return f"{inner} (CPU {socket.group(1)})"
            if label == "Tdie":
                return "CPU temperature"  # the real die temperature
            if label == "Tctl":
                # Tctl is the value fans are driven by; some chips report it with an offset, and
                # then Tdie (when there) is the real temperature.
                return "CPU temperature (fan control)" if context.has_tdie else "CPU temperature"
            if ccd := CCD_LABEL.match(label):
                return f"CCD {ccd.group(1)}"  # one per chiplet; AMD reports no per-core temperatures
            return ZENPOWER_LABELS.get(label, label)
        return label

    def _read(self, chip: Path, device: str, info: ChannelInfo, asleep: bool = False) -> Reading:
        raw = None if asleep else read_int(chip / info.value_file)
        value = raw / info.divisor if raw is not None else None
        if info.ratio_of is not None:
            total = read_int(chip / info.ratio_of)
            value = raw * 100 / total if raw is not None and total else None
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
            empty_if_idle=info.empty_if_idle,
            cap=info.cap,
            integrated=info.integrated,
            setpoint=info.setpoint,
        )
