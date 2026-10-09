"""The focus view: one sensor across the whole window. A dial with its reading against its scale,
a large chart with its limits, every statistic, and for a CPU sensor every core over the same
minutes, on one scale."""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from corewatch.gui.cards import CompactView, Item, ViewData
from corewatch.gui.overview import core_groups, heat_band, split_value
from corewatch.gui.theme import BODY_FONT, DISPLAY_FONT, MONO_FONT, Theme, current_theme, font, paint_card, px
from corewatch.gui.widgets import DetailPanel, ElidedLabel, HistoryChart, _clock, drawable_segments, value_range
from corewatch.model import (
    Kind,
    Reading,
    Row,
    Status,
    format_delta,
    format_duration,
    format_trend,
    format_value,
    to_fahrenheit,
)

HEAT_WORDS = ("cool", "mild", "warm", "hot")


def dial_full_scale(reading: Reading) -> float | None:
    """What a full dial stands for, counting from zero; None for a sensor with no natural scale
    (a voltage, a clock, a fan's RPM, traffic), which gets its number without a ring."""
    if reading.kind is Kind.TEMPERATURE:
        top = reading.crit if reading.crit is not None else reading.high
        return max(100.0, (top if top is not None else 0.0) + 10.0)
    if reading.kind in (Kind.LOAD, Kind.FAN_DUTY):
        return 100.0
    if reading.kind is Kind.POWER and reading.cap:
        return reading.cap
    return None


def reading_word(reading: Reading) -> str:
    """A word for where the reading stands: past a limit, or for a temperature, how warm."""
    if reading.status is Status.CRITICAL:
        return "critical"
    if reading.status is Status.WARNING:
        return "past its limit"
    if reading.kind is Kind.TEMPERATURE and reading.value is not None:
        return HEAT_WORDS[heat_band(reading.value)]
    return ""


def headroom(reading: Reading, fahrenheit: bool) -> tuple[str, str]:
    """(how far, to what): the gap to the nearest limit the reading is heading for."""
    value = reading.value
    if value is None:
        return "—", "no reading"

    def gap(amount: float) -> str:
        return format_delta(reading.kind, amount, fahrenheit, signed=False)

    low = reading.low
    if low is not None and value < low:  # a fan too slow, a rail sagging
        return gap(low - value), "below the low limit"
    uppers = ((reading.high, "the high limit"), (reading.crit, "critical"), (reading.cap, "its limit"))
    upper = next(((limit, name) for limit, name in uppers if limit is not None), None)
    if upper is not None:
        limit, name = upper
        if value > limit:
            return gap(value - limit), f"past {name}"
        if low is None or limit - value <= value - low:  # whichever limit is nearer
            return gap(limit - value), f"to {name}"
    if low is not None:
        return gap(value - low), "above the low limit"
    return "—", "no limit reported"


def trend_word(text: str, kind: Kind) -> str:
    """``warming``/``cooling`` for a temperature, ``rising``/``falling`` otherwise, from
    ``format_trend``'s arrow."""
    if text.startswith("↑"):
        return "warming" if kind is Kind.TEMPERATURE else "rising"
    if text.startswith("↓"):
        return "cooling" if kind is Kind.TEMPERATURE else "falling"
    return "steady" if text.startswith("→") else ""


def balanced(count: int, fits: int) -> int:
    """Tiles to a line: as many as fit, evened out so the last line isn't left with one (8 tiles
    where 7 fit go 4 and 4, not 7 and 1)."""
    fits = max(1, fits)
    if count <= fits:
        return max(1, count)
    return math.ceil(count / math.ceil(count / fits))


@dataclass(frozen=True)
class Stat:
    title: str
    value: str
    note: str = ""
    color: str = ""  # the value's colour; the theme's text colour when empty


def focus_stats(row: Row, now: float, fahrenheit: bool, to_wall: Callable[[float], float]) -> list[Stat]:
    """The statistics under the chart, each with a word on what it means."""
    reading, stats = row.reading, row.stats
    kind, f = reading.kind, fahrenheit
    since = _clock(stats.started_at, to_wall)[:5] if stats.started_at is not None else ""
    past = Stat("Past a limit", "no limit", "none reported")
    if reading.high is not None or reading.low is not None or reading.crit is not None:
        seconds = stats.seconds_warning + stats.seconds_critical
        # The time isn't kept per side, so a sensor with both a low and a high limit can only say
        # it was outside them.
        if reading.high is not None and reading.low is not None:
            never, past_note = "never outside its limits", "outside its limits"
        elif reading.high is not None:
            never, past_note = f"never past {format_value(kind, reading.high, f)}", "past the high limit"
        elif reading.low is not None:
            never, past_note = f"never below {format_value(kind, reading.low, f)}", "below the low limit"
        else:
            never, past_note = f"never at {format_value(kind, reading.crit, f)}", "at critical"
        past = Stat("Past a limit", format_duration(seconds), never if seconds == 0 else past_note)
    critical = Stat("At critical", "no limit", "none reported")
    if reading.crit is not None:
        shown = format_value(kind, reading.crit, f)
        critical = Stat(
            "At critical",
            format_duration(stats.seconds_critical),
            f"never at {shown}" if stats.seconds_critical == 0 else f"at or above {shown}",
        )
    return [
        Stat("Lowest", format_value(kind, stats.minimum, f), f"at {_clock(stats.minimum_at, to_wall)}"),
        Stat("Highest", format_value(kind, stats.maximum, f), f"at {_clock(stats.maximum_at, to_wall)}"),
        Stat("Session average", format_value(kind, stats.average, f), f"since {since}" if since else ""),
        Stat("Last minute", format_value(kind, stats.window_average(60, now), f), "average"),
        Stat("Last 5 min", format_value(kind, stats.window_average(300, now), f), "average"),
        Stat("Variation", f"± {format_delta(kind, stats.stddev, f, signed=False)}", "spread"),
        past,
        critical,
    ]


class Panel(QFrame):
    """A card like the rest of the window's, for the focus view's sections."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        paint_card(painter, QRectF(self.rect()), current_theme())
        painter.end()


def _caps_font(pixels: int = 10) -> QFont:
    return font(DISPLAY_FONT, pixels, QFont.Weight.DemiBold, 1.5)


class StatGrid(QWidget):
    """Statistics as tiles, as many to a line as fit (at most ``max_columns``)."""

    TILE_W = 170
    GAP = 8

    def __init__(self, max_columns: int = 8, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.max_columns = max_columns
        self.stats: list[Stat] = []
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_stats(self, stats: Sequence[Stat]) -> None:
        stats = list(stats)
        if len(stats) != len(self.stats):
            self.updateGeometry()
        if stats != self.stats:
            self.stats = stats
            self.setAccessibleDescription("; ".join(f"{s.title}: {s.value}" for s in stats))
            self.update()

    def columns_for(self, width: int) -> int:
        fits = (width + px(self.GAP)) // (px(self.TILE_W) + px(self.GAP))
        return balanced(len(self.stats), min(self.max_columns, fits))

    @staticmethod
    def _fonts() -> tuple[QFont, QFont, QFont]:
        return _caps_font(), font(MONO_FONT, 18, QFont.Weight.Bold), font(BODY_FONT, 12)

    def tile_height(self) -> int:
        return sum(QFontMetrics(f).height() for f in self._fonts()) + px(28)

    def place(self, width: int) -> list[QRectF]:
        columns = self.columns_for(width)
        gap = px(self.GAP)
        tile_w = (width - gap * (columns - 1)) / columns
        tile_h = self.tile_height()
        return [
            QRectF((i % columns) * (tile_w + gap), (i // columns) * (tile_h + gap), tile_w, tile_h)
            for i in range(len(self.stats))
        ]

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return math.ceil(max((r.bottom() for r in self.place(width)), default=0.0))

    def sizeHint(self) -> QSize:
        return QSize(px(360), self.heightForWidth(max(self.width(), px(360))))

    def minimumSizeHint(self) -> QSize:
        return QSize(px(self.TILE_W), self.heightForWidth(px(self.TILE_W)))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.updateGeometry()

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        title_font, value_font, note_font = self._fonts()
        heights = [QFontMetrics(f).height() for f in (title_font, value_font, note_font)]
        for stat, rect in zip(self.stats, self.place(self.width()), strict=True):
            painter.fillRect(rect, QColor(theme.surface))
            painter.setPen(QPen(QColor(theme.edge), 1))
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            inner = rect.adjusted(px(14), px(10), -px(14), -px(8))
            y = inner.top()
            for text, qfont, color, height in (
                (stat.title.upper(), title_font, theme.muted, heights[0]),
                (stat.value, value_font, stat.color or theme.text, heights[1]),
                (stat.note, note_font, theme.muted, heights[2]),
            ):
                painter.setFont(qfont)
                painter.setPen(QColor(color))
                shown = QFontMetrics(qfont).elidedText(text, Qt.TextElideMode.ElideRight, int(inner.width()))
                painter.drawText(
                    QRectF(inner.left(), y, inner.width(), height),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    shown,
                )
                y += height + px(4)
        painter.end()


class Dial(QWidget):
    """The reading, large, inside a ring showing how far along its scale it is: the critical
    zone shaded at the end of the ring and a tick at the high limit. A sensor with no natural
    scale gets its number without the ring."""

    SWEEP = 270.0  # degrees of ring, open at the bottom
    SIZE = 260

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.reading: Reading | None = None
        self.fahrenheit = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_reading(self, reading: Reading | None, fahrenheit: bool) -> None:
        if (reading, fahrenheit) != (self.reading, self.fahrenheit):
            self.reading, self.fahrenheit = reading, fahrenheit
            if reading is not None:
                value, unit = split_value(reading.kind, reading.value, fahrenheit)
                self.setAccessibleName(f"Now {value} {unit} {reading_word(reading)}".strip())
            self.update()

    def legend(self) -> list[tuple[str, str]]:
        """(colour role, text) under the ring: the limits it marks."""
        reading = self.reading
        if reading is None or dial_full_scale(reading) is None:
            return []
        shown = []
        if reading.high is not None:
            shown.append(("warning", f"high {format_value(reading.kind, reading.high, self.fahrenheit)}"))
        if reading.crit is not None:
            shown.append(("critical", f"critical {format_value(reading.kind, reading.crit, self.fahrenheit)}"))
        return shown

    def _legend_height(self) -> int:
        return QFontMetrics(font(BODY_FONT, 13)).height() + px(10)

    def sizeHint(self) -> QSize:
        return QSize(px(self.SIZE), px(self.SIZE) + self._legend_height())

    def minimumSizeHint(self) -> QSize:
        return QSize(px(200), self.sizeHint().height())

    def _angle(self, fraction: float) -> float:
        """Qt's degrees (counter-clockwise from 3 o'clock) for a point ``fraction`` along the ring,
        which runs clockwise from the bottom left."""
        return 225.0 - min(max(fraction, 0.0), 1.0) * self.SWEEP

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        reading = self.reading
        if reading is None:
            painter.end()
            return
        side = min(self.width(), self.height() - self._legend_height())
        ring = QRectF((self.width() - side) / 2, 0, side, side)
        center = ring.center()
        full = dial_full_scale(reading)
        status_color = theme.value_color(reading.status)
        if full is not None:
            dotted = QPen(QColor(theme.border), 1)
            dotted.setDashPattern([1, 4])
            painter.setPen(dotted)
            painter.drawEllipse(center, side * 0.48, side * 0.48)
            thickness = side * 0.06
            radius = side * 0.40
            track = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)

            def arc(start: float, end: float, color: QColor) -> None:
                painter.setPen(QPen(color, thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                begin = self._angle(start)
                painter.drawArc(track, round(begin * 16), round((self._angle(end) - begin) * 16))

            arc(0.0, 1.0, QColor(theme.edge))
            if reading.crit is not None:
                zone = QColor(theme.critical)
                zone.setAlpha(90)
                arc(reading.crit / full, 1.0, zone)
            if reading.value is not None:
                ring_color = (
                    QColor(theme.heat[heat_band(reading.value)][0])
                    if reading.kind is Kind.TEMPERATURE and reading.status is Status.OK
                    else status_color
                )
                arc(0.0, reading.value / full, ring_color)
            if reading.high is not None:
                angle = math.radians(self._angle(reading.high / full))
                direction = QPointF(math.cos(angle), -math.sin(angle))
                painter.setPen(QPen(QColor(theme.warning), 2))
                painter.drawLine(center + direction * (radius - thickness), center + direction * (radius + thickness))

        # Inside the ring: NOW, the value, its unit and a word for it.
        value, unit = split_value(reading.kind, reading.value, self.fahrenheit)
        caption, big, small = _caps_font(11), font(MONO_FONT, 52, QFont.Weight.Bold), font(MONO_FONT, 15)
        while QFontMetrics(big).horizontalAdvance(value) > side * 0.62 and big.pixelSize() > px(20):
            big.setPixelSize(big.pixelSize() - 2)  # a long number (12,000 RPM) still fits the ring
        heights = [QFontMetrics(f).height() for f in (caption, big, small)]
        y = center.y() - sum(heights) / 2
        word = reading_word(reading)
        value_color = (
            QColor(theme.heat[heat_band(reading.value)][2])
            if reading.kind is Kind.TEMPERATURE and reading.value is not None and reading.status is Status.OK
            else status_color
        )
        for text, qfont, color, height in (
            ("NOW", caption, QColor(theme.muted), heights[0]),
            (value, big, value_color, heights[1]),
            (" · ".join(p for p in (unit, word) if p), small, QColor(theme.muted), heights[2]),
        ):
            painter.setFont(qfont)
            painter.setPen(color)
            painter.drawText(QRectF(0, y, self.width(), height), Qt.AlignmentFlag.AlignCenter, text)
            y += height

        # The limits the ring marks.
        legend = self.legend()
        if legend:
            body = font(BODY_FONT, 13)
            metrics = QFontMetrics(body)
            swatch, gap = px(14), px(18)
            widths = [swatch + px(6) + metrics.horizontalAdvance(text) for _, text in legend]
            x = (self.width() - sum(widths) - gap * (len(widths) - 1)) / 2
            top = ring.bottom() + px(4)
            painter.setFont(body)
            for (role, text), width in zip(legend, widths, strict=True):
                color = QColor(theme.warning if role == "warning" else theme.critical)
                mark = 2 if role == "warning" else 6
                painter.fillRect(QRectF(x, top + metrics.height() / 2 - mark / 2, swatch, mark), color)
                painter.setPen(QColor(theme.muted))
                painter.drawText(
                    QRectF(x + swatch + px(6), top, width, metrics.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    text,
                )
                x += width + gap
        painter.end()


# ----- every core ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreItem(Item):
    name: str = ""
    value: str = ""
    band: int = 0
    points: tuple[tuple[float, float], ...] = field(default=(), compare=False)
    note: str = ""


class CoreStrip(CompactView):
    """Every core's temperature over the focus view's window, all on one scale, with its peak
    and clock. Click a core to focus on it."""

    ACCESSIBLE_NAME = "Every core"
    TILE_W = 150
    GAP = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window_seconds = 900.0
        self.scale: tuple[float, float] = (0.0, 100.0)

    def load(self, data: ViewData) -> list[Item]:
        clocks = {r.reading.label: r for r in data.rows if r.reading.kind is Kind.CLOCK}
        cores = [row for _, group in core_groups(data.rows) for row in group]
        windows = {row.reading.key: row.stats.window(self.window_seconds, data.now) for row in cores}
        every = [v for points in windows.values() for _, v in points]
        self.scale = value_range(every, Kind.TEMPERATURE) if every else (0.0, 100.0)
        items: list[Item] = []
        for row in cores:
            reading = row.reading
            points = windows[reading.key]
            name = data.labels.get(reading.key, reading.label)
            value = _degrees(reading.value, data.fahrenheit)
            notes = []
            if points:
                notes.append(f"max {_degrees(max(v for _, v in points), data.fahrenheit)}")
            clock = clocks.get(f"{reading.label} clock")
            if clock is not None and clock.reading.value is not None:
                notes.append(format_value(Kind.CLOCK, clock.reading.value))
            items.append(
                CoreItem(
                    reading.key,
                    f"{name}: {format_value(Kind.TEMPERATURE, reading.value, data.fahrenheit)}",
                    name,
                    value,
                    heat_band(reading.value) if reading.value is not None else 0,
                    tuple(points),
                    " · ".join(notes),
                )
            )
        return items

    def columns(self) -> int:
        return self.columns_for(self.width())

    def columns_for(self, width: int) -> int:
        fits = (width + px(self.GAP)) // (px(self.TILE_W) + px(self.GAP))
        return balanced(len(self.items), fits)

    @staticmethod
    def _fonts() -> tuple[QFont, QFont, QFont]:
        return _caps_font(), font(MONO_FONT, 14, QFont.Weight.Bold), font(MONO_FONT, 11)

    def place(self, width: int) -> list[QRectF]:
        columns = self.columns_for(width)
        gap = px(self.GAP)
        tile_w = (width - gap * (columns - 1)) / columns
        title, _, note = self._fonts()
        tile_h = max(QFontMetrics(title).height(), QFontMetrics(self._fonts()[1]).height())
        tile_h += px(34) + QFontMetrics(note).height() + px(28)
        return [
            QRectF((i % columns) * (tile_w + gap), (i // columns) * (tile_h + gap), tile_w, tile_h)
            for i in range(len(self.items))
        ]

    def paint_item(self, painter: QPainter, rect: QRectF, item: Item, theme: Theme) -> None:
        assert isinstance(item, CoreItem)
        warm = item.band >= 2
        edge, _, name_color = theme.heat[item.band]
        painter.setPen(QPen(QColor(edge if warm else theme.edge), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        title_font, value_font, note_font = self._fonts()
        inner = rect.adjusted(px(12), px(10), -px(12), -px(8))
        line_h = max(QFontMetrics(title_font).height(), QFontMetrics(value_font).height())
        top = QRectF(inner.left(), inner.top(), inner.width(), line_h)
        painter.setFont(value_font)
        painter.setPen(QColor(theme.text))
        painter.drawText(top, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, item.value)
        room = inner.width() - QFontMetrics(value_font).horizontalAdvance(item.value) - px(8)
        painter.setFont(title_font)
        painter.setPen(QColor(name_color if warm else theme.muted))
        name = QFontMetrics(title_font).elidedText(item.name.upper(), Qt.TextElideMode.ElideRight, int(room))
        painter.drawText(top, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        chart = QRectF(inner.left(), top.bottom() + px(6), inner.width(), px(34))
        low, high = self.scale
        start = self.data.now - self.window_seconds

        def to_xy(t: float, v: float) -> QPointF:
            return QPointF(
                chart.left() + (t - start) / self.window_seconds * chart.width(),
                chart.bottom() - (v - low) / (high - low) * chart.height(),
            )

        painter.setPen(QPen(QColor(edge), 1.5))
        for segment in drawable_segments(item.points, start, self.window_seconds, int(chart.width()), self.data.gap):
            if len(segment) > 1:
                path = QPainterPath(to_xy(*segment[0]))
                for point in segment[1:]:
                    path.lineTo(to_xy(*point))
                painter.drawPath(path)
        painter.setFont(note_font)
        painter.setPen(QColor(theme.muted))
        note_box = QRectF(inner.left(), chart.bottom() + px(6), inner.width(), QFontMetrics(note_font).height())
        shown = QFontMetrics(note_font).elidedText(item.note, Qt.TextElideMode.ElideRight, int(inner.width()))
        painter.drawText(note_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)


def _degrees(celsius: float | None, fahrenheit: bool) -> str:
    """``46°``: a whole number of degrees, as on the heat map."""
    if celsius is None:
        return "—"
    return f"{to_fahrenheit(celsius) if fahrenheit else celsius:.0f}°"


def _window_text(seconds: float) -> str:
    return f"{seconds / 60:g} minute{'s' if seconds != 60 else ''}"


# ----- the view -----------------------------------------------------------------------------


class FocusView(QWidget):
    """One sensor across the whole window. ``back`` returns to the overview; a click on a core in
    the strip focuses on that core instead (``selected``); ``stepped`` asks for the sensor before
    (-1) or after (1) this one."""

    back = Signal()
    selected = Signal(str)
    stepped = Signal(int)
    window_changed = Signal(float)
    rename_requested = Signal(str)
    pin_toggled = Signal(bool)

    SIDE_BY_SIDE = 900  # px the page needs for the dial to sit beside the chart

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key: str | None = None
        self.window_seconds = 300.0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.back_button = QPushButton("←  Back")
        self.back_button.setObjectName("back")
        self.back_button.setToolTip("Back to every sensor (Esc)")
        self.back_button.clicked.connect(self.back)
        header.addWidget(self.back_button)
        # Through every sensor in the overview's order without going back to it. Ctrl+PgUp/PgDn
        # rather than the arrows, which the core strip and the window buttons use, or PgUp/PgDn,
        # which scroll the page.
        self.step_buttons: dict[int, QPushButton] = {}
        steps = QHBoxLayout()
        steps.setSpacing(4)
        for step, text, keys in ((-1, "‹", "Ctrl+PgUp"), (1, "›", "Ctrl+PgDown")):  # noqa: RUF001
            button = QPushButton(text)
            button.setObjectName("iconButton")
            button.clicked.connect(lambda _=False, step=step: self.stepped.emit(step))
            steps.addWidget(button)
            self.step_buttons[step] = button
            # A window shortcut, so it works wherever the keyboard is; it's off while this page
            # is hidden.
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(lambda step=step: self.stepped.emit(step))
        header.addLayout(steps)
        self.set_neighbours(None, None)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.device = ElidedLabel("", shortest=60)  # both cut short first: no other part of the header can
        self.device.setObjectName("focusDevice")
        self.title = ElidedLabel("", shortest=60)
        self.title.setObjectName("focusTitle")
        titles.addWidget(self.device)
        titles.addWidget(self.title)
        header.addLayout(titles, 1)
        self.window_buttons = QButtonGroup(self)
        self.window_buttons.setExclusive(True)
        for seconds, text in DetailPanel.WINDOWS:
            button = QPushButton(text)
            button.setObjectName("window")
            button.setCheckable(True)
            button.setChecked(seconds == self.window_seconds)
            button.setToolTip(f"Show the last {text}")
            button.clicked.connect(lambda _=False, s=seconds: self._choose_window(s))
            self.window_buttons.addButton(button)
            header.addWidget(button)
        self.rename_button = QPushButton("Rename")
        self.rename_button.setToolTip("Give this sensor your own name")
        self.rename_button.clicked.connect(lambda: self.key is not None and self.rename_requested.emit(self.key))
        header.addWidget(self.rename_button)
        self.pin_button = QPushButton("Pin to tray")
        self.pin_button.setCheckable(True)
        self.pin_button.toggled.connect(self._on_pin_toggled)
        header.addWidget(self.pin_button)
        outer.addLayout(header)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("listArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("listContainer")
        page = QVBoxLayout(body)
        page.setContentsMargins(0, 0, 4, 12)
        page.setSpacing(12)

        self.top = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.top.setSpacing(12)
        dial_panel = Panel()
        dial_box = QVBoxLayout(dial_panel)
        dial_box.setContentsMargins(20, 20, 20, 20)
        dial_box.setSpacing(16)
        self.dial = Dial()
        dial_box.addWidget(self.dial)
        self.outlook = StatGrid(max_columns=2)
        dial_box.addWidget(self.outlook)
        dial_box.addStretch(1)
        self.top.addWidget(dial_panel, 1)

        chart_panel = Panel()
        chart_box = QVBoxLayout(chart_panel)
        chart_box.setContentsMargins(18, 16, 18, 14)
        chart_box.setSpacing(10)
        chart_head = QHBoxLayout()
        self.chart_title = QLabel("")
        self.chart_title.setObjectName("statLabel")
        chart_head.addWidget(self.chart_title)
        chart_head.addStretch(1)
        hint = QLabel("hover to read any point")
        hint.setObjectName("muted")
        chart_head.addWidget(hint)
        chart_box.addLayout(chart_head)
        self.chart = HistoryChart(focus=True)
        self.chart.setMinimumHeight(px(340))
        chart_box.addWidget(self.chart, 1)
        legend = QLabel("—  reading      - - -  1-minute average")
        legend.setObjectName("muted")
        chart_box.addWidget(legend)
        self.top.addWidget(chart_panel, 2)
        page.addLayout(self.top)

        self.stats = StatGrid()
        page.addWidget(self.stats)

        self.cores_panel = Panel()
        cores_box = QVBoxLayout(self.cores_panel)
        cores_box.setContentsMargins(18, 16, 18, 18)
        cores_box.setSpacing(12)
        cores_head = QHBoxLayout()
        cores_title = QLabel("Every core")
        cores_title.setObjectName("sectionTitle")
        cores_head.addWidget(cores_title)
        self.cores_note = QLabel("")
        self.cores_note.setObjectName("muted")
        cores_head.addWidget(self.cores_note)
        cores_head.addStretch(1)
        cores_box.addLayout(cores_head)
        self.cores = CoreStrip()
        self.cores.selected.connect(self.selected)
        cores_box.addWidget(self.cores)
        page.addWidget(self.cores_panel)
        page.addStretch(1)
        self.scroll_area.setWidget(body)
        outer.addWidget(self.scroll_area, 1)

    def _choose_window(self, seconds: float) -> None:
        self.set_window(seconds)
        self.window_changed.emit(self.window_seconds)

    def set_window(self, seconds: float) -> None:
        """Show ``seconds`` of history (snapped to an offered window), without announcing it."""
        self.window_seconds = DetailPanel.nearest_window(seconds)
        for button, (window, _) in zip(self.window_buttons.buttons(), DetailPanel.WINDOWS, strict=True):
            button.setChecked(window == self.window_seconds)

    def _on_pin_toggled(self, pinned: bool) -> None:
        self.pin_button.setText("Pinned to tray" if pinned else "Pin to tray")
        self.pin_toggled.emit(pinned)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        wide = self.width() >= px(self.SIDE_BY_SIDE)
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        if self.top.direction() != direction:
            self.top.setDirection(direction)

    def set_neighbours(self, previous: str | None, following: str | None) -> None:
        """Name the sensors a step either way; None turns that step off."""
        for step, name, word, keys in ((-1, previous, "Previous", "Ctrl+PgUp"), (1, following, "Next", "Ctrl+PgDn")):
            button = self.step_buttons[step]
            button.setEnabled(name is not None)
            button.setAccessibleName(f"{word} sensor")
            button.setToolTip(f"{word}: {name}  ({keys})" if name is not None else f"{word} sensor")

    def show_row(
        self,
        row: Row,
        cores: ViewData | None,
        now: float,
        fahrenheit: bool,
        gap: float,
        to_wall: Callable[[float], float],
        pinned: bool,
        can_pin: bool,
    ) -> None:
        """Show ``row``; ``cores`` is its CPU's sensors when it's a CPU sensor, else None."""
        reading = row.reading
        self.key = reading.key
        self.device.setText((reading.origin or reading.device).upper())
        self.title.setText(reading.label)
        self.pin_button.setEnabled(can_pin)
        self.pin_button.setToolTip(
            "Show this sensor's value as its own icon in the system tray"
            if can_pin
            else "No system tray is available on this desktop"
        )
        self.pin_button.blockSignals(True)  # reflecting state, not a click
        self.pin_button.setChecked(pinned)
        self.pin_button.setText("Pinned to tray" if pinned else "Pin to tray")
        self.pin_button.blockSignals(False)

        self.dial.set_reading(reading, fahrenheit)
        room, room_note = headroom(reading, fahrenheit)
        trend = format_trend(reading.kind, row.stats.trend_per_minute(now), fahrenheit)
        theme = current_theme()
        trend_color = theme.accent_text if trend.startswith("↓") else theme.warning if trend.startswith("↑") else ""
        self.outlook.set_stats(
            [
                Stat("Headroom", room, room_note),
                Stat("Trend", trend, trend_word(trend, reading.kind), trend_color),
            ]
        )
        self.chart_title.setText(f"LAST {_window_text(self.window_seconds).upper()}")
        self.chart.set_data(row, self.window_seconds, fahrenheit, gap, now, to_wall)
        self.stats.set_stats(focus_stats(row, now, fahrenheit, to_wall))

        self.cores.window_seconds = self.window_seconds
        if cores is not None:
            self.cores.set_data(cores)
        shown = cores is not None and len(self.cores.items) >= 2
        self.cores_panel.setVisible(shown)
        if shown:
            self.cores_note.setText(f"same {_window_text(self.window_seconds)}, same scale")
            self.cores.set_selected(reading.key)
