from types import SimpleNamespace
from typing import Any

from corewatch.model import Kind
from corewatch.sources.nvidia import NvidiaSource


class NVMLError(Exception):
    pass


class NVMLError_NotSupported(NVMLError):
    pass


def fake_nvml(**overrides: Any) -> SimpleNamespace:
    values = {
        "nvmlDeviceGetTemperature": 41,
        "nvmlDeviceGetTemperatureThreshold": lambda h, which: {1: 94, 0: 99}[which],
        "nvmlDeviceGetNumFans": 2,
        "nvmlDeviceGetFanSpeed_v2": lambda h, fan: [71, 69][fan],
        "nvmlDeviceGetEnforcedPowerLimit": 200000,
        "nvmlDeviceGetPowerUsage": 29660,
        "nvmlDeviceGetClockInfo": lambda h, clock: {0: 2475, 2: 10501}[clock],
        "nvmlDeviceGetMaxClockInfo": lambda h, clock: {0: 3105, 2: 10501}[clock],
        "nvmlDeviceGetUtilizationRates": SimpleNamespace(gpu=16),
        "nvmlDeviceGetMemoryInfo": SimpleNamespace(used=3 * 2**30, total=12 * 2**30),
    } | overrides

    def make(value: Any) -> Any:
        if isinstance(value, type) and issubclass(value, Exception):

            def raiser(*args: Any) -> Any:
                raise value()

            return raiser
        if callable(value):
            return value
        return lambda *args: value

    module = SimpleNamespace(
        NVMLError=NVMLError,
        NVMLError_NotSupported=NVMLError_NotSupported,
        NVML_TEMPERATURE_GPU=0,
        NVML_TEMPERATURE_THRESHOLD_SHUTDOWN=0,
        NVML_TEMPERATURE_THRESHOLD_SLOWDOWN=1,
        NVML_CLOCK_GRAPHICS=0,
        NVML_CLOCK_MEM=2,
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: setattr(module, "shut_down", True),
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda i: f"handle{i}",
        nvmlDeviceGetName=lambda h: b"NVIDIA GeForce RTX 4070",
        shut_down=False,
    )
    for name, value in values.items():
        setattr(module, name, make(value))
    return module


def test_reads_every_gpu_sensor() -> None:
    source = NvidiaSource(nvml=fake_nvml(), nvapi=None)  # type: ignore[arg-type]
    readings = source.sample()
    assert [(r.label, r.kind, r.value, r.high, r.crit, r.cap) for r in readings] == [
        ("GPU temperature", Kind.TEMPERATURE, 41, 94, 99, None),
        ("Fan 1 speed", Kind.FAN_DUTY, 71, None, None, None),
        ("Fan 2 speed", Kind.FAN_DUTY, 69, None, None, None),
        ("Power draw", Kind.POWER, 29.66, None, None, 200.0),
        ("Graphics clock", Kind.CLOCK, 2475, None, None, 3105.0),
        ("Memory clock", Kind.CLOCK, 10501, None, None, 10501.0),
        ("GPU load", Kind.LOAD, 16.0, None, None, None),
        ("Video memory used", Kind.LOAD, 25.0, None, None, None),
    ]
    assert {r.device for r in readings} == {"GPU · NVIDIA GeForce RTX 4070"}


def test_unsupported_features_are_skipped_and_transient_errors_are_blank() -> None:
    nvml = fake_nvml(
        nvmlDeviceGetNumFans=NVMLError_NotSupported,
        nvmlDeviceGetFanSpeed=NVMLError_NotSupported,
        nvmlDeviceGetPowerUsage=NVMLError,  # e.g. GPU busy: keep the row, show no value
        nvmlDeviceGetEnforcedPowerLimit=NVMLError_NotSupported,
    )
    readings = {r.label: r for r in NvidiaSource(nvml=nvml, nvapi=None).sample()}  # type: ignore[arg-type]
    assert not any("Fan" in label for label in readings)
    assert readings["Power draw"].value is None
    assert readings["Power draw"].high is None


def test_no_driver_means_no_readings() -> None:
    def fail() -> None:
        raise NVMLError("driver not loaded")

    source = NvidiaSource(nvml=SimpleNamespace(nvmlInit=fail), nvapi=None)  # type: ignore[arg-type]
    assert source.sample() == []
    source.close()  # must not raise


def test_close_shuts_nvml_down_once() -> None:
    nvml = fake_nvml()
    source = NvidiaSource(nvml=nvml, nvapi=None)  # type: ignore[arg-type]
    source.close()
    assert nvml.shut_down is True
    assert source.sample() == []


class NVMLError_FunctionNotFound(NVMLError):
    pass


def test_power_limit_is_a_cap_not_a_warning() -> None:
    readings = {r.label: r for r in NvidiaSource(nvml=fake_nvml(nvmlDeviceGetPowerUsage=210000), nvapi=None).sample()}  # type: ignore[arg-type]
    power = readings["Power draw"]
    assert (power.cap, power.high, power.status.value) == (200.0, None, "ok")


def test_old_drivers_and_old_pynvml_skip_rows_instead_of_blanking_them() -> None:
    nvml = fake_nvml(nvmlDeviceGetMemoryInfo=NVMLError_FunctionNotFound)
    nvml.NVMLError_FunctionNotFound = NVMLError_FunctionNotFound
    del nvml.nvmlDeviceGetUtilizationRates  # not in this pynvml
    labels = [r.label for r in NvidiaSource(nvml=nvml, nvapi=None).sample()]  # type: ignore[arg-type]
    assert "Video memory used" not in labels and "GPU load" not in labels
    assert "GPU temperature" in labels


def test_transient_fan_count_error_keeps_the_fan_rows() -> None:
    nvml = fake_nvml()
    source = NvidiaSource(nvml=nvml, nvapi=None)  # type: ignore[arg-type]
    first = [r.key for r in source.sample()]

    def flaky(handle):  # type: ignore[no-untyped-def]
        raise NVMLError()

    nvml.nvmlDeviceGetNumFans = flaky
    assert [r.key for r in source.sample()] == first  # same rows, no layout flip


class FakeNvApi:
    def __init__(
        self, temps: dict[str, float] | Exception, bus: int = 1, hotspot: int | None = 1, memory: int | None = 7
    ):
        self.gpus = [SimpleNamespace(bus=bus, hotspot=hotspot, memory=memory)]
        self.temps = temps
        self.closed = False

    def temperatures(self, gpu):  # type: ignore[no-untyped-def]
        if isinstance(self.temps, Exception):
            raise self.temps
        return self.temps

    def close(self) -> None:
        self.closed = True


def with_pci(nvml: SimpleNamespace, bus: int = 1) -> SimpleNamespace:
    nvml.nvmlDeviceGetPciInfo = lambda handle: SimpleNamespace(bus=bus)
    return nvml


def test_hotspot_and_vram_come_from_nvapi_matched_by_bus() -> None:
    nvapi = FakeNvApi({"hotspot": 46.7, "memory": 48.0})
    source = NvidiaSource(nvml=with_pci(fake_nvml()), nvapi=nvapi)  # type: ignore[arg-type]
    readings = {r.label: r.value for r in source.sample()}
    assert readings["Hotspot temperature"] == 46.7 and readings["Memory temperature"] == 48.0
    source.close()
    assert nvapi.closed


def test_nvapi_rows_need_a_matching_gpu_and_channel() -> None:
    other_bus = FakeNvApi({"hotspot": 46.7}, bus=9)
    two_gpus = fake_nvml(nvmlDeviceGetCount=2)
    two_gpus.nvmlDeviceGetCount = lambda: 2
    labels = [r.label for r in NvidiaSource(nvml=with_pci(two_gpus), nvapi=other_bus).sample()]  # type: ignore[arg-type]
    assert "Hotspot temperature" not in labels  # two GPUs, no bus match: don't guess
    no_memory = FakeNvApi({"hotspot": 46.7}, memory=None)
    labels = [r.label for r in NvidiaSource(nvml=with_pci(fake_nvml()), nvapi=no_memory).sample()]  # type: ignore[arg-type]
    assert "Hotspot temperature" in labels and "Memory temperature" not in labels


def test_a_failing_nvapi_read_keeps_the_rows_blank() -> None:
    nvapi = FakeNvApi(RuntimeError("status -1"))
    readings = {r.label: r.value for r in NvidiaSource(nvml=with_pci(fake_nvml()), nvapi=nvapi).sample()}  # type: ignore[arg-type]
    assert readings["Hotspot temperature"] is None and readings["Memory temperature"] is None


def test_fan_rpm_falls_back_to_pynvml_for_the_first_fan() -> None:
    nvml = fake_nvml(nvmlDeviceGetFanSpeedRPM=2320)
    readings = {r.key: r for r in NvidiaSource(nvml=nvml, nvapi=None).sample()}  # type: ignore[arg-type]
    assert readings["nvidia/0/fan0/rpm"].value == 2320 and readings["nvidia/0/fan0/rpm"].kind is Kind.FAN
    assert "nvidia/0/fan1/rpm" not in readings  # pynvml's wrapper can only ask about fan 0


def test_real_nvapi_either_loads_or_refuses_cleanly() -> None:
    from corewatch.sources.nvapi import NvApi

    try:
        nvapi = NvApi()
    except (OSError, RuntimeError):
        return  # no NVIDIA driver here
    assert all(gpu.hotspot is None or 0 <= gpu.hotspot < 32 for gpu in nvapi.gpus)
    for gpu in nvapi.gpus:
        assert all(0 < value < 255 for value in nvapi.temperatures(gpu).values())
    nvapi.close()


def test_every_reading_has_its_own_key() -> None:
    nvapi = FakeNvApi({"hotspot": 46.7, "memory": 48.0})
    readings = NvidiaSource(nvml=with_pci(fake_nvml(nvmlDeviceGetFanSpeedRPM=2320)), nvapi=nvapi).sample()  # type: ignore[arg-type]
    keys = [r.key for r in readings]
    assert len(keys) == len(set(keys)), keys  # a shared key would merge two sensors' statistics
    assert {"nvidia/0/memory", "nvidia/0/temp/memory", "nvidia/0/temp/hotspot"} <= set(keys)


def test_fan_rpm_for_every_fan_through_pynvmls_request_structure() -> None:
    import ctypes

    class FanSpeedInfo(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]

    asked = []

    def rpm_function(handle, info_pointer):  # type: ignore[no-untyped-def]
        info = ctypes.cast(info_pointer, ctypes.POINTER(FanSpeedInfo)).contents
        asked.append(info.fan)
        if info.fan == 1:
            return 3  # NVML_ERROR_NOT_SUPPORTED for this fan
        info.speed = 2300 + info.fan
        return 0

    def check(result):  # type: ignore[no-untyped-def]
        if result == 3:
            raise NVMLError_NotSupported()

    nvml = fake_nvml()
    nvml.c_nvmlFanSpeedInfo_t = FanSpeedInfo
    nvml.nvmlFanSpeedInfo_v1 = 0x100000C
    nvml._nvmlGetFunctionPointer = lambda name: rpm_function
    nvml._nvmlCheckReturn = check
    readings = {r.key: r.value for r in NvidiaSource(nvml=nvml, nvapi=None).sample()}  # type: ignore[arg-type]
    assert asked == [0, 1]
    assert readings["nvidia/0/fan0/rpm"] == 2300.0
    assert "nvidia/0/fan1/rpm" not in readings  # unsupported for that fan: no row


def test_gpu_fans_idling_at_zero_rpm_stay_visible() -> None:
    from corewatch.monitor import Monitor

    nvml = fake_nvml(nvmlDeviceGetFanSpeedRPM=0)
    monitor = Monitor([NvidiaSource(nvml=nvml, nvapi=None)])  # type: ignore[list-item]
    rows = monitor.sample()
    keys = [r.reading.key for r in monitor.visible_rows(rows, False)]
    assert "nvidia/0/fan0/rpm" in keys  # zero-RPM idle is normal for a GPU, not an empty header


def test_ambiguous_bus_numbers_are_not_paired() -> None:
    nvapi = FakeNvApi({"hotspot": 46.7})
    nvapi.gpus.append(SimpleNamespace(bus=1, hotspot=1, memory=7))  # same bus, other PCI domain
    labels = [r.label for r in NvidiaSource(nvml=with_pci(fake_nvml()), nvapi=nvapi).sample()]  # type: ignore[arg-type]
    assert "Hotspot temperature" not in labels
