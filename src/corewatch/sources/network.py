"""Download and upload rates per physical network interface."""

import time
from collections.abc import Callable
from pathlib import Path

from corewatch.model import Kind, Reading
from corewatch.sources.base import read_int, read_text


class NetworkSource:
    """Rates come from the kernel's byte counters: the change since the last sample over the
    time between them. Only physical interfaces (those backed by a device) that currently
    have a link are shown, so Docker bridges, loopback and unplugged ports stay out of the way.
    """

    name = "network"

    def __init__(self, root: Path = Path("/sys/class/net"), clock: Callable[[], float] = time.monotonic) -> None:
        self.root = root
        self.clock = clock
        self._previous: dict[str, tuple[int, int, float]] = {}
        self.sample()  # prime the counters so the first real sample has a rate

    def _interfaces(self) -> list[str]:
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            return []
        up = []
        for entry in entries:
            if not (entry / "device").exists():  # virtual: lo, docker0, bridges, veth, tun
                continue
            if read_text(entry / "carrier") != "1":  # no cable / not associated
                continue
            up.append(entry.name)
        return up

    def sample(self) -> list[Reading]:
        readings = []
        now = self.clock()
        current: dict[str, tuple[int, int, float]] = {}
        for interface in self._interfaces():
            stats = self.root / interface / "statistics"
            received, sent = read_int(stats / "rx_bytes"), read_int(stats / "tx_bytes")
            if received is None or sent is None:
                continue
            current[interface] = (received, sent, now)
            down = up = None
            previous = self._previous.get(interface)
            if previous is not None:
                last_received, last_sent, last_time = previous
                elapsed = now - last_time
                # Counters go backwards when a driver resets them (link reset, module reload).
                if elapsed > 0 and received >= last_received and sent >= last_sent:
                    down = (received - last_received) / elapsed
                    up = (sent - last_sent) / elapsed
            device = f"Network · {interface}"
            readings.append(Reading(f"net/{interface}/down", device, "Download", Kind.THROUGHPUT, down))
            readings.append(Reading(f"net/{interface}/up", device, "Upload", Kind.THROUGHPUT, up))
        self._previous = current
        return readings

    def notes(self) -> list[str]:
        return []

    def close(self) -> None:
        pass
