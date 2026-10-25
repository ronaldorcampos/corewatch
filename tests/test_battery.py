from pathlib import Path

from conftest import write_tree

from corewatch.model import Kind
from corewatch.sources.battery import BatterySource

# What an M1 Pro's SMC battery publishes (charge in µAh, power negative while discharging).
APPLE = {
    "type": "Battery",
    "scope": "System",
    "present": "1",
    "status": "Discharging",
    "capacity": "80",
    "charge_now": "5460000",
    "charge_full": "7105000",
    "charge_full_design": "8694000",
    "energy_full": "80997000",
    "energy_full_design": "99111600",
    "power_now": "-14646000",
    "current_now": "-1233000",
    "voltage_now": "11879000",
    "temp": "263",
}
# A battery counting in energy (µWh) with power unsigned, as ACPI reports it, from a driver that
# gives no percentage of its own, so the charge comes from energy now against full.
ACPI = {
    "type": "Battery",
    "present": "1",
    "status": "Discharging",
    "energy_now": "30000000",
    "energy_full": "40000000",
    "energy_full_design": "50000000",
    "power_now": "8500000",
    "voltage_now": "12100000",
}


def supplies(tmp_path: Path, batteries: dict[str, dict[str, str]]) -> Path:
    root = tmp_path / "power_supply"
    write_tree(root, {f"{name}/{file}": value for name, files in batteries.items() for file, value in files.items()})
    return root


def readings(root: Path) -> dict[str, tuple[str, str, Kind, float | None]]:
    return {r.key: (r.device, r.label, r.kind, r.value) for r in BatterySource(root=root).sample()}


def test_an_apple_battery(tmp_path: Path) -> None:
    root = supplies(tmp_path, {"macsmc-battery": APPLE, "macsmc-ac": {"type": "Mains", "online": "0"}})
    found = readings(root)
    assert found == {
        "battery/macsmc-battery/charge": ("Battery", "Charge", Kind.LOAD, 80.0),
        "battery/macsmc-battery/power": ("Battery", "Power", Kind.POWER, 14.646),  # out of the battery
        "battery/macsmc-battery/current": ("Battery", "Current", Kind.CURRENT, 1.233),
        "battery/macsmc-battery/voltage": ("Battery", "Voltage", Kind.VOLTAGE, 11.879),
        "battery/macsmc-battery/temp": ("Battery", "Temperature", Kind.TEMPERATURE, 26.3),
        "battery/macsmc-battery/health": ("Battery", "Health", Kind.LOAD, 80997000 * 100 / 99111600),
    }


def test_an_acpi_battery_without_capacity(tmp_path: Path) -> None:
    found = readings(supplies(tmp_path, {"BAT0": ACPI}))
    assert {key.rpartition("/")[2]: value[3] for key, value in found.items()} == {
        "charge": 75.0,  # energy now against full
        "power": 8.5,
        "voltage": 12.1,
        "health": 80.0,
    }


def test_charge_from_charge_counters_and_power_from_current_and_voltage(tmp_path: Path) -> None:
    battery = {
        "type": "Battery",
        "charge_now": "2000000",
        "charge_full": "4000000",
        "charge_full_design": "5000000",
        "current_now": "1500000",  # charging: positive
        "voltage_now": "8000000",
    }
    found = readings(supplies(tmp_path, {"BAT1": battery}))
    assert {key.rpartition("/")[2]: value[3] for key, value in found.items()} == {
        "charge": 50.0,
        "power": 12.0,
        "current": 1.5,
        "voltage": 8.0,
        "health": 80.0,
    }
    discharging = {**battery, "current_now": "-1500000"}  # others count discharging as negative
    found = readings(supplies(tmp_path / "out", {"BAT1": discharging}))
    assert (found["battery/BAT1/power"][3], found["battery/BAT1/current"][3]) == (12.0, 1.5)


def test_an_unknown_cycle_count_is_left_out(tmp_path: Path) -> None:
    for unknown in ("-1", "0"):
        found = BatterySource(root=supplies(tmp_path / unknown, {"BAT0": {**APPLE, "cycle_count": unknown}})).sample()
        assert next(r.detail for r in found if r.label == "Health") == ""
    found = BatterySource(root=supplies(tmp_path / "one", {"BAT0": {**APPLE, "cycle_count": "1"}})).sample()
    assert next(r.detail for r in found if r.label == "Health") == "1 cycle"


def test_an_unreadable_power_is_none(tmp_path: Path) -> None:
    found = readings(supplies(tmp_path, {"BAT0": {**ACPI, "power_now": "?"}}))
    assert found["battery/BAT0/power"][3] is None


def test_an_unreadable_value_is_none_and_a_zero_full_charge_is_unknown(tmp_path: Path) -> None:
    battery = {"type": "Battery", "energy_now": "1", "energy_full": "0", "energy_full_design": "0", "temp": "?"}
    found = readings(supplies(tmp_path, {"BAT0": battery}))
    assert {key.rpartition("/")[2]: value[3] for key, value in found.items()} == {
        "charge": None,
        "temp": None,
        "health": None,
    }


def test_only_the_machines_own_batteries_that_are_in(tmp_path: Path) -> None:
    root = supplies(
        tmp_path,
        {
            "BAT0": ACPI,
            "BAT1": {**ACPI, "present": "0"},  # the second bay is empty: no readings, but named as a bay
            "hidpp_battery_0": {"type": "Battery", "scope": "Device", "capacity": "60"},  # a mouse
            "AC": {"type": "Mains", "online": "1"},
            "ucsi-source-psy-USBC000:001": {"type": "USB", "online": "0"},
        },
    )
    assert {device for device, *_ in readings(root).values()} == {"Battery · BAT0"}


def test_two_batteries_are_told_apart(tmp_path: Path) -> None:
    root = supplies(tmp_path, {"BAT0": ACPI, "BAT1": ACPI})
    assert {device for device, *_ in readings(root).values()} == {"Battery · BAT0", "Battery · BAT1"}
    write_tree(root, {"BAT1/present": "0"})  # the second one taken out: the first keeps its name
    assert {device for device, *_ in readings(root).values()} == {"Battery · BAT0"}


def test_a_battery_type_outside_the_power_supply_class_is_not_a_battery(tmp_path: Path) -> None:
    from corewatch.sources.base import is_battery

    write_tree(tmp_path, {"elsewhere/BAT0/type": "Battery", "power_supply/BAT0/type": "Battery"})
    assert not is_battery(tmp_path / "elsewhere" / "BAT0")
    assert is_battery(tmp_path / "power_supply" / "BAT0")


def test_no_power_supply_class_means_no_battery(tmp_path: Path) -> None:
    assert BatterySource(root=tmp_path / "missing").sample() == []
