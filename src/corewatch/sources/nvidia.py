"""NVIDIA GPU sensors via NVML, the library behind nvidia-smi."""

import contextlib
import ctypes
import importlib
from collections.abc import Callable
from types import ModuleType
from typing import Any

from corewatch.model import Kind, Reading


class NvidiaSource:
    name = "nvidia"

    def __init__(self, nvml: ModuleType | None = None, nvapi: Any = "auto") -> None:
        """``nvapi`` adds hotspot and VRAM temperatures: "auto" loads it if the driver
        provides it, None turns it off, anything else is used as-is (tests)."""
        self.nvml: Any = nvml
        self.nvapi: Any = None
        self.handles: list[tuple[str, Any]] = []
        self._fan_counts: dict[int, int] = {}
        self._nvapi_gpus: dict[int, Any] = {}
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
        self._attach_nvapi(nvapi)

    def _attach_nvapi(self, nvapi: Any) -> None:
        if nvapi == "auto":
            try:
                from corewatch.sources.nvapi import NvApi

                nvapi = NvApi()
            except Exception:  # no NvAPI on this driver: just no hotspot / VRAM rows
                return
        if nvapi is None:
            return
        self.nvapi = nvapi
        # Pair each NVML GPU with its NvAPI twin by PCI bus number. NvAPI only reports the
        # bus, not the PCI domain, so a bus number seen twice on either side is ambiguous
        # and those GPUs are left unpaired rather than risk showing another card's values.
        nvapi_buses = [gpu.bus for gpu in nvapi.gpus]
        nvml_buses = {}
        for index, (_, handle) in enumerate(self.handles):
            _, pci = self._call("nvmlDeviceGetPciInfo", handle)
            nvml_buses[index] = getattr(pci, "bus", None)
        for index, bus in nvml_buses.items():
            unique = list(nvml_buses.values()).count(bus) == 1 and nvapi_buses.count(bus) == 1
            if bus is not None and unique:
                self._nvapi_gpus[index] = nvapi.gpus[nvapi_buses.index(bus)]
            elif len(self.handles) == 1 and len(nvapi.gpus) == 1:
                self._nvapi_gpus[index] = nvapi.gpus[0]

    def sample(self) -> list[Reading]:
        if self.nvml is None:
            return []
        readings: list[Reading] = []
        for index, (device, handle) in enumerate(self.handles):
            readings.extend(self._device_readings(index, device, handle))
        return readings

    def _call(self, func: str, *args: Any) -> tuple[bool, Any]:
        """Call an NVML function: (supported, value). Unsupported features are skipped."""
        function = getattr(self.nvml, func, None)
        if function is None:  # an older pynvml without this call
            return False, None
        return self._call_function(function, *args)

    def _call_function(self, function: Callable[..., Any], *args: Any) -> tuple[bool, Any]:
        nvml = self.nvml
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
        readings.extend(self._gpu_extra_temperatures(index, device))

        supported, fans = self._call("nvmlDeviceGetNumFans", handle)
        if supported and fans is None:  # transient error: keep the fan rows we had
            fans = self._fan_counts.get(index)
        if fans:
            self._fan_counts[index] = fans
            for fan in range(fans):
                add(f"fan{fan}", f"Fan {fan + 1} speed", Kind.FAN_DUTY, "nvmlDeviceGetFanSpeed_v2", fan)
                supported, rpm = self._fan_rpm(handle, fan)
                if supported:
                    readings.append(Reading(f"nvidia/{index}/fan{fan}/rpm", device, f"Fan {fan + 1}", Kind.FAN, rpm))
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

    def _fan_rpm(self, handle: Any, fan: int) -> tuple[bool, float | None]:
        """RPM for one fan. pynvml's own wrapper only ever asks for fan 0, so for the others
        this fills in the same request structure itself."""
        nvml = self.nvml
        if hasattr(nvml, "c_nvmlFanSpeedInfo_t") and hasattr(nvml, "_nvmlGetFunctionPointer"):

            def read(handle: Any) -> float:
                info = nvml.c_nvmlFanSpeedInfo_t()
                info.fan = fan
                info.version = nvml.nvmlFanSpeedInfo_v1
                result = nvml._nvmlGetFunctionPointer("nvmlDeviceGetFanSpeedRPM")(handle, ctypes.byref(info))
                nvml._nvmlCheckReturn(result)
                return float(info.speed)

            return self._call_function(read, handle)
        if fan == 0:
            return self._call("nvmlDeviceGetFanSpeedRPM", handle)
        return False, None

    def _gpu_extra_temperatures(self, index: int, device: str) -> list[Reading]:
        gpu = self._nvapi_gpus.get(index)
        if gpu is None:
            return []
        try:
            temps: dict[str, float] | None = self.nvapi.temperatures(gpu)
        except Exception:  # a passing failure: keep the rows, show no value this time
            temps = None
        readings = []
        for key, label, channel in (
            ("hotspot", "Hotspot temperature", gpu.hotspot),
            ("memory", "Memory temperature", gpu.memory),
        ):
            if channel is not None:
                value = temps.get(key) if temps is not None else None
                # "temp/" keeps these apart from e.g. nvidia/0/memory (video memory used).
                readings.append(Reading(f"nvidia/{index}/temp/{key}", device, label, Kind.TEMPERATURE, value))
        return readings

    def close(self) -> None:
        if self.nvapi is not None:
            with contextlib.suppress(Exception):
                self.nvapi.close()
            self.nvapi = None
        if self.nvml is not None:
            with contextlib.suppress(Exception):
                self.nvml.nvmlShutdown()
            self.nvml = None
