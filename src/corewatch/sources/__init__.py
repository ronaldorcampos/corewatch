from collections.abc import Callable

from corewatch.sources.base import Source
from corewatch.sources.cpu import CpuSource
from corewatch.sources.hwmon import HwmonSource
from corewatch.sources.network import NetworkSource
from corewatch.sources.nvidia import NvidiaSource
from corewatch.sources.rapl import RaplSource


def default_sources(factories: list[Callable[[], Source]] | None = None) -> list[Source]:
    """Every source that starts up on this machine, CPU first like Core Temp.

    A source that fails to start (missing driver, odd sysfs layout) is left out
    rather than taking the whole app down.
    """
    sources = []
    for factory in factories or [HwmonSource, CpuSource, RaplSource, NvidiaSource, NetworkSource]:
        try:
            sources.append(factory())
        except Exception:
            continue
    return sources


__all__ = ["CpuSource", "HwmonSource", "NetworkSource", "NvidiaSource", "RaplSource", "Source", "default_sources"]
