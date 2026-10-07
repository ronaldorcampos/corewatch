"""NvAPI without a GPU: a fake library hands out real C callbacks through nvapi_QueryInterface."""

import ctypes

import pytest

from corewatch.sources import nvapi as nv


def test_struct_layouts_match_nvidias() -> None:
    # Same sizes as LACT's repr(C) structs; the driver checks them through the version word.
    assert ctypes.sizeof(nv._Channel) == 84
    assert ctypes.sizeof(nv._ChannelInfo) == 2736
    assert nv._ChannelInfo.primary.offset == 2728
    assert ctypes.sizeof(nv._Temperatures) == 168
    assert nv._version(nv._ChannelInfo, 2) == 0x20AB0 and nv._version(nv._Temperatures, 2) == 0x200A8


class FakeLibrary:
    def __init__(
        self,
        mask: int = 0b1000_0011,
        primary: dict[int, int] | None = None,
        temps: dict[int, float] | None = None,
        enum_status: int = 0,
    ) -> None:
        self.calls: list[str] = []
        primary = {0: 0, 1: 1, 3: 7} if primary is None else primary
        temps = {0: 40.4, 1: 46.5, 7: 50.0} if temps is None else temps
        handle_array = ctypes.POINTER(ctypes.c_void_p)
        uint_out = ctypes.POINTER(ctypes.c_uint32)

        def initialize() -> int:
            self.calls.append("initialize")
            return 0

        def unload() -> int:
            self.calls.append("unload")
            return 0

        def enum(handles, count):  # type: ignore[no-untyped-def]
            handles[0] = 256
            count[0] = 1
            return enum_status

        def bus(handle, out):  # type: ignore[no-untyped-def]
            out[0] = 1
            return 0

        def info(handle, data):  # type: ignore[no-untyped-def]
            if data.contents.version != nv._version(nv._ChannelInfo, 2):
                return -9  # wrong struct version
            data.contents.mask = mask
            for slot, channel in primary.items():
                data.contents.primary[slot] = channel
            return 0

        def status(handle, data):  # type: ignore[no-untyped-def]
            if data.contents.version != nv._version(nv._Temperatures, 2) or data.contents.mask != mask:
                return -9
            for channel, celsius in temps.items():
                data.contents.temps[channel] = int(celsius * 256)
            return 0

        int_fn = ctypes.CFUNCTYPE(ctypes.c_int)
        self._functions = {
            nv.INITIALIZE: int_fn(initialize),
            nv.UNLOAD: int_fn(unload),
            nv.ENUM_PHYSICAL_GPUS: ctypes.CFUNCTYPE(ctypes.c_int, handle_array, uint_out)(enum),
            nv.GPU_GET_BUS_ID: ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, uint_out)(bus),
            nv.THERM_CHANNEL_GET_INFO: ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(nv._ChannelInfo))(
                info
            ),
            nv.THERM_CHANNEL_GET_STATUS: ctypes.CFUNCTYPE(
                ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(nv._Temperatures)
            )(status),
        }

        def query(function_id: int) -> int | None:
            function = self._functions.get(function_id)
            return ctypes.cast(function, ctypes.c_void_p).value if function is not None else None

        # A real C function, like the library's own nvapi_QueryInterface symbol.
        self.nvapi_QueryInterface = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint32)(query)


def test_reads_hotspot_and_memory_from_their_channels() -> None:
    api = nv.NvApi(library=FakeLibrary())
    [gpu] = api.gpus
    assert (gpu.bus, gpu.hotspot, gpu.memory) == (1, 1, 7)
    assert api.temperatures(gpu) == {"hotspot": 46.5, "memory": 50.0}
    api.close()
    assert api.lib.calls == ["initialize", "unload"]


def test_missing_channels_and_dead_values_are_left_out() -> None:
    no_hotspot = nv.NvApi(library=FakeLibrary(primary={3: 7}))  # primary slot left at 255
    assert no_hotspot.gpus[0].hotspot is None
    unmasked = nv.NvApi(library=FakeLibrary(mask=0b0000_0011))  # memory channel 7 not in the mask
    assert unmasked.gpus[0].memory is None
    dead = nv.NvApi(library=FakeLibrary(temps={1: 0.0, 7: 255.0}))  # outside 0 < °C < 255
    assert dead.temperatures(dead.gpus[0]) == {}


def test_failed_discovery_unloads_before_giving_up() -> None:
    library = FakeLibrary(enum_status=-6)
    with pytest.raises(RuntimeError):
        nv.NvApi(library=library)
    assert library.calls == ["initialize", "unload"]


def test_unknown_function_ids_are_refused() -> None:
    api = nv.NvApi(library=FakeLibrary())
    with pytest.raises(RuntimeError):
        api._function(0xDEADBEEF)
