"""Custom-painted pieces of the window: row sparklines, the history chart, the detail panel."""

import bisect
import math
import time
from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from corewatch.gui.theme import current_theme
from corewatch.model import (
    Kind,
    Row,
    format_delta,
    format_duration,
    format_limit,
    format_trend,
    format_value,
    to_fahrenheit,
)

# The smallest vertical span a chart shows, so a steady sensor draws a calm flat line
# instead of magnifying sensor noise to full height.
MIN_SPAN = {
    Kind.TEMPERATURE: 4.0,
    Kind.VOLTAGE: 0.05,
    Kind.FAN: 100.0,
    Kind.FAN_DUTY: 10.0,
    Kind.POWER: 5.0,
    Kind.CURRENT: 0.5,
    Kind.CLOCK: 400.0,
    Kind.LOAD: 10.0,
}


def value_range(values: Sequence[float], kind: Kind) -> tuple[float, float]:
    low, high = min(values), max(values)
    span = max(high - low, MIN_SPAN[kind])
    middle = (low + high) / 2
    low, high = middle - span / 2 * 1.15, middle + span / 2 * 1.15
    if kind in (Kind.LOAD, Kind.FAN_DUTY, Kind.FAN, Kind.POWER, Kind.CLOCK) and low < 0:
        low, high = 0.0, high - low  # these can't go negative; keep the span
    return low, high


def nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Round-numbered axis ticks covering [low, high], e.g. 45, 50, 55, 60."""
    raw = (high - low) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw * (1 - 1e-9))
    # The small epsilons absorb float error (1.18 / 0.02 is 58.99999...).
    first = math.floor(low / step + 1e-9)
    last = math.ceil(high / step - 1e-9)
    return [round(i * step, 10) for i in range(first, last + 1)]


# Window length -> spacing of the time labels under the chart, in seconds.
TIME_TICKS = {60.0: 15.0, 300.0: 60.0, 900.0: 300.0}


def decimate(
    points: Sequence[tuple[float, float]], start: float, seconds: float, pixels: int
) -> list[tuple[float, float]]:
    """At most two points per pixel column (its lowest and highest), so spikes survive.

    Fifteen minutes at 0.25 s is 3,600 points for a chart a few hundred pixels wide;
    drawing them all costs tens of milliseconds per repaint for no visible gain.
    """
    if len(points) <= pixels * 2:
        return list(points)
    columns: dict[int, list[tuple[float, float]]] = {}
    for point in points:
        columns.setdefault(int((point[0] - start) / seconds * pixels), []).append(point)
    kept: list[tuple[float, float]] = []
    for column in sorted(columns):
        bucket = columns[column]
        lowest = min(bucket, key=lambda p: p[1])
        highest = max(bucket, key=lambda p: p[1])
        kept.extend(sorted({lowest, highest}, key=lambda p: p[0]))
    return kept


def drawable_segments(
    points: Sequence[tuple[float, float]], start: float, seconds: float, pixels: int, gap: float
) -> list[list[tuple[float, float]]]:
    """Unbroken stretches of a series, each thinned to what ``pixels`` columns can show.

    Gaps must be found on the raw readings: once thinned, neighbouring points can be
    further apart than ``gap`` even though no reading was missed.
    """
    return [decimate(raw, start, seconds, max(1, pixels)) for raw in split_on_gaps(points, gap)]


def split_on_gaps(points: Sequence[tuple[float, float]], gap: float) -> list[list[tuple[float, float]]]:
    """Break a series wherever samples are missing for longer than ``gap`` seconds."""
    segments: list[list[tuple[float, float]]] = []
    for point in points:
        if segments and point[0] - segments[-1][-1][0] <= gap:
            segments[-1].append(point)
        else:
            segments.append([point])
    return segments


class HistoryChart(QWidget):
    """A line chart of one sensor with gridlines, limits and lowest/highest markers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.row: Row | None = None
        self.window_seconds = 300.0
        self.fahrenheit = False
        self.gap = 5.0
        self.now = 0.0
        self.to_wall: Callable[[float], float] = lambda t: t
        self.hover_x: float | None = None
        self.setMouseTracking(True)

    def set_data(
        self,
        row: Row | None,
        window_seconds: float,
        fahrenheit: bool,
        gap: float,
        now: float,
        to_wall: Callable[[float], float] | None = None,
    ) -> None:
        self.row, self.window_seconds, self.fahrenheit, self.gap, self.now = row, window_seconds, fahrenheit, gap, now
        if to_wall is not None:
            self.to_wall = to_wall
        self.update()

    def plot_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(64, 10, -14, -24)

    def hovered_point(self) -> tuple[float, float] | None:
        """The reading nearest the mouse, by time, among those in view."""
        if self.row is None or self.hover_x is None:
            return None
        plot = self.plot_rect()
        if not plot.left() <= self.hover_x <= plot.right() or plot.width() <= 0:
            return None
        points = self.row.stats.window(self.window_seconds, self.now)
        if not points:
            return None
        target = self.now - self.window_seconds + (self.hover_x - plot.left()) / plot.width() * self.window_seconds
        index = bisect.bisect_left(points, target, key=lambda p: p[0])
        nearby = points[max(0, index - 1) : index + 1]
        return min(nearby, key=lambda p: abs(p[0] - target))

    def hover_text(self, point: tuple[float, float] | None = None) -> tuple[str, str] | None:
        """(value, when) for the hovered reading, e.g. ("46.0 °C", "20:55:14 · 32 s ago")."""
        point = point if point is not None else self.hovered_point()
        if point is None or self.row is None:
            return None
        ago = self.now - point[0]
        when = time.strftime("%H:%M:%S", time.localtime(self.to_wall(point[0])))
        return format_value(self.row.reading.kind, point[1], self.fahrenheit), (
            f"{when} · {format_duration(ago)} ago" if ago >= 0.5 else f"{when} · now"
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self.hover_x = event.position().x()
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self.hover_x = None
        self.update()

    def _fahrenheit_axis(self, kind: Kind) -> bool:
        return kind is Kind.TEMPERATURE and self.fahrenheit

    def _display_range(self, kind: Kind, low: float, high: float) -> tuple[float, float]:
        if self._fahrenheit_axis(kind):
            return to_fahrenheit(low), to_fahrenheit(high)
        return low, high

    def _from_display(self, kind: Kind, value: float) -> float:
        return (value - 32) * 5 / 9 if self._fahrenheit_axis(kind) else value

    def paintEvent(self, event: object) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        small = QFont(self.font())
        small.setPixelSize(11)
        painter.setFont(small)
        plot = self.plot_rect()
        points = self.row.stats.window(self.window_seconds, self.now) if self.row else []
        if self.row is None or not points:
            painter.setPen(QColor(theme.muted))
            message = "Collecting readings…" if self.row else "Select a sensor to see its history"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)
            return
        kind = self.row.reading.kind
        low, high = value_range([v for _, v in points], kind)
        start = self.now - self.window_seconds

        def to_xy(t: float, v: float) -> QPointF:
            return QPointF(
                plot.left() + (t - start) / self.window_seconds * plot.width(),
                plot.bottom() - (v - low) / (high - low) * plot.height(),
            )

        # Horizontal gridlines on round values; the axis snaps to the outer ticks.
        ticks = nice_ticks(*self._display_range(kind, low, high))
        low, high = self._from_display(kind, ticks[0]), self._from_display(kind, ticks[-1])
        grid_pen = QPen(QColor(theme.border), 1)
        for tick in ticks:
            y = to_xy(start, self._from_display(kind, tick)).y()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(theme.muted))
            label = format_value(kind, self._from_display(kind, tick), self.fahrenheit)
            painter.drawText(QRectF(0, y - 8, plot.left() - 8, 16), Qt.AlignmentFlag.AlignRight, label)

        # Time labels on whole steps back from now.
        step = TIME_TICKS.get(self.window_seconds, self.window_seconds / 4)
        ago = self.window_seconds
        while ago >= 0:
            x = plot.left() + (1 - ago / self.window_seconds) * plot.width()
            if ago == 0:
                text = "now"
            elif self.window_seconds >= 120:
                text = f"−{ago / 60:g} min"
            else:
                text = f"−{ago:g} s"
            box = QRectF(x - 40, plot.bottom() + 6, 80, 14)
            align = Qt.AlignmentFlag.AlignHCenter
            if ago == self.window_seconds:
                box.moveLeft(x)
                align = Qt.AlignmentFlag.AlignLeft
            elif ago == 0:
                box.moveRight(x)
                align = Qt.AlignmentFlag.AlignRight
            painter.drawText(box, align, text)
            ago -= step

        # Limit lines, only when they fall inside the visible range.
        reading = self.row.reading
        for limit, color in (
            (reading.high, theme.warning),
            (reading.crit, theme.critical),
            (reading.low, theme.warning),
            (reading.cap, theme.muted),  # informational: drawn, never alarming
        ):
            if limit is not None and low <= limit <= high:
                pen = QPen(QColor(color), 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                y = to_xy(start, limit).y()
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        # The series itself, with a soft fill underneath.
        line_color = theme.status(reading.status)
        fill = QColor(line_color)
        fill.setAlpha(36)
        for segment in drawable_segments(points, start, self.window_seconds, int(plot.width()) // 2, self.gap):
            if len(segment) == 1:  # a lone reading between gaps: a dot, since a one-point path draws nothing
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(line_color)
                painter.drawEllipse(to_xy(*segment[0]), 2, 2)
                continue
            path = QPainterPath(to_xy(*segment[0]))
            for point in segment[1:]:
                path.lineTo(to_xy(*point))
            area = QPainterPath(path)
            area.lineTo(to_xy(segment[-1][0], low))
            area.lineTo(to_xy(segment[0][0], low))
            area.closeSubpath()
            # The soft fill doesn't need smooth edges (the line on top has them), and
            # antialiasing a 1,000-point area costs ~10 ms a repaint.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.fillPath(area, fill)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(line_color, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)  # an earlier dot may have left a fill brush set
            painter.drawPath(path)

        # Lowest and highest points in view.
        lowest = min(points, key=lambda p: p[1])
        highest = max(points, key=lambda p: p[1])
        for point, text in ((highest, "max"), (lowest, "min")):
            center = to_xy(*point)
            painter.setPen(QPen(QColor(theme.surface), 2))
            painter.setBrush(line_color)
            painter.drawEllipse(center, 4, 4)
            painter.setPen(QColor(theme.muted))
            label = f"{text} {format_value(kind, point[1], self.fahrenheit)}"
            above = text == "max"
            box = QRectF(center.x() - 60, center.y() + (-20 if above else 6), 120, 14)
            box.moveLeft(min(max(box.left(), plot.left()), plot.right() - box.width()))
            if lowest is not highest or above:
                painter.drawText(box, Qt.AlignmentFlag.AlignHCenter, label)

        # Hover: a guide line, the nearest reading highlighted, and its value and time.
        hovered = self.hovered_point()
        hover = self.hover_text(hovered) if hovered is not None else None
        if hovered is not None and hover is not None:
            center = to_xy(*hovered)
            painter.setPen(QPen(QColor(theme.muted), 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(center.x(), plot.top()), QPointF(center.x(), plot.bottom()))
            painter.setPen(QPen(QColor(theme.surface), 2))
            painter.setBrush(line_color)
            painter.drawEllipse(center, 5, 5)
            value_text, when_text = hover
            bold = QFont(small)
            bold.setPixelSize(13)
            bold.setBold(True)
            width = max(
                QFontMetrics(bold).horizontalAdvance(value_text), QFontMetrics(small).horizontalAdvance(when_text)
            )
            bubble = QRectF(center.x() + 12, center.y() - 40, width + 20, 38)
            if bubble.right() > plot.right():  # flip to the left of the point near the right edge
                bubble.moveRight(center.x() - 12)
            bubble.moveTop(min(max(bubble.top(), plot.top()), plot.bottom() - bubble.height()))
            painter.setPen(QPen(QColor(theme.border), 1))
            painter.setBrush(QColor(theme.raised))
            painter.drawRoundedRect(bubble, 6, 6)
            painter.setFont(bold)
            painter.setPen(theme.value_color(self.row.reading.status))
            painter.drawText(
                bubble.adjusted(10, 3, -10, -18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value_text
            )
            painter.setFont(small)
            painter.setPen(QColor(theme.muted))
            painter.drawText(
                bubble.adjusted(10, 20, -10, -3), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, when_text
            )
        painter.end()


def _clock(timestamp: float | None, to_wall: Callable[[float], float]) -> str:
    return time.strftime("%H:%M:%S", time.localtime(to_wall(timestamp))) if timestamp is not None else ""


class DetailPanel(QFrame):
    """The lower half of the window: a big chart and every statistic for one sensor."""

    window_changed = Signal(float)

    WINDOWS = ((60.0, "1 min"), (300.0, "5 min"), (900.0, "15 min"))
    STATS = (
        ("now", "Now", "The latest reading"),
        ("average", "Average", "Average of every reading since you started watching (or last reset)"),
        ("average_1m", "Average, last minute", "Average of the readings from the last 60 seconds"),
        ("average_5m", "Average, last 5 min", "Average of the readings from the last 5 minutes"),
        ("lowest", "Lowest", "The lowest reading so far, and when it happened"),
        ("highest", "Highest", "The highest reading so far, and when it happened"),
        (
            "variation",
            "Variation (±)",
            "How far readings usually stray from the average (standard deviation). Small means steady.",
        ),
        ("trend", "Trend", "How fast the reading is changing, based on the last 60 seconds"),
        (
            "past_limit",
            "Time past a limit",
            "How long the sensor has spent past its warning limit: too hot, a fan too slow, or a voltage out of range",
        ),
        ("critical", "Time at critical", "How long the sensor has spent at or above its critical limit"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.window_seconds = 300.0
        self._value_color = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("No sensor selected")
        self.title.setObjectName("detailTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("muted")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        self.value = QLabel("")
        self.value.setObjectName("detailValue")
        header.addWidget(self.value)
        header.addSpacing(16)
        self.window_buttons = QButtonGroup(self)
        self.window_buttons.setExclusive(True)
        for seconds, text in self.WINDOWS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setChecked(seconds == self.window_seconds)
            button.setToolTip(f"Show the last {text}")
            button.clicked.connect(lambda _=False, s=seconds: self.set_window(s))
            self.window_buttons.addButton(button)
            header.addWidget(button)
        layout.addLayout(header)

        self.chart = HistoryChart()
        layout.addWidget(self.chart, 1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        self.stat_values: dict[str, QLabel] = {}
        for i, (key, label, tip) in enumerate(self.STATS):
            box = QVBoxLayout()
            box.setSpacing(1)
            name = QLabel(label)
            name.setObjectName("statLabel")
            name.setToolTip(tip)
            value = QLabel("—")
            value.setObjectName("statValue")
            value.setToolTip(tip)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(name)
            box.addWidget(value)
            grid.addLayout(box, i // 4, i % 4)
            self.stat_values[key] = value
        for column in range(4):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        self.limits = QLabel("")
        self.limits.setObjectName("muted")
        layout.addWidget(self.limits)

    @classmethod
    def nearest_window(cls, seconds: float) -> float:
        """Snap any value (say, from a hand-edited settings file) to one of the offered windows."""
        if not math.isfinite(seconds):
            return 300.0
        return min((window for window, _ in cls.WINDOWS), key=lambda window: abs(window - seconds))

    def set_window(self, seconds: float) -> None:
        seconds = self.nearest_window(seconds)
        self.window_seconds = seconds
        for button, (window, _) in zip(self.window_buttons.buttons(), self.WINDOWS, strict=True):
            button.setChecked(window == seconds)
        self.window_changed.emit(seconds)

    def show_row(
        self,
        row: Row | None,
        now: float,
        fahrenheit: bool,
        gap: float,
        to_wall: Callable[[float], float] = lambda t: t,
    ) -> None:
        self.chart.set_data(row, self.window_seconds, fahrenheit, gap, now, to_wall)
        if row is None:
            self.title.setText("No sensor selected")
            self.subtitle.setText("Pick a sensor in the list above")
            self.value.setText("")
            self.limits.setText("")
            for label in self.stat_values.values():
                label.setText("—")
            return
        reading, stats = row.reading, row.stats
        kind, f = reading.kind, fahrenheit
        theme = current_theme()
        self.title.setText(reading.label)
        self.subtitle.setText(reading.device)
        self.value.setText(format_value(kind, reading.value, f))
        color = theme.value_color(reading.status).name()
        if color != self._value_color:  # restyling re-polishes the label; only do it on a change
            self._value_color = color
            self.value.setStyleSheet(f"color: {color};")
        has_limits = reading.high is not None or reading.low is not None or reading.crit is not None
        values = {
            "now": format_value(kind, reading.value, f),
            "average": format_value(kind, stats.average, f),
            "average_1m": format_value(kind, stats.window_average(60, now), f),
            "average_5m": format_value(kind, stats.window_average(300, now), f),
            "lowest": f"{format_value(kind, stats.minimum, f)}  {_clock(stats.minimum_at, to_wall)}".strip(),
            "highest": f"{format_value(kind, stats.maximum, f)}  {_clock(stats.maximum_at, to_wall)}".strip(),
            "variation": format_delta(kind, stats.stddev, f, signed=False),
            "trend": format_trend(kind, stats.trend_per_minute(now), f),
            "past_limit": format_duration(stats.seconds_warning + stats.seconds_critical) if has_limits else "no limit",
            "critical": format_duration(stats.seconds_critical) if reading.crit is not None else "no limit",
        }
        for key, text in values.items():
            self.stat_values[key].setText(text)
        limits = format_limit(reading, f)
        self.limits.setText(f"Limits reported by the hardware: {limits}" if limits else "")
