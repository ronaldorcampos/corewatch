"""AMD CPUs (k10temp, zenpower) and GPUs (amdgpu), against fake sysfs trees laid out as the
kernel drivers document them (drivers/gpu/drm/amd/pm/amdgpu_pm.c, Documentation/hwmon/k10temp.rst)."""

from pathlib import Path

from conftest import write_tree

from corewatch.model import Kind, Row
from corewatch.monitor import Monitor, headline
from corewatch.sources.hwmon import HwmonSource, pci_name

PCI_IDS = """\
# The real file has comments everywhere, including inside vendor blocks.
1002  Advanced Micro Devices, Inc. [AMD/ATI]
# Used in the Steam Deck OLED
\t1435  Aerith

\t163f  AMD Custom GPU 0405
\t164e  Raphael
\t744c  Navi 31 [Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M]
\t\t1002 0e3b  Radeon RX 7900 XTX
10de  NVIDIA Corporation
\t2786  AD104 [GeForce RTX 4070]
"""


def source(tmp_path: Path, proc_root: Path, files: dict[str, str]) -> HwmonSource:
    root = tmp_path / "hwmon"
    write_tree(root, files)
    ids = tmp_path / "pci.ids"
    ids.write_text(PCI_IDS)
    return HwmonSource(root=root, proc_root=proc_root, sys_cpu=tmp_path / "cpu", pci_ids=(ids,))


def amdgpu_card(tmp_path: Path, hwmon: str, pci: str, device_id: str, extra: dict[str, str]) -> dict[str, str]:
    files = {f"{hwmon}/name": "amdgpu"} | {f"{hwmon}/{k}": v for k, v in extra.items()}
    write_tree(
        tmp_path / "pci" / pci,
        {
            "vendor": "0x1002",
            "device": device_id,
            "gpu_busy_percent": "37",
            "mem_info_vram_used": str(2 * 2**30),
            "mem_info_vram_total": str(24 * 2**30),
        },
    )
    return files


def link(tmp_path: Path, hwmon: str, pci: str) -> None:
    (tmp_path / "hwmon" / hwmon / "device").symlink_to(tmp_path / "pci" / pci)


DGPU = {
    "temp1_input": "45000", "temp1_label": "edge", "temp1_crit": "100000", "temp1_emergency": "105000",
    "temp2_input": "61000", "temp2_label": "junction", "temp2_crit": "110000", "temp2_emergency": "115000",
    "temp3_input": "58000", "temp3_label": "mem", "temp3_crit": "100000", "temp3_emergency": "105000",
    "in0_input": "850", "in0_label": "vddgfx",
    "freq1_input": "2400000000", "freq1_label": "sclk",
    "freq2_input": "1250000000", "freq2_label": "mclk",
    "power1_average": "62000000", "power1_label": "PPT", "power1_cap": "263000000",
    "fan1_input": "0", "pwm1": "0",
}  # fmt: skip


def test_amd_graphics_card_gets_readable_names_limits_and_its_model(tmp_path: Path, proc_root: Path) -> None:
    files = amdgpu_card(tmp_path, "hwmon3", "0000:03:00.0", "0x744c", DGPU)
    src = source(tmp_path, proc_root, files)
    link(tmp_path, "hwmon3", "0000:03:00.0")
    readings = {r.label: r for r in src.sample()}
    assert {r.device for r in readings.values()} == {"GPU · AMD Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M"}
    edge, junction = readings["GPU temperature"], readings["Hotspot temperature"]
    assert (edge.value, edge.high, edge.crit) == (45.0, 100.0, 105.0)  # throttle = warning, shutdown = critical
    assert (junction.value, junction.high, junction.crit) == (61.0, 110.0, 115.0)
    assert readings["Memory temperature"].value == 58.0
    assert readings["Core voltage"].value == 0.85
    assert (readings["Graphics clock"].value, readings["Memory clock"].value) == (2400.0, 1250.0)
    power = readings["Power draw"]
    assert (power.value, power.cap, power.high) == (62.0, 263.0, None)  # the cap is for reference only
    assert readings["GPU load"].value == 37.0
    assert round(readings["Video memory used"].value, 2) == 8.33
    assert readings["Fan 1"].kind is Kind.FAN and readings["Fan 1 speed"].kind is Kind.FAN_DUTY


def test_an_amd_gpu_fan_resting_at_zero_rpm_stays_visible(tmp_path: Path, proc_root: Path) -> None:
    files = amdgpu_card(tmp_path, "hwmon3", "0000:03:00.0", "0x744c", DGPU)
    src = source(tmp_path, proc_root, files)
    link(tmp_path, "hwmon3", "0000:03:00.0")
    monitor = Monitor([src])
    rows = monitor.sample()
    shown = {r.reading.label for r in monitor.visible_rows(rows, False)}
    assert {"Fan 1", "Fan 1 speed"} <= shown  # zero-RPM idle is normal for a graphics card


def test_amd_apu_power_covers_the_whole_chip(tmp_path: Path, proc_root: Path) -> None:
    apu = {
        "temp1_input": "52000", "temp1_label": "edge",
        "in0_input": "900", "in0_label": "vddgfx",
        "in1_input": "1050", "in1_label": "vddnb",
        "freq1_input": "2200000000", "freq1_label": "sclk",
        "power1_input": "18000000", "power1_label": "PPT",
    }  # fmt: skip
    files = amdgpu_card(tmp_path, "hwmon4", "0000:0e:00.0", "0x164e", apu)
    src = source(tmp_path, proc_root, files)
    link(tmp_path, "hwmon4", "0000:0e:00.0")
    readings = {r.label: r for r in src.sample()}
    assert "Power draw (CPU and GPU)" in readings and "SoC voltage" in readings
    assert {r.device for r in readings.values()} == {"GPU · AMD Raphael"}  # no bracketed name: the codename


def test_product_name_wins_over_the_pci_database(tmp_path: Path, proc_root: Path) -> None:
    files = amdgpu_card(tmp_path, "hwmon3", "0000:03:00.0", "0x744c", {"temp1_input": "40000", "temp1_label": "edge"})
    write_tree(tmp_path / "pci" / "0000:03:00.0", {"product_name": "Radeon PRO W7900"})
    src = source(tmp_path, proc_root, files)
    link(tmp_path, "hwmon3", "0000:03:00.0")
    assert {r.device for r in src.sample()} == {"GPU · AMD Radeon PRO W7900"}


def test_unknown_amd_gpu_is_still_named(tmp_path: Path, proc_root: Path) -> None:
    files = amdgpu_card(tmp_path, "hwmon3", "0000:03:00.0", "0xffff", {"temp1_input": "40000", "temp1_label": "edge"})
    src = source(tmp_path, proc_root, files)
    link(tmp_path, "hwmon3", "0000:03:00.0")
    assert {r.device for r in src.sample()} == {"GPU · AMD"}


def test_pci_name_lookup(tmp_path: Path) -> None:
    ids = tmp_path / "pci.ids"
    ids.write_text(PCI_IDS)
    assert pci_name("0x10de", "0x2786", (ids,)) == "GeForce RTX 4070"
    assert pci_name("0x1002", "0x164e", (ids,)) == "Raphael"
    assert pci_name("0x1002", "0x0e3b", (ids,)) is None  # a subsystem line, not a device
    assert pci_name("0x1002", "0x744c", (tmp_path / "missing.ids",)) is None


def test_ryzen_with_tdie_and_chiplets(tmp_path: Path, proc_root: Path) -> None:
    write_tree(proc_root, {"cpuinfo": "model name\t: AMD Ryzen 9 7950X 16-Core Processor\n"})
    src = source(
        tmp_path,
        proc_root,
        {
            "hwmon2/name": "k10temp",
            "hwmon2/temp1_input": "75000",
            "hwmon2/temp1_label": "Tctl",
            "hwmon2/temp2_input": "48000",
            "hwmon2/temp2_label": "Tdie",
            "hwmon2/temp3_input": "46000",
            "hwmon2/temp3_label": "Tccd1",
            "hwmon2/temp4_input": "44000",
            "hwmon2/temp4_label": "Tccd2",
        },
    )
    readings = src.sample()
    assert [(r.label, r.value) for r in readings] == [
        ("CPU temperature (fan control)", 75.0),
        ("CPU temperature", 48.0),
        ("CCD 1", 46.0),
        ("CCD 2", 44.0),
    ]
    assert {r.device for r in readings} == {"CPU · AMD Ryzen 9 7950X"}
    best = headline([Row(r) for r in readings])
    assert best is not None and best.reading.value == 48.0  # the real temperature, not the offset one


def test_ryzen_with_only_tctl(tmp_path: Path, proc_root: Path) -> None:
    src = source(
        tmp_path, proc_root, {"hwmon2/name": "k10temp", "hwmon2/temp1_input": "55000", "hwmon2/temp1_label": "Tctl"}
    )
    [reading] = src.sample()
    assert reading.label == "CPU temperature"
    assert headline([Row(reading)]) is not None


def test_zenpower_voltages_currents_and_power(tmp_path: Path, proc_root: Path) -> None:
    src = source(
        tmp_path,
        proc_root,
        {
            "hwmon5/name": "zenpower",
            "hwmon5/in1_input": "1250",
            "hwmon5/in1_label": "SVI2_Core",
            "hwmon5/in2_input": "1000",
            "hwmon5/in2_label": "SVI2_SoC",
            "hwmon5/curr1_input": "35000",
            "hwmon5/curr1_label": "SVI2_C_Core",
            "hwmon5/power1_input": "43750000",
            "hwmon5/power1_label": "SVI2_P_Core",
        },
    )
    labels = {r.label: r.value for r in src.sample()}
    assert labels == {"Core voltage": 1.25, "SoC voltage": 1.0, "Core current": 35.0, "Core power": 43.75}


def test_intel_package_still_wins_the_headline(tmp_path: Path, proc_root: Path) -> None:
    src = source(
        tmp_path,
        proc_root,
        {
            "hwmon6/name": "coretemp",
            "hwmon6/temp1_input": "50000",
            "hwmon6/temp1_label": "Package id 0",
            "hwmon6/temp2_input": "70000",
            "hwmon6/temp2_label": "Core 4",
        },
    )
    best = headline([Row(r) for r in src.sample()])
    assert best is not None and best.reading.label == "CPU package"


def test_steam_deck_apu_power_and_name(tmp_path: Path, proc_root: Path) -> None:
    deck = {
        "temp1_input": "50000",
        "temp1_label": "edge",
        "in1_input": "1000",
        "in1_label": "vddnb",
        "power1_input": "9000000",
        "power1_label": "slowPPT",
    }
    files = amdgpu_card(tmp_path, "hwmon4", "0000:04:00.0", "0x163f", deck)
    src = source(tmp_path, proc_root, files)
    link(tmp_path, "hwmon4", "0000:04:00.0")
    readings = {r.label: r for r in src.sample()}
    assert "Power draw (CPU and GPU)" in readings
    assert {r.device for r in readings.values()} == {"GPU · AMD Custom GPU 0405"}  # not "AMD AMD"


def test_board_voltage_is_named(tmp_path: Path, proc_root: Path) -> None:
    files = amdgpu_card(tmp_path, "hwmon3", "0000:03:00.0", "0x744c", {"in2_input": "12100", "in2_label": "vddboard"})
    src = source(tmp_path, proc_root, files)
    link(tmp_path, "hwmon3", "0000:03:00.0")
    assert [r.label for r in src.sample() if r.kind is Kind.VOLTAGE] == ["Board voltage"]


def test_pci_names_after_comments_and_blank_lines(tmp_path: Path) -> None:
    ids = tmp_path / "pci.ids"
    ids.write_text(PCI_IDS)
    assert pci_name("1002", "744c", (ids,)) == "Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M"
    assert pci_name("1002", "1435", (ids,)) == "Aerith"


def test_zenpower_on_two_sockets(tmp_path: Path, proc_root: Path) -> None:
    src = source(
        tmp_path,
        proc_root,
        {
            "hwmon5/name": "zenpower",
            "hwmon5/temp1_input": "48000",
            "hwmon5/temp1_label": "cpu0 Tdie",
            "hwmon5/temp2_input": "75000",
            "hwmon5/temp2_label": "cpu0 Tctl",
            "hwmon5/in1_input": "1250",
            "hwmon5/in1_label": "cpu1 SVI2_Core",
        },
    )
    readings = src.sample()
    assert [r.label for r in readings] == [
        "CPU temperature (CPU 0)",
        "CPU temperature (fan control) (CPU 0)",
        "Core voltage (CPU 1)",
    ]
    best = headline([Row(r) for r in readings])
    assert best is not None and best.reading.value == 48.0
