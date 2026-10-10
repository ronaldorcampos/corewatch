"""Limits the hardware doesn't report, filled in from what's typical for the sensor: a load's
100 %, an ATX rail's ±5 %, a graphics card's or drive's other temperatures against its main
one's. Only for sensors that report no limit at all; shown marked as typical, never a warning."""

from collections.abc import Sequence
from dataclasses import replace

from corewatch.model import Kind, Reading

FULL = 100.0  # a load's or a fan control's top
# Named supply rails (as the board tables in sources/hwmon.py name them, matched without case or
# a trailing " Voltage") and what they should be. ATX allows each ±5 %.
RAILS = {"+12v": 12.0, "+5v": 5.0, "+3.3v": 3.3, "+3.3v standby": 3.3, "avcc": 3.3}  # AVCC: the chip's 3.3 V
RAIL_TOLERANCE = 0.05
# A device's main temperature, by its name: a graphics card's (NVIDIA, amdgpu's edge, xe's
# package) and an NVMe drive's. Its critical (else its high) holds for the device's other
# temperatures (a hotspot, memory, a drive's extra sensors), which run hotter than it by design.
MAIN_TEMPERATURES = {"GPU temperature", "Composite"}


def reports_limits(reading: Reading) -> bool:
    return any(limit is not None for limit in (reading.low, reading.high, reading.crit, reading.cap))


def with_typical(readings: Sequence[Reading]) -> list[Reading]:
    """``readings`` (one source's), each with no limit of its own given a typical one where
    there's a sensible one, by its original name (before any rename)."""
    filled = []
    for reading in readings:
        low, high = typical_limits(reading, readings) if not reports_limits(reading) else (None, None)
        if low is None and high is None:
            filled.append(reading)
        else:
            filled.append(replace(reading, typical_low=low, typical_high=high))
    return filled


def typical_limits(reading: Reading, siblings: Sequence[Reading]) -> tuple[float | None, float | None]:
    if reading.kind in (Kind.LOAD, Kind.FAN_DUTY):
        return None, FULL
    rail = RAILS.get(reading.label.casefold().removesuffix(" voltage"))
    if reading.kind is Kind.VOLTAGE and rail is not None:
        return rail * (1 - RAIL_TOLERANCE), rail * (1 + RAIL_TOLERANCE)
    if reading.kind is Kind.TEMPERATURE:
        main = next(
            (
                other
                for other in siblings
                if other.label in MAIN_TEMPERATURES
                and other.kind is Kind.TEMPERATURE
                and other.device == reading.device
                and other is not reading
            ),
            None,
        )
        if main is not None:
            return None, main.crit if main.crit is not None else main.high
    return None, None
