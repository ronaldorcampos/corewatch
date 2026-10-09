"""Core data types shared by sensor sources and the user interfaces."""

import math
from collections import deque
from dataclasses import dataclass, field, fields
from enum import Enum


class Kind(Enum):
    TEMPERATURE = "temperature"
    VOLTAGE = "voltage"
    FAN = "fan"
    FAN_DUTY = "fan_duty"
    POWER = "power"
    CURRENT = "current"
    CLOCK = "clock"
    LOAD = "load"
    THROUGHPUT = "throughput"  # bytes per second


UNITS = {
    Kind.THROUGHPUT: "B/s",
    Kind.TEMPERATURE: "°C",
    Kind.VOLTAGE: "V",
    Kind.FAN: "RPM",
    Kind.FAN_DUTY: "%",
    Kind.POWER: "W",
    Kind.CURRENT: "A",
    Kind.CLOCK: "MHz",
    Kind.LOAD: "%",
}

DECIMALS = {
    Kind.THROUGHPUT: 1,
    Kind.TEMPERATURE: 1,
    Kind.VOLTAGE: 3,
    Kind.FAN: 0,
    Kind.FAN_DUTY: 0,
    Kind.POWER: 1,
    Kind.CURRENT: 2,
    Kind.CLOCK: 0,
    Kind.LOAD: 0,
}


class Status(Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Reading:
    """One sensor value at one point in time.

    ``key`` is stable across samples and identifies the sensor; ``device`` is the
    human-readable group the sensor belongs to (e.g. "CPU (Intel Core i7)").
    ``value`` is None when the sensor exists but could not be read right now.
    """

    key: str
    device: str
    label: str
    kind: Kind
    value: float | None
    low: float | None = None
    high: float | None = None
    crit: float | None = None
    # A limit worth showing that isn't a problem to reach, like a GPU's power cap.
    cap: float | None = None
    # True when the source knows this input isn't wired to anything (e.g. an open thermistor).
    unused: bool = False
    # Key of the sensor this one belongs to (a fan's speed control -> the fan): hidden together.
    companion: str | None = None
    # A fan header that may have nothing plugged in: hidden while it has never spun. Only
    # motherboard headers; a GPU's fans are known to exist even when idling at 0 RPM.
    empty_if_idle: bool = False
    # The device this reading really comes from, when it's shown in another card (Fans).
    origin: str | None = None
    # From graphics built into the CPU (Intel integrated graphics, an AMD APU). Its power is part
    # of the CPU's package power, or on an APU covers the whole chip: never add the two together.
    integrated: bool = False

    @property
    def status(self) -> Status:
        if self.value is None:
            return Status.OK
        if self.crit is not None and self.value >= self.crit:
            return Status.CRITICAL
        if self.high is not None and self.value >= self.high:
            return Status.WARNING
        if self.low is not None and self.value < self.low:
            return Status.WARNING
        return Status.OK


# How much history each sensor keeps for charts and windowed averages.
HISTORY_SECONDS = 15 * 60
# A gap longer than this between samples (suspend, a stalled UI) is not counted as
# time spent above a limit.
MAX_GAP_SECONDS = 120.0
TREND_WINDOW_SECONDS = 60.0
# Fewer seconds of data than this give a noisy, misleading trend.
MIN_TREND_SPAN_SECONDS = 20.0

# Changes per minute smaller than this read as "steady" rather than rising/falling.
STEADY_PER_MINUTE = {
    Kind.TEMPERATURE: 0.3,
    Kind.VOLTAGE: 0.005,
    Kind.FAN: 25.0,
    Kind.FAN_DUTY: 1.0,
    Kind.POWER: 1.0,
    Kind.CURRENT: 0.05,
    Kind.CLOCK: 50.0,
    Kind.LOAD: 2.0,
    Kind.THROUGHPUT: 10_000.0,
}


@dataclass
class Stats:
    """What a sensor has done since the last reset: extremes, averages, spread and history.

    ``add`` takes monotonic timestamps (``Monitor.clock``) so intervals stay right if the
    system clock changes; ``Monitor.to_wall`` turns them into times of day for display.
    """

    minimum: float | None = None
    maximum: float | None = None
    minimum_at: float | None = None
    maximum_at: float | None = None
    total: float = 0.0
    count: int = 0
    started_at: float | None = None
    seconds_warning: float = 0.0
    seconds_critical: float = 0.0
    history: deque[tuple[float, float]] = field(default_factory=deque)
    _mean: float = 0.0
    _m2: float = 0.0
    _last_at: float | None = None
    _last_status: Status = Status.OK

    def add(self, value: float | None, now: float = 0.0, status: Status = Status.OK) -> None:
        if self.started_at is None:
            self.started_at = now
        if self._last_at is not None:
            elapsed = now - self._last_at
            if 0 < elapsed <= MAX_GAP_SECONDS:
                # Time is charged to the state the sensor was in during the interval.
                if self._last_status is Status.WARNING:
                    self.seconds_warning += elapsed
                elif self._last_status is Status.CRITICAL:
                    self.seconds_critical += elapsed
        self._last_at = now
        self._last_status = status if value is not None else Status.OK
        if value is None:
            return
        if self.minimum is None or value < self.minimum:
            self.minimum, self.minimum_at = value, now
        if self.maximum is None or value > self.maximum:
            self.maximum, self.maximum_at = value, now
        self.total += value
        self.count += 1
        delta = value - self._mean  # Welford's online variance
        self._mean += delta / self.count
        self._m2 += delta * (value - self._mean)
        self.history.append((now, value))
        while self.history and self.history[0][0] < now - HISTORY_SECONDS:
            self.history.popleft()

    @property
    def average(self) -> float | None:
        return self.total / self.count if self.count else None

    @property
    def stddev(self) -> float | None:
        """Population standard deviation: how far readings typically stray from the average."""
        return math.sqrt(self._m2 / self.count) if self.count >= 2 else None

    def window(self, seconds: float, now: float) -> list[tuple[float, float]]:
        """The last ``seconds`` of history, walking back from the newest point only as far as needed."""
        cutoff = now - seconds
        points = []
        for point in reversed(self.history):
            if point[0] < cutoff:
                break
            points.append(point)
        points.reverse()
        return points

    def clear(self) -> None:
        """Forget everything in place, so rows already holding this object show the reset at once."""
        fresh = Stats()
        for item in fields(self):
            setattr(self, item.name, getattr(fresh, item.name))

    def window_average(self, seconds: float, now: float) -> float | None:
        values = [v for _, v in self.window(seconds, now)]
        return sum(values) / len(values) if values else None

    def trend_per_minute(self, now: float, seconds: float = TREND_WINDOW_SECONDS) -> float | None:
        """Least-squares slope over the recent window, in units per minute."""
        points = self.window(seconds, now)
        if len(points) < 3 or points[-1][0] - points[0][0] < MIN_TREND_SPAN_SECONDS:
            return None
        mean_t = sum(t for t, _ in points) / len(points)
        mean_v = sum(v for _, v in points) / len(points)
        spread = sum((t - mean_t) ** 2 for t, _ in points)
        if spread == 0:
            return None
        slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / spread
        return slope * 60


@dataclass
class Row:
    """A reading paired with its session statistics, ready to display."""

    reading: Reading
    stats: Stats = field(default_factory=Stats)


def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def format_value(kind: Kind, value: float | None, fahrenheit: bool = False) -> str:
    """Render a value with its unit, e.g. ``45.0 °C`` or ``1,200 RPM``."""
    if value is None:
        return "—"
    if kind is Kind.THROUGHPUT:
        return format_rate(value)
    unit = UNITS[kind]
    if kind is Kind.TEMPERATURE and fahrenheit:
        value, unit = to_fahrenheit(value), "°F"
    return f"{value:,.{DECIMALS[kind]}f} {unit}"


def format_rate(bytes_per_second: float, signed: bool = False) -> str:
    """``0 B/s``, ``12.3 KB/s``, ``4.5 MB/s``: decimal units, like browsers and file managers."""
    sign = "+" if signed else ""
    if round(abs(bytes_per_second)) < 1000:
        return f"{bytes_per_second:{sign}.0f} B/s"
    # Pick the unit after rounding, so 999,960 B/s reads "1.0 MB/s", not "1000.0 KB/s".
    for unit, scale in (("KB/s", 1e3), ("MB/s", 1e6)):
        if round(abs(bytes_per_second) / scale, 1) < 1000:
            return f"{bytes_per_second / scale:{sign}.1f} {unit}"
    return f"{bytes_per_second / 1e9:{sign}.1f} GB/s"


def format_limit(reading: Reading, fahrenheit: bool = False) -> str:
    """Describe the sensor's thresholds, e.g. ``high 80.0 °C · crit 100.0 °C``."""
    parts = []
    if reading.low is not None:
        parts.append(f"min {format_value(reading.kind, reading.low, fahrenheit)}")
    if reading.high is not None:
        parts.append(
            f"{'max' if reading.low is not None else 'high'} {format_value(reading.kind, reading.high, fahrenheit)}"
        )
    if reading.crit is not None:
        parts.append(f"crit {format_value(reading.kind, reading.crit, fahrenheit)}")
    if reading.cap is not None:
        parts.append(f"limit {format_value(reading.kind, reading.cap, fahrenheit)}")
    return " · ".join(parts)


def format_delta(kind: Kind, delta: float | None, fahrenheit: bool = False, signed: bool = True) -> str:
    """Render a difference (spread, change per minute). °F differences scale but don't shift by 32."""
    if delta is None:
        return "—"
    if kind is Kind.THROUGHPUT:
        return format_rate(delta, signed)
    unit = UNITS[kind]
    if kind is Kind.TEMPERATURE and fahrenheit:
        delta, unit = delta * 9 / 5, "°F"
    sign = "+" if signed else ""
    return f"{delta:{sign},.{DECIMALS[kind]}f} {unit}"


def format_trend(kind: Kind, per_minute: float | None, fahrenheit: bool = False) -> str:
    """``↑ +2.1 °C/min``, ``↓ -40 RPM/min`` or ``→ steady``."""
    if per_minute is None:
        return "—"
    if abs(per_minute) < STEADY_PER_MINUTE[kind]:
        return "→ steady"
    arrow = "↑" if per_minute > 0 else "↓"
    if kind is Kind.THROUGHPUT:  # "KB/s/min" is hard to read
        return f"{arrow} {format_delta(kind, per_minute, fahrenheit)} per min"
    return f"{arrow} {format_delta(kind, per_minute, fahrenheit)}/min"


def format_duration(seconds: float) -> str:
    """``0 s``, ``45 s``, ``3 min 20 s``, ``2 h 05 min``."""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def format_short(kind: Kind, value: float | None, fahrenheit: bool = False) -> str:
    """A number small enough for a tray icon, no unit: ``47``, ``2.3k``, ``4.8`` (GHz), ``1.2M``."""
    if value is None:
        return "?"
    if kind is Kind.TEMPERATURE:
        return f"{to_fahrenheit(value) if fahrenheit else value:.0f}"
    if kind is Kind.FAN:
        return f"{value:.0f}" if round(value) < 1000 else f"{value / 1000:.1f}k"
    if kind is Kind.CLOCK:
        return f"{value / 1000:.1f}"  # GHz
    if kind is Kind.VOLTAGE:
        return f"{value:.2f}" if abs(value) < 10 else f"{value:.1f}"
    if kind is Kind.CURRENT:
        return f"{value:.1f}"
    if kind is Kind.THROUGHPUT:
        if round(value) < 1000:
            return f"{value:.0f}"
        # Unit chosen after rounding, so 999,600 B/s reads "1.0M", not "1000K".
        for suffix, scale in (("K", 1e3), ("M", 1e6)):
            scaled = value / scale
            if round(scaled) < 1000:
                return f"{scaled:.1f}{suffix}" if round(scaled, 1) < 10 else f"{scaled:.0f}{suffix}"
        scaled = value / 1e9
        return f"{scaled:.1f}G" if round(scaled, 1) < 10 else f"{scaled:.0f}G"
    return f"{value:.0f}"  # load, fan duty, power
