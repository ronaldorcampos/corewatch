"""The overview: gauges for the machine's headline numbers, and a CPU card's core heat map."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QFont,
    QFontMetrics,
    QHelpEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QToolTip, QWidget

from corewatch.gui.theme import BODY_FONT, DISPLAY_FONT, MONO_FONT, Theme, current_theme, font, paint_card, px
from corewatch.model import Kind, Row, format_limit, format_value, to_fahrenheit
from corewatch.monitor import headline

# Sources name per-core temperatures "P-core 3", "E-core 0" (Intel hybrid), "Core 5", or one
# per chiplet, "CCD 1" (AMD, which has no per-core temperatures).
CORE_LABEL = re.compile(r"^(P-core|E-core|Core|CCD) (\d+)$")
CORE_ORDER = ("P-core", "E-core", "Core", "CCD")
TILE_PREFIX = {"P-core": "P", "E-core": "E", "Core": "C", "CCD": "CCD"}
GROUP_NAMES = {"P-core": "P-core", "E-core": "E-core", "Core": "core", "CCD": "CCD"}
# The heat map's colour bands, in °C: under 40, 40-50, 50-70, 70 and up.
HEAT_EDGES = (40.0, 50.0, 70.0)

# What a gauge's arc is drawn in.
HEAT = "heat"  # the temperature's heat band
ACCENT = "accent"
AMBER = "amber"


def heat_band(celsius: float) -> int:
    """0 (coolest) to 3 (hottest)."""
    return sum(celsius >= edge for edge in HEAT_EDGES)


def heat_legend(fahrenheit: bool) -> list[str]:
    def shown(celsius: float) -> str:
        return f"{to_fahrenheit(celsius) if fahrenheit else celsius:g}"

    unit = "°F" if fahrenheit else "°C"
    low, mid, high = HEAT_EDGES
    return [
        f"under {shown(low)} {unit}",
        f"{shown(low)}–{shown(mid)} {unit}",
        f"{shown(mid)}–{shown(high)} {unit}",
        f"{shown(high)} {unit} and up",
    ]


def core_groups(rows: Sequence[Row], kind: Kind = Kind.TEMPERATURE, suffix: str = "") -> list[tuple[str, list[Row]]]:
    """Per-core rows of one kind, by core type (P-cores first) and number. ``suffix`` is what
    follows the core's name in the label, like " load"."""
    groups: dict[str, list[tuple[int, Row]]] = {}
    for row in rows:
        if row.reading.kind is not kind or not row.reading.label.endswith(suffix):
            continue
        match = CORE_LABEL.match(row.reading.label.removesuffix(suffix) if suffix else row.reading.label)
        if match:
            groups.setdefault(match[1], []).append((int(match[2]), row))
    return [
        (name, [row for _, row in sorted(groups[name], key=lambda p: p[0])]) for name in CORE_ORDER if name in groups
    ]


def tile_name(label: str) -> str:
    """``P-core 4`` -> ``P4``, ``CCD 1`` -> ``CCD1``."""
    match = CORE_LABEL.match(label)
    return f"{TILE_PREFIX[match[1]]}{match[2]}" if match else label


def cores_text(groups: Sequence[tuple[str, Sequence[Row]]]) -> str:
    """``8 P-cores · 8 E-cores``, ``6 cores``, ``2 CCDs``."""
    return " · ".join(f"{len(rows)} {GROUP_NAMES[name]}{'s' if len(rows) != 1 else ''}" for name, rows in groups)


def split_value(kind: Kind, value: float | None, fahrenheit: bool) -> tuple[str, str]:
    """``("45.0", "°C")``; ``("—", "")`` when there is no reading."""
    number, _, unit = format_value(kind, value, fahrenheit).rpartition(" ")
    return (number, unit) if number else ("—", "")


@dataclass(frozen=True)
class GaugeSpec:
    title: str
    key: str  # the sensor a click shows in the detail panel
    value: str
    unit: str
    caption: str  # inside the ring: what a full ring means
    details: tuple[str, ...]  # a line or two under the value
    arcs: tuple[tuple[float, str], ...]  # (fraction of the ring, colour role), drawn one after another
    celsius: float | None = None  # for HEAT arcs and the value's colour


def _fraction(value: float | None, full: float) -> float:
    return min(max((value or 0.0) / full, 0.0), 1.0) if full > 0 else 0.0


def _temperature_name(label: str) -> str:
    """``Hotspot temperature`` -> ``hotspot``."""
    return label.removesuffix(" temperature").lower()


def _devices(rows: Sequence[Row], prefix: str) -> list[str]:
    """The devices whose names start with ``prefix``, in the order their rows come."""
    return list(dict.fromkeys(r.reading.device for r in rows if r.reading.device.startswith(prefix)))


def primary_gpu(rows: Sequence[Row]) -> str | None:
    """The graphics card the gauges describe: a card of its own before graphics built into the
    CPU (a laptop's APU next to its RTX), and one with a temperature to show before one without."""
    gpus = _devices(rows, "GPU")
    integrated = {r.reading.device for r in rows if r.reading.integrated}
    with_temperature = {r.reading.device for r in rows if r.reading.kind is Kind.TEMPERATURE}
    dedicated = [d for d in gpus if d not in integrated]
    for pool in (
        [d for d in dedicated if d in with_temperature],
        dedicated,
        [d for d in gpus if d in with_temperature],
    ):
        if pool:
            return pool[0]
    return gpus[0] if gpus else None


@dataclass(frozen=True)
class Watts:
    key: str  # the sensor a click on the gauge opens
    value: float | None
    cap: float | None = None
    integrated: bool = False


def _power_of(rows: Sequence[Row], device: str | None) -> Watts | None:
    """A device's power: a CPU's package power (the whole chip; "Cores power" is only part of
    it), zenpower's core and SoC rails added together, or a card's power draw."""
    found = {r.reading.label: r for r in rows if r.reading.device == device and r.reading.kind is Kind.POWER}
    if not found:
        return None
    if "Core power" in found and "SoC power" in found and "Package power" not in found:
        core, soc = found["Core power"].reading, found["SoC power"].reading
        both = core.value is not None and soc.value is not None
        return Watts(core.key, (core.value or 0.0) + (soc.value or 0.0) if both else None)
    row = found.get("Package power") or found.get("Power draw") or next(iter(found.values()))
    return Watts(row.reading.key, row.reading.value, row.reading.cap, row.reading.integrated)


def power_gauge(rows: Sequence[Row], gpu_device: str | None, fahrenheit: bool) -> GaugeSpec | None:
    """CPU + GPU power, never counting a watt twice: integrated graphics' power is already inside
    the CPU's package power (Intel), or is the whole chip's (an AMD APU), so it is never added."""
    cpus = _devices(rows, "CPU")
    cpu = _power_of(rows, cpus[0]) if cpus else None
    if cpu is None:  # no RAPL reading: an APU's whole-chip figure is the CPU's power
        apu = next((r for r in rows if r.reading.kind is Kind.POWER and r.reading.integrated), None)
        cpu = Watts(apu.reading.key, apu.reading.value, apu.reading.cap, True) if apu is not None else None
    gpu = _power_of(rows, gpu_device)
    if gpu is not None and gpu.integrated:
        gpu = None
    if cpu is not None and gpu is not None:
        known = [w for w in (cpu.value, gpu.value) if w is not None]
        total = sum(known)
        both = len(known) == 2
        each = (f"CPU {format_value(Kind.POWER, cpu.value)}", f"GPU {format_value(Kind.POWER, gpu.value)}")
        # A card that isn't reporting (an idle Arc leaves it blank) doesn't blank the CPU's.
        value = split_value(Kind.POWER, total if known else None, fahrenheit)
        if cpu.cap and gpu.cap:  # a full ring is both at their limits; each arc is its share of that
            full = cpu.cap + gpu.cap
            cpu_part = _fraction(cpu.value, full)
            arcs = ((cpu_part, AMBER), (min(_fraction(gpu.value, full), 1.0 - cpu_part), ACCENT))
            return GaugeSpec("CPU + GPU power", cpu.key, *value, f"of {full:g} W", each, arcs)
        # Without both limits there's no full to measure against: the ring splits the total instead.
        split = ((_fraction(cpu.value, total), AMBER), (_fraction(gpu.value, total), ACCENT)) if both and total else ()
        return GaugeSpec("CPU + GPU power", cpu.key, *value, "CPU | GPU", each, split)
    watts = cpu or gpu
    if watts is None:
        return None
    name = "CPU" if watts is cpu else "GPU"
    cap = watts.cap
    details = [f"limit {format_value(Kind.POWER, cap)}"] if cap else []
    if watts.integrated:
        details.insert(0, "CPU and integrated graphics")
    return GaugeSpec(
        f"{name} power",
        watts.key,
        *split_value(Kind.POWER, watts.value, fahrenheit),
        f"of {cap:g} W" if cap else "",
        tuple(details),
        ((_fraction(watts.value, cap), AMBER if name == "CPU" else ACCENT),) if cap else (),
    )


def gauge_specs(rows: Sequence[Row], fahrenheit: bool) -> list[GaugeSpec]:
    """The gauges worth showing for these rows (under their original names), in order: CPU
    temperature, GPU temperature, CPU load, power. A gauge whose sensor is missing is left out."""
    specs: list[GaugeSpec] = []
    temperature_caption = "32–212 °F" if fahrenheit else "0–100 °C"

    top = headline(rows)
    if top is None or not top.reading.device.startswith("CPU"):  # a missed reading keeps its gauge, as "—"
        top = next(
            (
                r
                for r in rows
                if r.reading.device.startswith("CPU")
                and r.reading.kind is Kind.TEMPERATURE
                and r.reading.label.lower() in ("cpu package", "cpu temperature")
            ),
            None,
        )
    if top is not None:
        device_rows = [r for r in rows if r.reading.device == top.reading.device]
        cores = [r for _, group in core_groups(device_rows) for r in group if r.reading.value is not None]
        hottest = max(cores, key=lambda r: r.reading.value or 0.0, default=None)
        detail = (
            f"hottest {tile_name(hottest.reading.label)} · "
            f"{format_value(Kind.TEMPERATURE, hottest.reading.value, fahrenheit)}"
            if hottest is not None
            else format_limit(top.reading, fahrenheit)
        )
        specs.append(
            GaugeSpec(
                "CPU temperature",
                top.reading.key,
                *split_value(Kind.TEMPERATURE, top.reading.value, fahrenheit),
                temperature_caption,
                (detail,) if detail else (),
                ((_fraction(top.reading.value, 100.0), HEAT),),
                top.reading.value,
            )
        )

    gpu_device = primary_gpu(rows)
    gpu = next((r for r in rows if r.reading.device == gpu_device and r.reading.kind is Kind.TEMPERATURE), None)
    if gpu is not None:
        others = [
            r
            for r in rows
            if r.reading.device == gpu.reading.device and r.reading.kind is Kind.TEMPERATURE and r is not gpu
        ]
        specs.append(
            GaugeSpec(
                "GPU temperature",
                gpu.reading.key,
                *split_value(Kind.TEMPERATURE, gpu.reading.value, fahrenheit),
                temperature_caption,
                tuple(
                    f"{_temperature_name(r.reading.label)} {format_value(r.reading.kind, r.reading.value, fahrenheit)}"
                    for r in others[:2]
                ),
                ((_fraction(gpu.reading.value, 100.0), HEAT),),
                gpu.reading.value,
            )
        )

    load = next(
        (
            r
            for r in rows
            if r.reading.device.startswith("CPU")
            and r.reading.kind is Kind.LOAD
            and r.reading.label.startswith("CPU load")
        ),
        None,
    )
    if load is not None:
        device_rows = [r for r in rows if r.reading.device == load.reading.device]
        per_core = [
            r for _, group in core_groups(device_rows, Kind.LOAD, " load") for r in group if r.reading.value is not None
        ]
        busiest = max(per_core, key=lambda r: r.reading.value or 0.0, default=None)
        clocks = [r.reading.value for r in device_rows if r.reading.kind is Kind.CLOCK and r.reading.value is not None]
        parts = []
        if busiest is not None:
            name = tile_name(busiest.reading.label.removesuffix(" load"))
            parts.append(f"busiest {name} · {format_value(Kind.LOAD, busiest.reading.value)}")
        if clocks:
            parts.append(f"fastest {format_value(Kind.CLOCK, max(clocks))}")  # the fastest core right now
        specs.append(
            GaugeSpec(
                "CPU load",
                load.reading.key,
                *split_value(Kind.LOAD, load.reading.value, fahrenheit),
                "all cores",
                tuple(parts),
                ((_fraction(load.reading.value, 100.0), ACCENT),),
            )
        )

    power = power_gauge(rows, gpu_device, fahrenheit)
    if power is not None:
        specs.append(power)
    return specs


KEYBOARD_FOCUS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)


def _arc_color(role: str, spec: GaugeSpec, theme: Theme) -> QColor:
    if role == HEAT:
        return QColor(theme.heat[heat_band(spec.celsius)][0] if spec.celsius is not None else theme.border)
    return QColor(theme.warning if role == AMBER else theme.accent)


class Gauge(QWidget):
    """One headline number with a ring showing how far along its scale it is. Click to see its
    history in the detail panel."""

    clicked = Signal(str)
    opened = Signal(str)  # a double-click: the sensor's focus view

    RING = 96

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec: GaugeSpec | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # Tab to it, Enter to open it
        self.keyboard_focus = False  # a focus ring is for keyboard users; a click needs none
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_spec(self, spec: GaugeSpec) -> None:
        if spec != self.spec:
            self.spec = spec
            self.setToolTip(f"{spec.title}: click to see its history")
            self.setAccessibleName(f"{spec.title} {spec.value} {spec.unit}".strip())
            self.update()

    DETAIL_LINES = 2

    @staticmethod
    def _fonts() -> tuple[QFont, QFont, QFont, QFont]:
        """Title, value, unit and detail fonts."""
        return (
            font(DISPLAY_FONT, 11, QFont.Weight.DemiBold, 1.8),
            font(MONO_FONT, 32, QFont.Weight.Bold),
            font(MONO_FONT, 15, QFont.Weight.Medium),
            font(BODY_FONT, 13),
        )

    def _text_height(self) -> int:
        title, value, _, detail = (QFontMetrics(f).height() for f in self._fonts())
        return title + value + self.DETAIL_LINES * detail + px(8)

    def sizeHint(self) -> QSize:
        return QSize(px(260), max(px(self.RING), self._text_height()) + px(32))

    def needed_width(self) -> int:
        """Room for the ring, the whole title and the value with its unit. Detail lines get at
        least DETAIL_W and are shortened past that; that floor also keeps a reading that gains a
        digit from reflowing the strip."""
        if self.spec is None:
            return px(200)
        title_font, value_font, unit_font, _ = self._fonts()
        value = (
            QFontMetrics(value_font).horizontalAdvance(self.spec.value)
            + px(6)
            + QFontMetrics(unit_font).horizontalAdvance(self.spec.unit)
        )
        title = QFontMetrics(title_font).horizontalAdvance(self.spec.title.upper())
        return px(18) + px(self.RING) + px(18) + max(title, value, px(self.DETAIL_W)) + px(14) + 2

    DETAIL_W = 150

    def minimumSizeHint(self) -> QSize:
        return QSize(self.needed_width(), self.sizeHint().height())

    def focusInEvent(self, event: QFocusEvent) -> None:
        self.keyboard_focus = event.reason() in KEYBOARD_FOCUS
        super().focusInEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self.spec is not None:
            self.keyboard_focus = True
            self.update()
            self.clicked.emit(self.spec.key)
            return
        super().keyPressEvent(event)

    def ring_rect(self) -> QRectF:
        ring = px(self.RING)
        return QRectF(px(18), (self.height() - ring) / 2, ring, ring)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.spec is not None
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit(self.spec.key)
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.spec is not None:
            self.opened.emit(self.spec.key)
            return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_card(painter, QRectF(self.rect()), theme)
        if self.hasFocus() and self.keyboard_focus:
            painter.setPen(QPen(QColor(theme.accent), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(self.rect()).adjusted(3.5, 3.5, -3.5, -3.5))
        spec = self.spec
        if spec is None:
            painter.end()
            return

        ring = self.ring_rect()
        center, size = ring.center(), ring.width()
        dotted = QPen(QColor(theme.border), 1)
        dotted.setDashPattern([1, 3])
        painter.setPen(dotted)
        painter.drawEllipse(center, size / 2 - 1, size / 2 - 1)
        thickness = size * 0.07
        radius = size * 0.41
        track = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        painter.setPen(QPen(QColor(theme.edge), thickness))
        painter.drawEllipse(track)
        start = 90.0  # degrees, from the top, clockwise
        gap = 4.0 if len(spec.arcs) > 1 else 0.0
        for fraction, role in spec.arcs:
            span = fraction * 360 - gap
            if span > 0:
                painter.setPen(
                    QPen(_arc_color(role, spec, theme), thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
                )
                painter.drawArc(track, round(start * 16), round(-span * 16))
            start -= fraction * 360
        caption = font(MONO_FONT, 10)
        painter.setFont(caption)
        painter.setPen(QColor(theme.muted))
        painter.drawText(ring, Qt.AlignmentFlag.AlignCenter, spec.caption)

        left = ring.right() + px(18)
        width = self.width() - left - px(14)
        title_font, value_font, unit_font, detail_font = self._fonts()
        heights = [QFontMetrics(f).height() for f in (title_font, value_font, detail_font)]
        y = (self.height() - self._text_height()) / 2  # room for every detail line, so gauges align

        painter.setFont(title_font)
        painter.setPen(QColor(theme.muted))
        title = QFontMetrics(title_font).elidedText(spec.title.upper(), Qt.TextElideMode.ElideRight, int(width))
        painter.drawText(
            QRectF(left, y, width, heights[0]), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title
        )
        y += heights[0] + px(4)

        painter.setFont(value_font)
        value_color = QColor(theme.heat[heat_band(spec.celsius)][2]) if spec.celsius is not None else QColor(theme.text)
        painter.setPen(value_color)
        value_box = QRectF(left, y, width, heights[1])
        painter.drawText(value_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, spec.value)
        value_width = QFontMetrics(value_font).horizontalAdvance(spec.value)
        painter.setFont(unit_font)
        painter.setPen(QColor(theme.muted))
        unit_box = value_box.adjusted(value_width + px(6), 0, 0, -px(4))
        painter.drawText(unit_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, spec.unit)
        y += heights[1] + px(4)

        painter.setFont(detail_font)
        for line in spec.details[: self.DETAIL_LINES]:
            shown = QFontMetrics(detail_font).elidedText(line, Qt.TextElideMode.ElideRight, int(width))
            painter.drawText(
                QRectF(left, y, width, heights[2]), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown
            )
            y += heights[2]
        painter.end()


class GaugeStrip(QWidget):
    """The gauges, as many to a line as fit."""

    selected = Signal(str)
    opened = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.gauges: list[Gauge] = []
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self._columns = 0
        self._wanted = True

    def minimumSizeHint(self) -> QSize:
        # One gauge, not the grid's current lines side by side: that would stop the strip from
        # ever narrowing enough to rearrange them (and clip them in a narrow window).
        need = max((gauge.needed_width() for gauge in self.gauges), default=0)
        return QSize(need, self.grid.minimumSize().height())

    def set_specs(self, specs: Sequence[GaugeSpec]) -> None:
        while len(self.gauges) > len(specs):
            self.gauges.pop().deleteLater()
        while len(self.gauges) < len(specs):
            gauge = Gauge(self)
            gauge.clicked.connect(self.selected)
            gauge.opened.connect(self.opened)
            self.gauges.append(gauge)
            self._columns = 0  # place it
        for gauge, spec in zip(self.gauges, specs, strict=True):
            gauge.set_spec(spec)
        self.setVisible(self._wanted and bool(specs))
        self._reflow()

    def set_wanted(self, wanted: bool) -> None:
        """Hide the strip (while filtering) without forgetting its gauges."""
        self._wanted = wanted
        self.setVisible(wanted and bool(self.gauges))

    def columns_for(self, width: int) -> int:
        """As many gauges to a line as fit whole, balanced so no line is left with one orphan
        (four gauges go 2 x 2, not 3 + 1)."""
        if not self.gauges:
            return 1
        spacing = self.grid.spacing()
        need = max(gauge.needed_width() for gauge in self.gauges)
        fits = max(1, min(len(self.gauges), (width + spacing) // (need + spacing)))
        lines = math.ceil(len(self.gauges) / fits)
        return math.ceil(len(self.gauges) / lines)

    def _reflow(self) -> None:
        columns = self.columns_for(self.width())
        if columns == self._columns:
            return
        self._columns = columns
        for gauge in self.gauges:
            self.grid.removeWidget(gauge)
        for column in range(self.grid.columnCount()):
            self.grid.setColumnStretch(column, 0)
        for index, gauge in enumerate(self.gauges):
            self.grid.addWidget(gauge, index // columns, index % columns)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow()


@dataclass
class Tile:
    key: str
    name: str
    label: str
    celsius: float | None
    shape: QPolygonF


def hexagon(rect: QRectF) -> QPolygonF:
    """A pointy-topped hexagon filling ``rect``."""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    return QPolygonF(
        [
            QPointF(x + w / 2, y),
            QPointF(x + w, y + h / 4),
            QPointF(x + w, y + h * 3 / 4),
            QPointF(x + w / 2, y + h),
            QPointF(x, y + h * 3 / 4),
            QPointF(x, y + h / 4),
        ]
    )


class CoreHeatMap(QWidget):
    """A CPU's cores as a honeycomb, each tinted by its temperature. Click one to see its history."""

    selected = Signal(str)
    opened = Signal(str)  # a double-click: the core's focus view

    TILE_W = 66
    TILE_H = 76
    GAP = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.groups: list[tuple[str, list[Row]]] = []
        self.labels: dict[str, str] = {}  # key -> the name shown in the list (renames included)
        self.fahrenheit = False
        self.selected_key: str | None = None
        self.focus_index = 0  # the tile the keyboard is on
        self.keyboard_focus = False  # a focus ring is for keyboard users; a click needs none
        self._shape: tuple[tuple[str, ...], ...] = ()
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # arrows move between cores, Enter opens one
        self.setAccessibleName("Core heat map")

    def set_cores(
        self, groups: Sequence[tuple[str, list[Row]]], fahrenheit: bool, labels: dict[str, str] | None = None
    ) -> None:
        self.groups = list(groups)
        self.fahrenheit = fahrenheit
        self.labels = labels or {}
        shape = tuple(tuple(r.reading.key for r in rows) for _, rows in self.groups)
        if shape != self._shape:  # only a change of cores changes the height; a reading doesn't
            self._shape = shape
            self.focus_index = min(self.focus_index, max(self.core_count() - 1, 0))
            self.updateGeometry()
        self.setAccessibleDescription(
            ", ".join(
                f"{tile_name(r.reading.label)} {format_value(Kind.TEMPERATURE, r.reading.value, fahrenheit)}"
                for _, rows in self.groups
                for r in rows
            )
        )
        self.update()

    def set_selected(self, key: str | None) -> None:
        if key != self.selected_key:
            self.selected_key = key
            self.update()

    def core_count(self) -> int:
        return sum(len(rows) for _, rows in self.groups)

    # Layout: lines of tiles, every other line shifted half a tile so they nest.
    def _metrics(self) -> tuple[int, int, int, int, int]:
        tile_w, tile_h, gap = px(self.TILE_W), px(self.TILE_H), px(self.GAP)
        caption = QFontMetrics(font(DISPLAY_FONT, 11, QFont.Weight.DemiBold, 1.8)).height() + px(8)
        legend = QFontMetrics(font(BODY_FONT, 12)).height() + px(14)
        return tile_w, tile_h, gap, caption, legend

    def _per_line(self, width: int) -> int:
        tile_w, _, gap, _, _ = self._metrics()
        return max(1, (width - (tile_w + gap) // 2 + gap) // (tile_w + gap))

    def _lines(self, width: int) -> list[list[Row]]:
        per_line = self._per_line(width)
        lines: list[list[Row]] = []
        for _, rows in self.groups:  # each core type starts a line of its own
            lines.extend(rows[i : i + per_line] for i in range(0, len(rows), per_line))
        return lines

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        _, tile_h, gap, caption, legend = self._metrics()
        lines = len(self._lines(width))
        if not lines:
            return 0
        return caption + tile_h + (lines - 1) * (tile_h * 3 // 4 + gap) + legend

    def sizeHint(self) -> QSize:
        width = max(self.width(), px(400))
        return QSize(px(400), self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(px(self.TILE_W) * 2, self.heightForWidth(px(self.TILE_W) * 2))

    def _layout_tiles(self) -> list[Tile]:
        tile_w, tile_h, gap, caption, _ = self._metrics()
        tiles = []
        for index, line in enumerate(self._lines(self.width())):
            x = (tile_w + gap) / 2 if index % 2 else 0.0
            y = caption + index * (tile_h * 3 // 4 + gap)
            for row in line:
                rect = QRectF(x, y, tile_w, tile_h)
                tiles.append(
                    Tile(
                        row.reading.key,
                        tile_name(row.reading.label),
                        self.labels.get(row.reading.key, row.reading.label),
                        row.reading.value,
                        hexagon(rect),
                    )
                )
                x += tile_w + gap
        return tiles

    def tile_at(self, point: QPointF) -> Tile | None:
        return next((t for t in self._layout_tiles() if t.shape.containsPoint(point, Qt.FillRule.OddEvenFill)), None)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.updateGeometry()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        tile = self.tile_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and tile is not None:
            self.selected.emit(tile.key)
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        tile = self.tile_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and tile is not None:
            self.opened.emit(tile.key)
            return
        super().mouseDoubleClickEvent(event)

    def focusInEvent(self, event: QFocusEvent) -> None:
        self.keyboard_focus = event.reason() in KEYBOARD_FOCUS
        super().focusInEvent(event)

    def _tile_above_or_below(self, tiles: list[Tile], step: int) -> int:
        """The tile on the next line up (-1) or down (1) nearest the focused one across; lines
        are uneven (each core type starts its own), so not simply a line's length away."""
        current = tiles[self.focus_index].shape.boundingRect()
        lines = sorted({t.shape.boundingRect().top() for t in tiles})
        line = lines.index(current.top()) + step
        if not 0 <= line < len(lines):
            return self.focus_index
        candidates = [i for i, t in enumerate(tiles) if t.shape.boundingRect().top() == lines[line]]
        # Lines nest, so two tiles are often equally near: lean right going down and left going
        # up, so Down then Up comes back to the same core.
        x = current.center().x()
        return min(
            candidates,
            key=lambda i: (
                round(abs(tiles[i].shape.boundingRect().center().x() - x), 3),
                -step * tiles[i].shape.boundingRect().center().x(),
            ),
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:
        tiles = self._layout_tiles()
        if not tiles:
            super().keyPressEvent(event)
            return
        self.focus_index = min(self.focus_index, len(tiles) - 1)
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.focus_index = min(max(self.focus_index + (1 if key == Qt.Key.Key_Right else -1), 0), len(tiles) - 1)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.focus_index = self._tile_above_or_below(tiles, 1 if key == Qt.Key.Key_Down else -1)
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.focus_index = 0 if key == Qt.Key.Key_Home else len(tiles) - 1
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.selected.emit(tiles[self.focus_index].key)
        else:
            super().keyPressEvent(event)
            return
        self.keyboard_focus = True
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        tile = self.tile_at(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if tile else Qt.CursorShape.ArrowCursor)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            tile = self.tile_at(QPointF(event.pos()))
            if tile is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(
                    event.globalPos(),
                    f"{tile.label}: {format_value(Kind.TEMPERATURE, tile.celsius, self.fahrenheit)}",
                    self,
                )
            return True
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        *_, caption, legend = self._metrics()

        caption_font = font(DISPLAY_FONT, 11, QFont.Weight.DemiBold, 1.8)
        painter.setFont(caption_font)
        painter.setPen(QColor(theme.muted))
        heading = f"CORE HEAT MAP  ·  {cores_text(self.groups).upper()}"
        painter.drawText(
            QRectF(0, 0, self.width(), caption - px(8)),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            heading,
        )

        name_font = font(DISPLAY_FONT, 10, QFont.Weight.DemiBold, 1.2)
        value_font = font(MONO_FONT, 15, QFont.Weight.Bold)
        name_h = QFontMetrics(name_font).height()
        value_h = QFontMetrics(value_font).height()
        for index, tile in enumerate(self._layout_tiles()):
            band = heat_band(tile.celsius) if tile.celsius is not None else None
            edge, tint, text = theme.heat[band] if band is not None else (theme.border, theme.raised, theme.muted)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(edge))
            painter.drawPolygon(tile.shape)
            inner = tile.shape.boundingRect().adjusted(2, 2, -2, -2)
            painter.setBrush(QColor(tint))
            painter.drawPolygon(hexagon(inner))
            if tile.key == self.selected_key:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.text), 2))
                painter.drawPolygon(tile.shape)
            if index == self.focus_index and self.hasFocus() and self.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.accent), 1, Qt.PenStyle.DashLine))
                painter.drawPolygon(hexagon(tile.shape.boundingRect().adjusted(-3, -3, 3, 3)))
            box = tile.shape.boundingRect()
            top = box.center().y() - (name_h + value_h) / 2
            painter.setFont(name_font)
            painter.setPen(QColor(text))
            painter.drawText(QRectF(box.x(), top, box.width(), name_h), Qt.AlignmentFlag.AlignCenter, tile.name)
            painter.setFont(value_font)
            painter.setPen(QColor(theme.text))
            shown = (
                "—"
                if tile.celsius is None
                else f"{to_fahrenheit(tile.celsius) if self.fahrenheit else tile.celsius:.0f}°"
            )
            painter.drawText(QRectF(box.x(), top + name_h, box.width(), value_h), Qt.AlignmentFlag.AlignCenter, shown)

        legend_font = font(BODY_FONT, 12)
        painter.setFont(legend_font)
        metrics = QFontMetrics(legend_font)
        swatch = px(10)
        x = 0.0
        y = self.height() - legend + px(10)
        for band, text in enumerate(heat_legend(self.fahrenheit)):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.heat[band][0]))
            painter.drawRect(QRectF(x, y + (metrics.height() - swatch) / 2, swatch, swatch))
            painter.setPen(QColor(theme.muted))
            painter.drawText(
                QRectF(x + swatch + px(6), y, metrics.horizontalAdvance(text) + 2, metrics.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
            x += swatch + px(6) + metrics.horizontalAdvance(text) + px(16)
        painter.end()
