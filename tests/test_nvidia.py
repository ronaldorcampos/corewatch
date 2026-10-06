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
    source = NvidiaSource(nvml=fake_nvml())  # type: ignore[arg-type]
    readings = source.sample()
    assert [(r.label, r.kind, r.value, r.high, r.crit, r.cap) for r in readings] == [
        ("GPU temperature", Kind.TEMPERATURE, 41, 94, 99, None),
        ("Fan 1 speed", Kind.FAN_DUTY, 71, None, None, None),
        ("Fan 2 speed", Kind.FAN_DUTY, 69, None, None, None),
        ("Power draw", Kind.POWER, 29.66, None, None, 200.0),
        ("Graphics clock", Kind.CLOCK, 2475, None, None, None),
        ("Memory clock", Kind.CLOCK, 10501, None, None, None),
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
    readings = {r.label: r for r in NvidiaSource(nvml=nvml).sample()}  # type: ignore[arg-type]
    assert not any("Fan" in label for label in readings)
    assert readings["Power draw"].value is None
    assert readings["Power draw"].high is None


def test_no_driver_means_no_readings() -> None:
    def fail() -> None:
        raise NVMLError("driver not loaded")

    source = NvidiaSource(nvml=SimpleNamespace(nvmlInit=fail))  # type: ignore[arg-type]
    assert source.sample() == []
    source.close()  # must not raise


def test_close_shuts_nvml_down_once() -> None:
    nvml = fake_nvml()
    source = NvidiaSource(nvml=nvml)  # type: ignore[arg-type]
    source.close()
    assert nvml.shut_down is True
    assert source.sample() == []


class NVMLError_FunctionNotFound(NVMLError):
    pass


def test_power_limit_is_a_cap_not_a_warning() -> None:
    readings = {r.label: r for r in NvidiaSource(nvml=fake_nvml(nvmlDeviceGetPowerUsage=210000)).sample()}  # type: ignore[arg-type]
    power = readings["Power draw"]
    assert (power.cap, power.high, power.status.value) == (200.0, None, "ok")


def test_old_drivers_and_old_pynvml_skip_rows_instead_of_blanking_them() -> None:
    nvml = fake_nvml(nvmlDeviceGetMemoryInfo=NVMLError_FunctionNotFound)
    nvml.NVMLError_FunctionNotFound = NVMLError_FunctionNotFound
    del nvml.nvmlDeviceGetUtilizationRates  # not in this pynvml
    labels = [r.label for r in NvidiaSource(nvml=nvml).sample()]  # type: ignore[arg-type]
    assert "Video memory used" not in labels and "GPU load" not in labels
    assert "GPU temperature" in labels


def test_transient_fan_count_error_keeps_the_fan_rows() -> None:
    nvml = fake_nvml()
    source = NvidiaSource(nvml=nvml)  # type: ignore[arg-type]
    first = [r.key for r in source.sample()]

    def flaky(handle):  # type: ignore[no-untyped-def]
        raise NVMLError()

    nvml.nvmlDeviceGetNumFans = flaky
    assert [r.key for r in source.sample()] == first  # same rows, no layout flip
