import shutil
from pathlib import Path

from conftest import write_tree

from corewatch.model import Kind
from corewatch.sources.hwmon import HwmonSource
from corewatch.sources.intel_gpu import IntelGpuSource, find_integrated_gpu, find_intel_gpus
from corewatch.sources.rapl import RaplSource

I915_GT0 = {
    "gt/gt0/rps_act_freq_mhz": "0",
    "gt/gt0/rps_RP0_freq_mhz": "1600",
    "gt/gt0/rc6_residency_ms": "10000",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def pci_ids(tmp_path: Path) -> tuple[Path, ...]:
    database = tmp_path / "pci.ids"
    database.write_text(
        "# comment\n8086  Intel Corporation\n\ta780  Raptor Lake-S GT1 [UHD Graphics 770]\n"
        "\t56a0  DG2 [Arc A770]\n\te20b  Battlemage G21 [Arc B580]\n\t7d55  Meteor Lake-P [Intel Arc Graphics]\n"
        "10de  NVIDIA Corporation\n\t2786  AD104 [GeForce RTX 4070]\n"
    )
    return (database,)


def intel_card(
    tmp_path: Path,
    card_files: dict[str, str],
    driver: str = "i915",
    slot: str = "0000:00:02.0",
    vendor: str = "0x8086",
    device_files: dict[str, str] | None = None,
    name: str = "card1",
    device_id: str = "0xa780",
    hwmon: tuple[str, dict[str, str]] | None = None,
) -> Path:
    """A fake /sys/class/drm with a card, linked to its PCI device and driver like sysfs does.
    Call again with another name to add a second card. ``hwmon`` is (hwmonN, files) for a
    discrete card's chip, also linked from a fake /sys/class/hwmon (tmp_path / "hwmon")."""
    drm = tmp_path / "drm"
    pci = tmp_path / "devices" / slot
    write_tree(pci, {"vendor": vendor, "device": device_id, **(device_files or {})})
    drivers = tmp_path / "drivers" / driver
    drivers.mkdir(parents=True, exist_ok=True)
    (pci / "driver").symlink_to(drivers)
    card = drm / name
    card.mkdir(parents=True)
    (card / "device").symlink_to(pci)
    write_tree(card, card_files)
    (drm / f"{name}-HDMI-A-1").mkdir()  # a connector, not a card
    if hwmon is not None:
        chip_name, files = hwmon
        chip = pci / "hwmon" / chip_name
        write_tree(chip, {"name": driver, **files})
        (chip / "device").symlink_to(pci)
        (tmp_path / "hwmon").mkdir(exist_ok=True)
        (tmp_path / "hwmon" / chip_name).symlink_to(chip)
    return drm


def test_i915_clock_and_active_time(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0)
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock.now += 2.0
    # 0.5 s asleep out of 2 s: awake three quarters of the time.
    write_tree(drm / "card1", {"gt/gt0/rps_act_freq_mhz": "1200", "gt/gt0/rc6_residency_ms": "10500"})
    clock_reading, active = source.sample()
    assert clock_reading.device == active.device == "GPU · Intel UHD Graphics 770"
    assert (clock_reading.key, clock_reading.label, clock_reading.kind) == (
        "intel-gpu/0000:00:02.0/clock",
        "Graphics clock",
        Kind.CLOCK,
    )
    assert clock_reading.value == 1200 and clock_reading.cap == 1600.0  # the max clock is a reference, not a warning
    assert clock_reading.high is None and clock_reading.crit is None
    assert (active.key, active.label, active.kind) == ("intel-gpu/0000:00:02.0/active", "Active time", Kind.LOAD)
    assert active.value == 75.0

    clock.now += 1.0
    write_tree(drm / "card1", {"gt/gt0/rps_act_freq_mhz": "0", "gt/gt0/rc6_residency_ms": "11500"})
    assert [r.value for r in source.sample()] == [0, 0.0]  # asleep the whole second: 0 MHz, 0 %

    clock.now += 1.0
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "11600"})
    assert source.sample()[1].value == 90.0


def test_active_time_never_goes_below_zero_or_survives_a_counter_reset(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0)
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock.now += 1.0
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "11010"})  # counters tick separately: 1.01 s asleep in 1 s
    assert source.sample()[1].value == 0.0
    clock.now += 1.0
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "5"})  # the driver reset it (reload, suspend)
    assert source.sample()[1].value is None
    clock.now += 1.0
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "505"})
    assert source.sample()[1].value == 50.0  # and it picks up again from there
    (drm / "card1" / "gt" / "gt0" / "rc6_residency_ms").unlink()
    clock.now += 1.0
    assert source.sample()[1].value is None


def test_first_sample_has_no_active_time_yet(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0)
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=FakeClock())
    source._previous.clear()  # as if __init__ hadn't primed the counters
    assert source.sample()[1].value is None


def test_older_i915_without_a_per_gt_folder_uses_the_card_files(tmp_path: Path) -> None:
    drm = intel_card(
        tmp_path,
        {"gt_act_freq_mhz": "300", "gt_RP0_freq_mhz": "1450", "power/rc6_residency_ms": "0"},
    )
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock.now += 1.0
    write_tree(drm / "card1", {"power/rc6_residency_ms": "250"})
    clock_reading, active = source.sample()
    assert (clock_reading.value, clock_reading.cap, active.value) == (300, 1450.0, 75.0)


def test_xe_reads_its_own_layout(tmp_path: Path) -> None:
    drm = intel_card(
        tmp_path,
        {},
        driver="xe",
        device_files={
            "tile0/gt0/freq0/act_freq": "900",
            "tile0/gt0/freq0/rp0_freq": "2050",
            "tile0/gt0/gtidle/idle_residency_ms": "1000",
        },
    )
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock.now += 1.0
    write_tree(tmp_path / "devices" / "0000:00:02.0", {"tile0/gt0/gtidle/idle_residency_ms": "1400"})
    clock_reading, active = source.sample()
    assert (clock_reading.value, clock_reading.cap, active.value) == (900, 2050.0, 60.0)


def test_only_intel_gpus_on_intels_drivers_count(tmp_path: Path) -> None:
    ids = pci_ids(tmp_path)
    arc = intel_card(tmp_path / "arc", I915_GT0, slot="0000:03:00.0", device_id="0x56a0")  # a discrete Arc card
    [gpu] = find_intel_gpus(arc, ids)
    assert not gpu.integrated and gpu.device == "GPU · Intel Arc A770"
    assert find_integrated_gpu(arc, ids) is None  # so RAPL's integrated graphics power stays with the CPU
    other = intel_card(tmp_path / "other", I915_GT0, vendor="0x1002")  # not Intel, even in that slot
    assert find_intel_gpus(other, ids) == []
    nouveau = intel_card(tmp_path / "nouveau", I915_GT0, driver="nouveau")
    assert find_intel_gpus(nouveau, ids) == []
    assert find_integrated_gpu(tmp_path / "missing", ids) is None
    assert IntelGpuSource(tmp_path / "missing", ids).sample() == []


def test_an_unknown_model_still_gets_a_name(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0)
    gpu = find_integrated_gpu(drm, (tmp_path / "no-such-pci.ids",))
    assert gpu is not None and gpu.device == "GPU · Intel integrated graphics"
    arc = intel_card(tmp_path / "arc", I915_GT0, slot="0000:03:00.0", device_id="0x56a0")
    [card] = find_intel_gpus(arc, (tmp_path / "no-such-pci.ids",))
    assert card.device == "GPU · Intel graphics card"


def rapl_tree(root: Path) -> None:
    write_tree(
        root,
        {
            "intel-rapl:0/name": "package-0",
            "intel-rapl:0/energy_uj": "0",
            "intel-rapl:0:1/name": "uncore",
            "intel-rapl:0:1/energy_uj": "0",
        },
    )


def test_rapl_files_integrated_graphics_power_under_the_gpu_card(tmp_path: Path, proc_root: Path) -> None:
    rapl_tree(tmp_path / "powercap")
    drm = intel_card(tmp_path, I915_GT0)
    clock = FakeClock()
    source = RaplSource(tmp_path / "powercap", proc_root, clock=clock, drm_root=drm, pci_ids=pci_ids(tmp_path))
    clock.now += 1.0
    write_tree(tmp_path / "powercap", {"intel-rapl:0:1/energy_uj": "2000000"})
    package, graphics = source.sample()
    assert (package.device, package.label) == ("CPU · Intel Core i7-13700K", "Package power")
    assert (graphics.key, graphics.device, graphics.label) == (
        "rapl/intel-rapl:0:1",  # unchanged, so pins and your own names carry over
        "GPU · Intel UHD Graphics 770",
        "Power draw",
    )
    assert graphics.value == 2.0


def test_rapl_keeps_it_with_the_cpu_when_the_igpu_is_off(tmp_path: Path, proc_root: Path) -> None:
    rapl_tree(tmp_path / "powercap")
    source = RaplSource(tmp_path / "powercap", proc_root, drm_root=tmp_path / "no-drm", pci_ids=pci_ids(tmp_path))
    _, graphics = source.sample()
    assert (graphics.device, graphics.label) == ("CPU · Intel Core i7-13700K", "Integrated graphics power")


def test_discrete_gpus_are_listed_before_the_integrated_one(tmp_path: Path, proc_root: Path) -> None:
    from corewatch.model import Row
    from corewatch.monitor import group_rows
    from corewatch.sources import DEFAULT_FACTORIES, IntelGpuSource, NvidiaSource, RaplSource

    order = DEFAULT_FACTORIES.index
    assert order(NvidiaSource) < order(IntelGpuSource) < order(RaplSource)
    # The hardest case: an Arc card with no hwmon chip (i915 before Linux 6.2), so its first
    # reading comes from the Intel source, next to an integrated GPU whose power RAPL reports.
    intel_card(tmp_path, I915_GT0, name="card0")
    drm = intel_card(tmp_path, I915_GT0, slot="0000:03:00.0", device_id="0x56a0", name="card1")
    rapl_tree(tmp_path / "powercap")
    ids = pci_ids(tmp_path)
    sources = [
        RaplSource(tmp_path / "powercap", proc_root, drm_root=drm, pci_ids=ids),
        IntelGpuSource(drm, ids),
    ]
    sources.sort(key=lambda source: order(type(source)))  # the order corewatch runs them in
    rows = [Row(reading) for source in sources for reading in source.sample()]
    assert [device for device, _ in group_rows(rows) if device.startswith("GPU")] == [
        "GPU · Intel Arc A770",
        "GPU · Intel UHD Graphics 770",
    ]


ARC_I915 = {
    "in0_input": "680",  # mV
    "energy1_input": "5000000",  # µJ
    "power1_max": "190000000",  # µW: the card's sustained power limit
    "power1_crit": "400000000",
    "curr1_crit": "300000",
    "fan1_input": "0",
}


def test_an_arc_card_on_i915_gets_power_from_its_energy_counter(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0, slot="0000:03:00.0", device_id="0x56a0", hwmon=("hwmon7", ARC_I915))
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock.now += 2.0
    write_tree(tmp_path / "devices/0000:03:00.0/hwmon/hwmon7", {"energy1_input": "105000000"})  # +100 J in 2 s
    clock_reading, _, power = source.sample()
    assert power.device == clock_reading.device == "GPU · Intel Arc A770"
    assert (power.key, power.label, power.kind) == ("intel-gpu/0000:03:00.0/power1", "Power draw", Kind.POWER)
    assert power.value == 50.0 and power.cap == 190.0  # the limit is a reference line, never a warning
    assert power.high is None and power.crit is None

    clock.now += 1.0
    write_tree(tmp_path / "devices/0000:03:00.0/hwmon/hwmon7", {"energy1_input": "5"})  # reset: reload or suspend
    assert source.sample()[2].value is None
    clock.now += 1.0
    write_tree(tmp_path / "devices/0000:03:00.0/hwmon/hwmon7", {"energy1_input": "3000000005"})  # 3 kJ in 1 s
    assert source.sample()[2].value is None  # an impossible spike, not a reading


def test_an_arc_cards_hwmon_chip_lands_on_the_same_card(tmp_path: Path) -> None:
    intel_card(tmp_path, I915_GT0, slot="0000:03:00.0", device_id="0x56a0", hwmon=("hwmon7", ARC_I915))
    readings = HwmonSource(root=tmp_path / "hwmon", pci_ids=pci_ids(tmp_path)).sample()
    by_label = {r.label: r for r in readings}
    assert set(by_label) == {"Core voltage", "Fan 1"}  # power is the Intel source's, from the energy counter
    assert {r.device for r in readings} == {"GPU · Intel Arc A770"}
    assert by_label["Core voltage"].value == 0.68
    assert not by_label["Fan 1"].empty_if_idle  # a graphics card's fan resting at 0 RPM stays listed


def test_an_arc_card_on_xe_with_its_labelled_sensors(tmp_path: Path) -> None:
    xe_gt = {
        "tile0/gt0/freq0/act_freq": "2850",
        "tile0/gt0/freq0/rp0_freq": "2850",
        "tile0/gt0/gtidle/idle_residency_ms": "0",
    }
    chip = {
        "energy1_input": "0",
        "energy1_label": "card",
        "energy2_input": "0",
        "energy2_label": "pkg",
        "power1_max": "190000000",
        "power1_label": "card",
        "power2_max": "150000000",
        "power2_label": "pkg",
        "in1_input": "1010",
        "temp2_input": "55000",
        "temp2_label": "pkg",
        "temp3_input": "60000",
        "temp3_label": "vram",
        "temp4_input": "50000",
        "temp4_label": "mctrl",
        "temp5_input": "48000",
        "temp5_label": "pcie",
        "temp6_input": "61000",
        "temp6_label": "vram_ch_0",
        "fan1_input": "1500",
        "fan2_input": "1480",
        "fan3_input": "0",
    }
    drm = intel_card(
        tmp_path, {}, driver="xe", slot="0000:03:00.0", device_id="0xe20b", device_files=xe_gt, hwmon=("hwmon4", chip)
    )
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock.now += 1.0
    write_tree(
        tmp_path / "devices/0000:03:00.0/hwmon/hwmon4", {"energy1_input": "120000000", "energy2_input": "90000000"}
    )
    readings = {r.label: r for r in source.sample()}
    assert readings["Graphics clock"].value == 2850 and readings["Active time"].value == 100.0
    assert (readings["Power draw"].value, readings["Power draw"].cap) == (120.0, 190.0)  # the whole card
    assert (readings["GPU chip power"].value, readings["GPU chip power"].cap) == (90.0, 150.0)
    assert {r.device for r in readings.values()} == {"GPU · Intel Arc B580"}

    hwmon = {r.label: r for r in HwmonSource(root=tmp_path / "hwmon", pci_ids=pci_ids(tmp_path)).sample()}
    assert {r.device for r in hwmon.values()} == {"GPU · Intel Arc B580"}
    assert {label: r.value for label, r in hwmon.items()} == {
        "Core voltage": 1.01,
        "GPU temperature": 55.0,
        "Memory temperature": 60.0,
        "Memory controller temperature": 50.0,
        "PCIe link temperature": 48.0,
        "Memory channel 0 temperature": 61.0,
        "Fan 1": 1500,
        "Fan 2": 1480,
        "Fan 3": 0,
    }


def test_a_card_reporting_power_directly_isnt_counted_twice(tmp_path: Path) -> None:
    chip = {**ARC_I915, "power1_input": "45000000"}
    drm = intel_card(tmp_path, I915_GT0, slot="0000:03:00.0", device_id="0x56a0", hwmon=("hwmon7", chip))
    assert [r.label for r in IntelGpuSource(drm, pci_ids(tmp_path)).sample()] == ["Graphics clock", "Active time"]
    [power] = [
        r for r in HwmonSource(root=tmp_path / "hwmon", pci_ids=pci_ids(tmp_path)).sample() if r.kind is Kind.POWER
    ]
    # i915 labels nothing; its one power reading is the card's, named like the Intel source's.
    assert (power.label, power.value, power.cap) == ("Power draw", 45.0, 190.0)


def test_twin_arc_cards_keep_their_readings_apart(tmp_path: Path) -> None:
    # Two of the same model: the hwmon source calls the second chip it meets (by hwmonN) "(2)",
    # and the Intel source must call the same card "(2)" too, whatever order the cards come in.
    intel_card(tmp_path, I915_GT0, slot="0000:03:00.0", device_id="0x56a0", hwmon=("hwmon9", ARC_I915), name="card1")
    drm = intel_card(
        tmp_path, I915_GT0, slot="0000:07:00.0", device_id="0x56a0", hwmon=("hwmon2", ARC_I915), name="card2"
    )
    ids = pci_ids(tmp_path)
    by_slot = {gpu.slot: gpu.device for gpu in find_intel_gpus(drm, ids)}
    assert by_slot == {"0000:07:00.0": "GPU · Intel Arc A770", "0000:03:00.0": "GPU · Intel Arc A770 (2)"}
    hwmon = HwmonSource(root=tmp_path / "hwmon", pci_ids=ids).sample()
    by_chip = {r.key.split("@")[1].split("/")[0]: r.device for r in hwmon}
    assert by_chip == {"0000:07:00.0": "GPU · Intel Arc A770", "0000:03:00.0": "GPU · Intel Arc A770 (2)"}


def test_an_igpu_next_to_an_arc_card(tmp_path: Path, proc_root: Path) -> None:
    intel_card(tmp_path, I915_GT0, name="card0")
    drm = intel_card(
        tmp_path, I915_GT0, slot="0000:03:00.0", device_id="0x56a0", hwmon=("hwmon7", ARC_I915), name="card1"
    )
    ids = pci_ids(tmp_path)
    devices = {gpu.slot: (gpu.device, gpu.integrated) for gpu in find_intel_gpus(drm, ids)}
    assert devices == {
        "0000:00:02.0": ("GPU · Intel UHD Graphics 770", True),
        "0000:03:00.0": ("GPU · Intel Arc A770", False),
    }
    rapl_tree(tmp_path / "powercap")
    _, graphics = RaplSource(tmp_path / "powercap", proc_root, drm_root=drm, pci_ids=ids).sample()
    assert graphics.device == "GPU · Intel UHD Graphics 770"  # RAPL measures the integrated one only


def suspend(pci: Path, asleep: bool, users: int = 1) -> None:
    """Runtime power state: powered down, or awake with ``users`` holding it (0: nobody, it's
    only counting down to powering down)."""
    write_tree(pci, {"power/runtime_status": "suspended" if asleep else "active", "power/runtime_usage": str(users)})


def test_a_sleeping_igpu_is_left_asleep(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0)
    pci = tmp_path / "devices" / "0000:00:02.0"
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)  # baseline: 10 000 ms asleep at t=100
    suspend(pci, True)
    clock.now += 1.0
    # Reading the sleep counter would wake it; this value would make it 75 % active if read.
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "10250", "gt/gt0/rps_act_freq_mhz": "1200"})
    clock_reading, active = source.sample()
    assert (clock_reading.value, active.value) == (0, 0.0)  # asleep: 0 MHz, doing nothing
    assert clock_reading.cap == 1600.0  # i915's clock limit doesn't wake it, so it's still read
    suspend(pci, False, users=0)  # woken by something else, but nobody is using it
    clock.now += 1.0
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "1"})  # a read would show a counter reset
    assert [r.value for r in source.sample()] == [0, 0.0]  # still left alone: no read, 0 MHz, 0 %
    suspend(pci, False, users=2)  # a program is drawing on it: awake anyway, so reading is free
    clock.now += 1.0
    # Both idle seconds counted as asleep (10 000 + 2 000); 0.9 s asleep in this one: 10 % busy.
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "12900", "gt/gt0/rps_act_freq_mhz": "900"})
    clock_reading, active = source.sample()
    assert clock_reading.value == 900 and round(active.value or 0, 6) == 10.0


def test_a_gpu_unused_from_the_start_is_never_woken(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0)
    pci = tmp_path / "devices" / "0000:00:02.0"
    suspend(pci, True)
    (drm / "card1" / "gt" / "gt0" / "rc6_residency_ms").chmod(0)  # any read would fail the test below
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock.now += 1.0
    assert [r.value for r in source.sample()] == [0, 0.0]  # not even once for a starting point
    (drm / "card1" / "gt" / "gt0" / "rc6_residency_ms").chmod(0o644)
    suspend(pci, False, users=1)
    clock.now += 1.0
    assert source.sample()[1].value is None  # first real reading: the starting point
    clock.now += 1.0
    write_tree(drm / "card1", {"gt/gt0/rc6_residency_ms": "10500"})
    assert source.sample()[1].value == 50.0


def test_a_sleeping_xe_card_is_left_alone_and_keeps_its_limits(tmp_path: Path) -> None:
    xe_gt = {
        "tile0/gt0/freq0/act_freq": "900",
        "tile0/gt0/freq0/rp0_freq": "2050",
        "tile0/gt0/gtidle/idle_residency_ms": "0",
    }
    chip = {"energy1_input": "0", "energy1_label": "card", "power1_max": "190000000", "in1_input": "900"}
    drm = intel_card(
        tmp_path, {}, driver="xe", slot="0000:03:00.0", device_id="0xe20b", device_files=xe_gt, hwmon=("hwmon4", chip)
    )
    pci = tmp_path / "devices" / "0000:03:00.0"
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    hwmon = HwmonSource(root=tmp_path / "hwmon", pci_ids=pci_ids(tmp_path))
    assert [r.value for r in hwmon.sample()] == [0.9]
    suspend(pci, True)
    clock.now += 1.0
    # Values that would show if anything were read while it sleeps.
    write_tree(pci, {"tile0/gt0/freq0/rp0_freq": "9999", "hwmon/hwmon4/power1_max": "1", "hwmon/hwmon4/in1_input": "5"})
    readings = {r.label: r for r in source.sample()}
    assert (readings["Graphics clock"].value, readings["Graphics clock"].cap) == (0, 2050.0)  # remembered
    assert readings["Active time"].value == 0.0
    assert (readings["Power draw"].value, readings["Power draw"].cap) == (None, 190.0)
    assert [r.value for r in hwmon.sample()] == [None]  # the hwmon source leaves it alone too
    suspend(pci, False, users=0)  # awake but unused: still left alone
    assert [r.value for r in hwmon.sample()] == [None]
    suspend(pci, False, users=1)
    assert [r.value for r in hwmon.sample()] == [0.005]


def test_newer_igpus_named_intel_in_pci_ids_say_it_once(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0, device_id="0x7d55")
    gpu = find_integrated_gpu(drm, pci_ids(tmp_path))
    assert gpu is not None and gpu.device == "GPU · Intel Arc Graphics"


def test_cards_that_come_and_go_are_picked_up(tmp_path: Path) -> None:
    ids = pci_ids(tmp_path)
    clock = FakeClock()
    (tmp_path / "drm").mkdir()
    source = IntelGpuSource(tmp_path / "drm", ids, clock=clock)
    assert source.sample() == []
    intel_card(tmp_path, I915_GT0)  # the driver is loaded, or the card bound, after corewatch started
    clock.now += 10.0
    assert source.sample() == []  # cards are looked for again every 30 s, not every sample
    clock.now += 25.0
    assert {r.device for r in source.sample()} == {"GPU · Intel UHD Graphics 770"}
    shutil.rmtree(tmp_path / "drm")  # and unbound again
    (tmp_path / "drm").mkdir()
    clock.now += 31.0
    assert source.sample() == []


def test_odd_states_give_no_value_instead_of_failing(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, {**I915_GT0, "gt/gt0/rps_RP0_freq_mhz": "0"}, slot="0000:03:00.0", device_id="0x56a0")
    pci = tmp_path / "devices" / "0000:03:00.0"
    (pci / "hwmon").write_text("")  # not a folder: no hwmon chip to be found
    [gpu] = find_intel_gpus(drm, pci_ids(tmp_path))
    assert gpu.hwmon is None and gpu.energy == []
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    clock_reading, active = source.sample()  # the clock didn't move: no time has passed
    assert clock_reading.cap is None  # a 0 limit means "not reported"
    assert active.value is None
    (drm / "card1" / "gt" / "gt0" / "rps_RP0_freq_mhz").unlink()
    clock.now += 1.0
    assert source.sample()[0].cap is None


def test_an_energy_counter_that_vanishes_gives_no_power(tmp_path: Path) -> None:
    drm = intel_card(tmp_path, I915_GT0, slot="0000:03:00.0", device_id="0x56a0", hwmon=("hwmon7", ARC_I915))
    clock = FakeClock()
    source = IntelGpuSource(drm, pci_ids(tmp_path), clock=clock)
    (tmp_path / "devices/0000:03:00.0/hwmon/hwmon7/energy1_input").unlink()
    clock.now += 1.0
    assert source.sample()[2].value is None
    write_tree(tmp_path / "devices/0000:03:00.0/hwmon/hwmon7", {"energy1_input": "1000000"})
    clock.now += 1.0
    assert source.sample()[2].value is None  # back, but with no earlier reading to compare to
    write_tree(tmp_path / "devices/0000:03:00.0/hwmon/hwmon7", {"energy1_input": "31000000"})
    clock.now += 1.0
    assert source.sample()[2].value == 30.0
