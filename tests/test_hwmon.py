from pathlib import Path

from conftest import write_tree

from corewatch.model import Kind
from corewatch.sources.hwmon import HwmonSource


def make_source(tmp_path: Path, proc_root: Path, files: dict[str, str]) -> HwmonSource:
    root = tmp_path / "hwmon"
    write_tree(root, files)
    dmi = tmp_path / "dmi"
    write_tree(dmi, {"board_name": "ROG STRIX Z790-A GAMING WIFI"})
    return HwmonSource(root=root, proc_root=proc_root, dmi_root=dmi, sys_cpu=tmp_path / "devices/system/cpu")


def by_key(source: HwmonSource) -> dict[str, object]:
    return {r.key: r for r in source.sample()}


def test_coretemp_labels_limits_and_scaling(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {
            "hwmon6/name": "coretemp",
            "hwmon6/temp1_input": "57000",
            "hwmon6/temp1_label": "Package id 0",
            "hwmon6/temp1_max": "80000",
            "hwmon6/temp1_crit": "100000",
            "hwmon6/temp2_input": "48000",
            "hwmon6/temp2_label": "Core 0",
        },
    )
    readings = source.sample()
    assert [(r.device, r.label, r.value, r.high, r.crit) for r in readings] == [
        ("CPU · Intel Core i7-13700K", "CPU package", 57.0, 80.0, 100.0),
        ("CPU · Intel Core i7-13700K", "Core 0", 48.0, None, None),
    ]


def test_motherboard_fans_voltages_pwm_and_units(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {
            "hwmon9/name": "nct6798",
            "hwmon9/in0_input": "1212",
            "hwmon9/in0_min": "1000",
            "hwmon9/in0_max": "1500",
            "hwmon9/in1_input": "12096",
            "hwmon9/in1_min": "0",
            "hwmon9/in1_max": "0",  # unconfigured range must not mark the rail as a warning
            "hwmon9/fan2_input": "1184",
            "hwmon9/fan2_min": "300",
            "hwmon9/fan1_input": "0",
            "hwmon9/fan1_min": "0",
            "hwmon9/pwm1": "255",
            "hwmon9/power1_average": "38200000",
            "hwmon9/curr1_input": "1500",
            "hwmon9/freq1_input": "4900000000",
        },
    )
    readings = source.sample()
    summary = [(r.label, r.kind, r.value, r.low, r.high) for r in readings]
    assert summary == [
        ("Fan 1", Kind.FAN, 0.0, None, None),
        ("Fan 2", Kind.FAN, 1184.0, 300.0, None),
        ("Fan control 1", Kind.FAN_DUTY, 100.0, None, None),
        ("Voltage 0", Kind.VOLTAGE, 1.212, 1.0, 1.5),
        ("Voltage 1", Kind.VOLTAGE, 12.096, None, None),
        ("Power 1", Kind.POWER, 38.2, None, None),
        ("Current 1", Kind.CURRENT, 1.5, None, None),
        ("Clock 1", Kind.CLOCK, 4900.0, None, None),
    ]
    assert {r.device for r in readings} == {"Motherboard · ROG STRIX Z790-A GAMING WIFI"}
    assert source.notes() == []


def test_power_input_wins_over_average(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {
            "hwmon0/name": "foo",
            "hwmon0/power1_input": "5000000",
            "hwmon0/power1_average": "9000000",
        },
    )
    assert [r.value for r in source.sample()] == [5.0]


def test_nvme_placeholder_limits_are_dropped_and_duplicates_disambiguated(tmp_path: Path, proc_root: Path) -> None:
    files = {}
    for index, controller in ((1, "nvme0"), (2, "nvme1")):
        files |= {
            f"hwmon{index}/name": "nvme",
            f"hwmon{index}/temp2_input": "53850",
            f"hwmon{index}/temp2_label": "Sensor 1",
            f"hwmon{index}/temp2_max": "65261850",
            f"hwmon{index}/temp2_min": "-273150",
            f"devices/{controller}/model": "Samsung SSD 980 PRO 2TB   ",
        }
    source = make_source(tmp_path, proc_root, files)
    root = tmp_path / "hwmon"
    (root / "hwmon1" / "device").symlink_to(root / "devices" / "nvme0")
    (root / "hwmon2" / "device").symlink_to(root / "devices" / "nvme0")  # same name twice -> suffix
    readings = source.sample()
    assert [r.device for r in readings] == [
        "NVMe nvme0 · Samsung SSD 980 PRO 2TB",
        "NVMe nvme0 · Samsung SSD 980 PRO 2TB (2)",
    ]
    assert all(r.high is None for r in readings)
    assert readings[0].value == 53.85


def test_unreadable_value_is_none_not_dropped(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(tmp_path, proc_root, {"hwmon7/name": "iwlwifi_1_2", "hwmon7/temp1_input": "N/A"})
    [reading] = source.sample()
    assert (reading.device, reading.label, reading.value) == ("Wi-Fi adapter", "Temperature 1", None)


def test_chips_without_channels_are_skipped_and_natural_order(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {
            "hwmon5/name": "asus",
            "hwmon10/name": "late",
            "hwmon10/temp1_input": "1000",
            "hwmon2/name": "early",
            "hwmon2/temp1_input": "2000",
        },
    )
    assert [r.device for r in source.sample()] == ["early", "late"]


def test_note_when_no_fans_or_voltages(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(tmp_path, proc_root, {"hwmon0/name": "acpitz", "hwmon0/temp1_input": "27800"})
    source.sample()
    [note] = source.notes()
    assert "modprobe nct6775" in note


def test_missing_root_yields_nothing(tmp_path: Path, proc_root: Path) -> None:
    source = HwmonSource(root=tmp_path / "absent", proc_root=proc_root)
    assert source.sample() == []


def nct_files(prefix: str = "hwmon8") -> dict[str, str]:
    return {
        f"{prefix}/name": "nct6798",
        f"{prefix}/in0_input": "1184",
        f"{prefix}/in1_input": "1000",
        f"{prefix}/in2_input": "3424",
        f"{prefix}/in4_input": "1000",
        f"{prefix}/in5_input": "1144",
        f"{prefix}/in8_input": "3216",
        f"{prefix}/temp3_input": "127000",
        f"{prefix}/temp3_label": "AUXTIN0",
        f"{prefix}/temp12_input": "0",
        f"{prefix}/temp12_label": "PCH_CPU_TEMP",
        f"{prefix}/temp1_input": "40000",
        f"{prefix}/temp1_label": "SYSTIN",
        f"{prefix}/pwm2": "147",
    }


def test_asus_nct679x_voltages_get_names_and_divider_scaling(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(tmp_path, proc_root, nct_files())
    write_tree(tmp_path / "dmi", {"board_vendor": "ASUSTeK COMPUTER INC."})
    volts = {r.label: r.value for r in source.sample() if r.kind is Kind.VOLTAGE}
    assert volts == {
        "CPU core (Vcore)": 1.184,
        "+5V": 5.0,
        "AVCC": 3.424,
        "+12V": 12.0,
        "Voltage 5": 1.144,  # board-specific input we can't name
        "CMOS battery": 3.216,
    }


def test_other_vendors_only_get_the_chip_internal_names(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(tmp_path, proc_root, nct_files())
    write_tree(tmp_path / "dmi", {"board_vendor": "Micro-Star International Co., Ltd."})
    volts = {r.label: r.value for r in source.sample() if r.kind is Kind.VOLTAGE}
    assert volts["Voltage 0"] == 1.184
    assert volts["Voltage 1"] == 1.0  # no ASUS divider assumed
    assert volts["AVCC"] == 3.424


def test_unconnected_motherboard_temps_are_flagged_and_pwm_links_to_its_fan(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(tmp_path, proc_root, nct_files())
    readings = {r.label: r for r in source.sample()}
    assert readings["AUXTIN0"].unused and readings["PCH_CPU_TEMP"].unused
    assert not readings["SYSTIN"].unused
    assert readings["Fan control 2"].companion == readings["Fan control 2"].key.replace("pwm2", "fan2")


def test_zero_degrees_off_the_motherboard_is_not_unused(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(tmp_path, proc_root, {"hwmon0/name": "drivetemp", "hwmon0/temp1_input": "0"})
    [reading] = source.sample()
    assert not reading.unused


def hybrid_topology(tmp_path: Path) -> Path:
    """An i7-13700K-like layout: P-cores with two threads at core IDs 0 and 4, E-cores at 32 and 33."""
    sys_cpu = tmp_path / "devices" / "system" / "cpu"
    files = {"../../cpu_core/cpus": "0-3", "../../cpu_atom/cpus": "4-5"}
    for cpu, core_id in enumerate((0, 0, 4, 4, 32, 33)):
        files[f"cpu{cpu}/topology/physical_package_id"] = "0"
        files[f"cpu{cpu}/topology/core_id"] = str(core_id)
    write_tree(sys_cpu, files)
    return sys_cpu


def test_coretemp_core_ids_become_p_and_e_core_names(tmp_path: Path, proc_root: Path) -> None:
    hybrid_topology(tmp_path)
    source = make_source(
        tmp_path,
        proc_root,
        {
            "hwmon6/name": "coretemp",
            "hwmon6/temp2_input": "45000",
            "hwmon6/temp2_label": "Core 4",
            "hwmon6/temp3_input": "46000",
            "hwmon6/temp3_label": "Core 33",
            "hwmon6/temp4_input": "47000",
            "hwmon6/temp4_label": "Core 99",  # unknown to the topology: left as the driver named it
            "platform/coretemp.0/x": "",
        },
    )
    (tmp_path / "hwmon" / "hwmon6" / "device").symlink_to(tmp_path / "hwmon" / "platform" / "coretemp.0")
    assert [r.label for r in source.sample()] == ["P-core 1", "E-core 1", "Core 99"]


def test_labels_and_limits_are_cached_but_values_are_fresh(tmp_path: Path, proc_root: Path) -> None:
    now = [0.0]
    root = tmp_path / "hwmon"
    write_tree(root, {"hwmon1/name": "nvme", "hwmon1/temp1_input": "40000", "hwmon1/temp1_label": "Composite"})
    source = HwmonSource(root=root, proc_root=proc_root, sys_cpu=tmp_path / "cpu", clock=lambda: now[0])
    source.sample()
    write_tree(root, {"hwmon1/temp1_input": "41000", "hwmon1/temp1_label": "Renamed"})
    [reading] = source.sample()
    assert (reading.label, reading.value) == ("Composite", 41.0)
    now[0] = 31.0  # past METADATA_SECONDS
    [reading] = source.sample()
    assert reading.label == "Renamed"
    (root / "hwmon1" / "temp1_input").unlink()
    (root / "hwmon1" / "temp1_label").unlink()
    (root / "hwmon1" / "name").unlink()
    (root / "hwmon1").rmdir()
    assert source.sample() == [] and source._chips == {}  # driver unloaded: cache dropped


def test_zero_degree_limits_are_ignored(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path, proc_root, {"hwmon0/name": "foo", "hwmon0/temp1_input": "30000", "hwmon0/temp1_max": "0"}
    )
    [reading] = source.sample()
    assert reading.high is None and reading.status.value == "ok"


def test_a_zero_volt_minimum_alone_is_dropped(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {"hwmon8/name": "foo", "hwmon8/in0_input": "1184", "hwmon8/in0_min": "0", "hwmon8/in0_max": "1744"},
    )
    [reading] = source.sample()
    assert (reading.low, reading.high) == (None, 1.744)


def test_a_negative_rail_keeps_its_real_minimum(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {"hwmon8/name": "foo", "hwmon8/in3_input": "-12100", "hwmon8/in3_min": "-13200", "hwmon8/in3_max": "-10800"},
    )
    [reading] = source.sample()
    assert (reading.low, reading.high) == (-13.2, -10.8)
    assert reading.status.value == "ok"


def test_only_motherboard_fan_headers_may_be_empty(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path, proc_root, {"hwmon9/name": "nct6798", "hwmon9/fan1_input": "0", "hwmon9/pwm1": "100"}
    )
    readings = {r.kind: r for r in source.sample()}
    assert readings[Kind.FAN].empty_if_idle and not readings[Kind.FAN_DUTY].empty_if_idle


def test_fan_hub_ports_with_nothing_plugged_in_can_be_hidden(tmp_path: Path, proc_root: Path) -> None:
    from corewatch.monitor import Monitor

    source = make_source(
        tmp_path, proc_root, {"hwmon10/name": "corsaircpro", "hwmon10/fan1_input": "0", "hwmon10/fan2_input": "900"}
    )
    readings = {r.label: r for r in source.sample()}
    assert readings["Fan 1"].empty_if_idle  # a fan hub, not a motherboard, but just as likely empty
    monitor = Monitor([source])
    shown = [r.reading.label for r in monitor.visible_rows(monitor.sample(), False)]
    assert shown == ["Fan 2"]


def test_a_third_temperature_limit_only_remaps_when_there_is_no_warning_limit(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {
            "hwmon11/name": "max6695",
            "hwmon11/temp1_input": "60000",
            "hwmon11/temp1_max": "70000",
            "hwmon11/temp1_crit": "85000",
            "hwmon11/temp1_emergency": "100000",
        },
    )
    [reading] = source.sample()
    assert (reading.high, reading.crit) == (70.0, 85.0)  # its own warning and critical limits stay


def test_a_thinkpads_fan_control_is_a_setpoint_and_a_boards_is_not(tmp_path: Path, proc_root: Path) -> None:
    source = make_source(
        tmp_path,
        proc_root,
        {
            "hwmon5/name": "thinkpad",
            "hwmon5/fan1_input": "0",  # stopped by the firmware while cool
            "hwmon5/pwm1": "255",  # yet reads 100 % in automatic mode
            "hwmon5/pwm1_enable": "2",
            **nct_files(),
        },
    )
    setpoints = {r.key: r.setpoint for r in source.sample() if r.kind is Kind.FAN_DUTY}
    assert setpoints == {"hwmon/thinkpad@hwmon5/pwm1": True, "hwmon/nct6798@hwmon8/pwm2": False}
