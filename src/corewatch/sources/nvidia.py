"""NVIDIA GPU sensors via NVML, the library behind nvidia-smi."""

import contextlib
import importlib
from types import ModuleType
from typing import Any

from corewatch.model import Kind, Reading


class NvidiaSource:
    name = "nvidia"

    def __init__(self, nvml: ModuleType | None = None) -> None:
        self.nvml: Any = nvml
        self.handles: list[tuple[str, Any]] = []
        self._fan_counts: dict[int, int] = {}
        if self.nvml is None:
            try:
                self.nvml = importlib.import_module("pynvml")
            except ImportError:
                return
        try:
            self.nvml.nvmlInit()
        except Exception:  # no driver / no NVIDIA GPU: nothing to show
            self.nvml = None
            return
        count = self.nvml.nvmlDeviceGetCount()
        for index in range(count):
            handle = self.nvml.nvmlDeviceGetHandleByIndex(index)
            name = self.nvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            device = f"GPU {index} · {name}" if count > 1 else f"GPU · {name}"
            self.handles.append((device, handle))

    def sample(self) -> list[Reading]:
        if self.nvml is None:
            return []
        readings: list[Reading] = []
        for index, (device, handle) in enumerate(self.handles):
            readings.extend(self._device_readings(index, device, handle))
        return readings

    def _call(self, func: str, *args: Any) -> tuple[bool, Any]:
        """Call an NVML function: (supported, value). Unsupported features are skipped."""
        nvml = self.nvml
        function = getattr(nvml, func, None)
        if function is None:  # an older pynvml without this call
            return False, None
        unsupported = tuple(
            getattr(nvml, name)
            for name in ("NVMLError_NotSupported", "NVMLError_FunctionNotFound")
            if hasattr(nvml, name)
        )
        try:
            return True, function(*args)
        except unsupported:  # this GPU or driver can't report it: skip the row
            return False, None
        except nvml.NVMLError:  # a passing hiccup: keep the row, show no value this time
            return True, None

    def _device_readings(self, index: int, device: str, handle: Any) -> list[Reading]:
        nvml = self.nvml
        readings = []

        def add(
            key: str,
            label: str,
            kind: Kind,
            func: str,
            *args: Any,
            scale: float = 1,
            high: float | None = None,
            crit: float | None = None,
            cap: float | None = None,
        ) -> None:
            supported, raw = self._call(func, handle, *args)
            if supported:
                value = raw / scale if raw is not None else None
                readings.append(
                    Reading(f"nvidia/{index}/{key}", device, label, kind, value, high=high, crit=crit, cap=cap)
                )

        _, slowdown = self._call("nvmlDeviceGetTemperatureThreshold", handle, nvml.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN)
        _, shutdown = self._call("nvmlDeviceGetTemperatureThreshold", handle, nvml.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN)
        add(
            "temp",
            "GPU temperature",
            Kind.TEMPERATURE,
            "nvmlDeviceGetTemperature",
            nvml.NVML_TEMPERATURE_GPU,
            high=slowdown,
            crit=shutdown,
        )

        supported, fans = self._call("nvmlDeviceGetNumFans", handle)
        if supported and fans is None:  # transient error: keep the fan rows we had
            fans = self._fan_counts.get(index)
        if fans:
            self._fan_counts[index] = fans
            for fan in range(fans):
                add(f"fan{fan}", f"Fan {fan + 1} speed", Kind.FAN_DUTY, "nvmlDeviceGetFanSpeed_v2", fan)
        else:
            add("fan0", "Fan speed", Kind.FAN_DUTY, "nvmlDeviceGetFanSpeed")

        _, power_limit = self._call("nvmlDeviceGetEnforcedPowerLimit", handle)
        add(
            "power",
            "Power draw",
            Kind.POWER,
            "nvmlDeviceGetPowerUsage",
            scale=1000,
            # Running at the power limit is normal under load: show it, don't warn about it.
            cap=power_limit / 1000 if power_limit else None,
        )
        add("clock/graphics", "Graphics clock", Kind.CLOCK, "nvmlDeviceGetClockInfo", nvml.NVML_CLOCK_GRAPHICS)
        add("clock/memory", "Memory clock", Kind.CLOCK, "nvmlDeviceGetClockInfo", nvml.NVML_CLOCK_MEM)

        supported, utilization = self._call("nvmlDeviceGetUtilizationRates", handle)
        if supported:
            gpu_load = float(utilization.gpu) if utilization is not None else None
            readings.append(Reading(f"nvidia/{index}/load", device, "GPU load", Kind.LOAD, gpu_load))
        supported, memory = self._call("nvmlDeviceGetMemoryInfo", handle)
        if supported:
            used = memory.used * 100 / memory.total if memory is not None and memory.total else None
            readings.append(Reading(f"nvidia/{index}/memory", device, "Video memory used", Kind.LOAD, used))
        return readings

    def notes(self) -> list[str]:
        return []

    def close(self) -> None:
        if self.nvml is not None:
            with contextlib.suppress(Exception):
                self.nvml.nvmlShutdown()
            self.nvml = None
