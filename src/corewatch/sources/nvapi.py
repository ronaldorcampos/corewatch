"""NVIDIA hotspot and VRAM temperatures through NvAPI, which NVML doesn't expose.

NvAPI ships with the driver as ``libnvidia-api.so.1``. Its only exported symbol is
``nvapi_QueryInterface``, which hands out functions by numeric ID. The IDs and struct
layouts below are the same ones LACT (github.com/ilya-zlobintsev/LACT) uses; they work
as a normal user on RTX 20-40 cards. Anything unexpected (no library, an ID the driver
doesn't know, a channel that isn't there) simply means no reading.
"""

import contextlib
import ctypes
from dataclasses import dataclass
from typing import Any

LIBRARY = "libnvidia-api.so.1"

INITIALIZE = 0x0150E828
UNLOAD = 0xD22BDD7E
ENUM_PHYSICAL_GPUS = 0xE5AC921F
GPU_GET_BUS_ID = 0x1BE0B8E5
THERM_CHANNEL_GET_INFO = 0x0BC8163D
THERM_CHANNEL_GET_STATUS = 0x65FE3AAD

MAX_GPUS = 64
CHANNELS = 32
HOTSPOT_TYPE = 1
MEMORY_TYPE = 3


class _Channel(ctypes.Structure):
    _fields_ = [
        ("cls", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("rel_loc", ctypes.c_uint32),
        ("target_gpu", ctypes.c_uint32),
        ("scaling", ctypes.c_int32),
        ("offset_sw", ctypes.c_int32),
        ("min_temp", ctypes.c_int32),
        ("max_temp", ctypes.c_int32),
        ("is_temp_sim_supported", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("offset_hw", ctypes.c_int32),
        ("rsvd", ctypes.c_uint8 * 28),
        ("data", ctypes.c_uint8 * 16),
    ]


class _ChannelInfo(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("mask", ctypes.c_int32),
        ("rsvd", ctypes.c_uint8 * 32),
        ("channels", _Channel * CHANNELS),
        ("primary", ctypes.c_uint8 * 5),
    ]


class _Temperatures(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("mask", ctypes.c_int32),
        ("rsvd", ctypes.c_uint8 * 32),
        ("temps", ctypes.c_int32 * CHANNELS),  # 1/256 °C
    ]


def _version(struct: type[ctypes.Structure], version: int) -> int:
    return ctypes.sizeof(struct) | (version << 16)


@dataclass(frozen=True)
class Gpu:
    handle: int
    bus: int
    mask: int
    hotspot: int | None  # channel index
    memory: int | None


class NvApi:
    def __init__(self, library: Any = None) -> None:
        """Raises OSError / RuntimeError if NvAPI isn't usable here."""
        self.lib = library if library is not None else ctypes.CDLL(LIBRARY)
        self._query = self.lib.nvapi_QueryInterface
        self._query.restype = ctypes.c_void_p
        self._query.argtypes = [ctypes.c_uint32]
        self._check(self._function(INITIALIZE)())
        try:
            self.gpus = self._discover()
            self._get_status = self._function(THERM_CHANNEL_GET_STATUS, ctypes.c_void_p, ctypes.POINTER(_Temperatures))
        except Exception:
            self.close()  # initialized but unusable: release it before giving up
            raise

    def _function(self, function_id: int, *argtypes: Any) -> Any:
        pointer = self._query(function_id)
        if not pointer:
            raise RuntimeError(f"NvAPI has no function {function_id:#x}")
        return ctypes.CFUNCTYPE(ctypes.c_int, *argtypes)(pointer)

    @staticmethod
    def _check(status: int) -> None:
        if status != 0:
            raise RuntimeError(f"NvAPI call failed with status {status}")

    def _discover(self) -> list[Gpu]:
        handles = (ctypes.c_void_p * MAX_GPUS)()
        count = ctypes.c_uint32()
        enum = self._function(ENUM_PHYSICAL_GPUS, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32))
        self._check(enum(handles, ctypes.byref(count)))
        get_bus = self._function(GPU_GET_BUS_ID, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        get_info = self._function(THERM_CHANNEL_GET_INFO, ctypes.c_void_p, ctypes.POINTER(_ChannelInfo))
        gpus = []
        for index in range(min(count.value, MAX_GPUS)):
            handle = handles[index]
            bus = ctypes.c_uint32()
            info = _ChannelInfo()
            info.version = _version(_ChannelInfo, 2)
            for slot in range(5):
                info.primary[slot] = 255
            try:
                self._check(get_bus(handle, ctypes.byref(bus)))
                self._check(get_info(handle, ctypes.byref(info)))
            except RuntimeError:
                continue

            def channel(kind: int, info: _ChannelInfo = info) -> int | None:
                index = info.primary[kind]
                return index if index < CHANNELS and info.mask & (1 << index) else None

            gpus.append(Gpu(handle or 0, bus.value, info.mask, channel(HOTSPOT_TYPE), channel(MEMORY_TYPE)))
        return gpus

    def temperatures(self, gpu: Gpu) -> dict[str, float]:
        """{"hotspot": °C, "memory": °C}, leaving out anything the card doesn't report."""
        status = _Temperatures()
        status.version = _version(_Temperatures, 2)
        status.mask = gpu.mask
        self._check(self._get_status(gpu.handle, ctypes.byref(status)))
        found = {}
        for name, index in (("hotspot", gpu.hotspot), ("memory", gpu.memory)):
            if index is None:
                continue
            celsius = status.temps[index] / 256
            if 0 < celsius < 255:  # outside this, the channel isn't really reporting
                found[name] = celsius
        return found

    def close(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._function(UNLOAD)()
