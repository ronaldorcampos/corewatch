"""Per-core CPU load (from /proc/stat) and clock speed (from cpufreq)."""

from dataclasses import dataclass
from pathlib import Path

from corewatch.model import Kind, Reading
from corewatch.sources.base import cpu_device_name, discover_cores, read_int, read_text


@dataclass(frozen=True)
class Ticks:
    busy: int
    total: int


def parse_proc_stat(text: str) -> dict[str, Ticks]:
    """Busy/total jiffies per line of /proc/stat (``cpu`` is the all-CPU total)."""
    ticks: dict[str, Ticks] = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        # user nice system idle iowait irq softirq steal [guest guest_nice];
        # guest time is already counted inside user/nice, so stop at steal.
        values = [int(v) for v in fields[1:9]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        ticks[fields[0]] = Ticks(busy=total - idle, total=total)
    return ticks


def load_percent(before: Ticks | None, after: Ticks | None) -> float | None:
    if before is None or after is None:
        return None
    total = after.total - before.total
    if total <= 0:
        return None
    busy = after.busy - before.busy
    return max(0.0, min(100.0, busy * 100 / total))


class CpuSource:
    name = "cpu"

    def __init__(self, sys_cpu: Path = Path("/sys/devices/system/cpu"), proc_root: Path = Path("/proc")) -> None:
        self.sys_cpu = sys_cpu
        self.proc_root = proc_root
        self.device = cpu_device_name(proc_root)
        self.cores = discover_cores(sys_cpu)
        # Prime the counters so the first sample already shows real load.
        self._previous = self._read_ticks()

    def _read_ticks(self) -> dict[str, Ticks]:
        text = read_text(self.proc_root / "stat")
        return parse_proc_stat(text) if text else {}

    def sample(self) -> list[Reading]:
        current = self._read_ticks()
        previous, self._previous = self._previous, current
        readings = [
            Reading(
                key="cpu/load/all",
                device=self.device,
                label="CPU load (all cores)",
                kind=Kind.LOAD,
                value=load_percent(previous.get("cpu"), current.get("cpu")),
            )
        ]
        for core in self.cores:
            loads = [load_percent(previous.get(f"cpu{t}"), current.get(f"cpu{t}")) for t in core.threads]
            known = [load for load in loads if load is not None]
            readings.append(
                Reading(
                    key=f"cpu/load/{core.package}/{core.core_id}",
                    device=self.device,
                    label=f"{core.name} load",
                    kind=Kind.LOAD,
                    value=sum(known) / len(known) if known else None,
                )
            )
        for core in self.cores:
            clocks = [read_int(self.sys_cpu / f"cpu{t}" / "cpufreq" / "scaling_cur_freq") for t in core.threads]
            known_clocks = [c for c in clocks if c is not None]
            if not known_clocks:
                continue
            # The core's top speed from the hardware (a favoured core's is higher than the rest,
            # an E-core's lower), not scaling_max_freq, which is only where a policy caps it now.
            tops = [read_int(self.sys_cpu / f"cpu{t}" / "cpufreq" / "cpuinfo_max_freq") for t in core.threads]
            known_tops = [top for top in tops if top]
            readings.append(
                Reading(
                    key=f"cpu/clock/{core.package}/{core.core_id}",
                    device=self.device,
                    label=f"{core.name} clock",
                    kind=Kind.CLOCK,
                    value=max(known_clocks) / 1000,
                    cap=max(known_tops) / 1000 if known_tops else None,
                )
            )
        return readings

    def notes(self) -> list[str]:
        return []

    def close(self) -> None:
        pass
