from collections.abc import Callable

from corewatch.sources.base import Source
from corewatch.sources.cpu import CpuSource
from corewatch.sources.hwmon import HwmonSource
from corewatch.sources.intel_gpu import IntelGpuSource
from corewatch.sources.network import NetworkSource
from corewatch.sources.nvidia import NvidiaSource
from corewatch.sources.rapl import RaplSource

# CPU first, like Core Temp. Cards in the same group keep the order their first reading
# arrives in, so discrete GPUs (amdgpu and Arc through hwmon, then NVIDIA, then the Intel source,
# which lists Arc cards before the integrated GPU) all start before RAPL, whose integrated
# graphics power would otherwise put the integrated GPU's card first.
DEFAULT_FACTORIES: list[Callable[[], Source]] = [
    HwmonSource,
    CpuSource,
    NvidiaSource,
    IntelGpuSource,
    RaplSource,
    NetworkSource,
]


def default_sources(factories: list[Callable[[], Source]] | None = None) -> list[Source]:
    """Every source that starts up on this machine, CPU first like Core Temp.

    A source that fails to start (missing driver, odd sysfs layout) is left out
    rather than taking the whole app down.
    """
    sources = []
    for factory in factories or DEFAULT_FACTORIES:
        try:
            sources.append(factory())
        except Exception:
            continue
    return sources


__all__ = [
    "DEFAULT_FACTORIES",
    "CpuSource",
    "HwmonSource",
    "IntelGpuSource",
    "NetworkSource",
    "NvidiaSource",
    "RaplSource",
    "Source",
    "default_sources",
]
