"""A laptop's battery: its charge, power, voltage, temperature and wear, from the kernel's power
supply class (``/sys/class/power_supply``). See
https://docs.kernel.org/power/power_supply_class.html.
"""

from pathlib import Path

from corewatch.model import Kind, Reading
from corewatch.sources.base import is_battery, read_int, read_text


def percent(part: int | None, whole: int | None) -> float | None:
    return part * 100 / whole if part is not None and whole else None


class BatterySource:
    """Every battery the machine runs on. A battery that's been taken out (``present`` 0) has
    no readings, so its card goes away until it's back."""

    name = "battery"

    def __init__(self, root: Path = Path("/sys/class/power_supply")) -> None:
        self.root = root

    def _batteries(self) -> list[Path]:
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            return []
        return [entry for entry in entries if is_battery(entry)]

    def sample(self) -> list[Reading]:
        # Named by the bays, not by what's in them, for drivers that keep an empty bay: taking out a
        # second battery mustn't rename the first one's card, which would lose where you put it.
        # ACPI removes an empty bay altogether, so there the card is renamed.
        batteries = self._batteries()
        readings = []
        for battery in batteries:
            if read_text(battery / "present") == "0":
                continue
            device = "Battery" if len(batteries) == 1 else f"Battery · {battery.name}"
            readings += self._read(battery, f"battery/{battery.name}", device)
        return readings

    def _read(self, battery: Path, key: str, device: str) -> list[Reading]:
        def has(*names: str) -> bool:
            return all((battery / name).exists() for name in names)

        def value(name: str, scale: float = 1) -> float | None:
            raw = read_int(battery / name)
            return raw / scale if raw is not None else None

        def ratio(part: str, whole: str) -> float | None:
            return percent(read_int(battery / part), read_int(battery / whole))

        readings = []

        def add(name: str, label: str, kind: Kind, reading: float | None, detail: str = "") -> None:
            readings.append(Reading(f"{key}/{name}", device, label, kind, reading, detail=detail))

        state = read_text(battery / "status") or ""  # Charging, Discharging, Full, Not charging
        cycles = value("cycle_count")
        condition = " · ".join(
            part
            for part in (
                read_text(battery / "health") or "",  # Good, Overheat, Dead ...
                # Firmware that keeps no count reports 0, or through ACPI -1.
                f"{cycles:.0f} cycle{'' if cycles == 1 else 's'}" if cycles is not None and cycles > 0 else "",
            )
            if part
        )

        # capacity is the driver's own percentage. Without it, what's left against full, in
        # energy (µWh) or, from drivers that count only charge, in charge (µAh).
        if has("capacity"):
            add("charge", "Charge", Kind.LOAD, value("capacity"), state)
        elif has("energy_now", "energy_full"):
            add("charge", "Charge", Kind.LOAD, ratio("energy_now", "energy_full"), state)
        elif has("charge_now", "charge_full"):
            add("charge", "Charge", Kind.LOAD, ratio("charge_now", "charge_full"), state)

        # Power going in or out: ACPI reports it unsigned, others negative while discharging.
        if has("power_now"):
            power = value("power_now", 1_000_000)
            add("power", "Power", Kind.POWER, abs(power) if power is not None else None)
        elif has("current_now", "voltage_now"):
            amps, volts = value("current_now", 1_000_000), value("voltage_now", 1_000_000)
            add("power", "Power", Kind.POWER, abs(amps * volts) if amps is not None and volts is not None else None)

        if has("current_now"):  # unsigned from ACPI, negative from others while discharging
            amps = value("current_now", 1_000_000)
            add("current", "Current", Kind.CURRENT, abs(amps) if amps is not None else None)
        if has("voltage_now"):
            add("voltage", "Voltage", Kind.VOLTAGE, value("voltage_now", 1_000_000))
        if has("temp"):
            add("temp", "Temperature", Kind.TEMPERATURE, value("temp", 10))  # tenths of a degree

        # What the battery holds now against when it was new (macOS's "Maximum Capacity").
        if has("energy_full", "energy_full_design"):
            add("health", "Health", Kind.LOAD, ratio("energy_full", "energy_full_design"), condition)
        elif has("charge_full", "charge_full_design"):
            add("health", "Health", Kind.LOAD, ratio("charge_full", "charge_full_design"), condition)
        return readings

    def notes(self) -> list[str]:
        return []

    def close(self) -> None:
        pass
