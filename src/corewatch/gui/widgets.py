"""Custom-painted pieces of the window: row sparklines, the history chart, the detail panel."""

import bisect
import math
import time
from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QMargins, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from corewatch.gui.theme import DISPLAY_FONT, MONO_FONT, Theme, current_theme, font, paint_card, px
from corewatch.model import (
    Kind,
    Row,
    cap_word,
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
    Kind.THROUGHPUT: 50_000.0,
}


def value_range(values: Sequence[float], kind: Kind) -> tuple[float, float]:
    low, high = min(values), max(values)
    span = max(high - low, MIN_SPAN[kind])
    middle = (low + high) / 2
    low, high = middle - span / 2 * 1.15, middle + span / 2 * 1.15
    if kind in (Kind.LOAD, Kind.FAN_DUTY, Kind.FAN, Kind.POWER, Kind.CLOCK, Kind.THROUGHPUT) and low < 0:
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


def rolling_average(points: Sequence[tuple[float, float]], seconds: float) -> list[tuple[float, float]]:
    """Each reading replaced by the average of the readings in the ``seconds`` up to it."""
    averaged: list[tuple[float, float]] = []
    total, first = 0.0, 0
    for t, value in points:
        total += value
        while points[first][0] < t - seconds:
            total -= points[first][1]
            first += 1
        averaged.append((t, total / (len(averaged) + 1 - first)))
    return averaged


def split_on_gaps(points: Sequence[tuple[float, float]], gap: float) -> list[list[tuple[float, float]]]:
    """Break a series wherever samples are missing for longer than ``gap`` seconds."""
    segments: list[list[tuple[float, float]]] = []
    for point in points:
        if segments and point[0] - segments[-1][-1][0] <= gap:
            segments[-1].append(point)
        else:
            segments.append([point])
    return segments


QWIDGET_MAX = 16777215  # Qt's QWIDGETSIZE_MAX: no maximum
SPARKLINE_SECONDS = 60.0


def recent(row: Row, seconds: float, now: float) -> list[tuple[float, float]]:
    """The last ``seconds`` of history, walking back from the newest point only as far as needed."""
    cutoff = now - seconds
    points = []
    for point in reversed(row.stats.history):
        if point[0] < cutoff:
            break
        points.append(point)
    points.reverse()
    return points


def draw_sparkline(painter: QPainter, rect: QRectF, row: Row, now: float, gap: float, theme: Theme) -> None:
    points = recent(row, SPARKLINE_SECONDS, now)
    if not points:
        return
    low, high = value_range([v for _, v in points], row.reading.kind)
    color = theme.status(row.reading.status)

    def to_xy(t: float, v: float) -> QPointF:
        x = rect.left() + (t - (now - SPARKLINE_SECONDS)) / SPARKLINE_SECONDS * rect.width()
        return QPointF(x, rect.bottom() - (v - low) / (high - low) * rect.height())

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(color, 1.5))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # One low/high pair per two pixels is all a 72 px line can show.
    for segment in drawable_segments(points, now - SPARKLINE_SECONDS, SPARKLINE_SECONDS, int(rect.width()) // 2, gap):
        if len(segment) == 1:  # a lone reading between gaps: a dot, since a one-point path draws nothing
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(to_xy(*segment[0]), 1.5, 1.5)
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            continue
        path = QPainterPath(to_xy(*segment[0]))
        for point in segment[1:]:
            path.lineTo(to_xy(*point))
        painter.drawPath(path)
    if now - points[-1][0] <= gap:  # only mark "now" if the sensor is still reporting
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(to_xy(*points[-1]), 2.2, 2.2)
    painter.restore()


class HistoryChart(QWidget):
    """A line chart of one sensor with gridlines, limits and lowest/highest markers.

    ``focus`` is the focus view's larger chart: its scale always takes in the sensor's limits,
    which are named at the right edge with the critical zone shaded, and a dashed line follows
    the 1-minute average."""

    AVERAGE_SECONDS = 60.0

    def __init__(self, parent: QWidget | None = None, focus: bool = False) -> None:
        super().__init__(parent)
        self.focus = focus
        self.setMinimumHeight(150)
        self.row: Row | None = None
        self.window_seconds = 300.0
        self.fahrenheit = False
        self.gap = 5.0
        self.now = 0.0
        self.to_wall: Callable[[float], float] = lambda t: t
        self.hover_x: float | None = None
        self.axis_width = 56  # room for the value labels left of the plot, widened to fit them
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
        """The plot, inside room for its labels, which grow with the desktop's text size."""
        text_height = QFontMetrics(font(MONO_FONT, 11)).height()
        top = max(10, (text_height + 1) // 2)  # the top value label is centred on the top line
        return QRectF(self.rect()).adjusted(self.axis_width + 8, top, -14, -(text_height + 10))

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

    def scale(self, points: Sequence[tuple[float, float]]) -> tuple[float, float]:
        """The span the readings are drawn against (before rounding to whole ticks)."""
        assert self.row is not None
        reading = self.row.reading
        values = [v for _, v in points]
        if self.focus:  # the limits stay in view, so you see how close the sensor runs to them
            values += [limit for limit in (reading.low, reading.high, reading.crit, reading.cap) if limit is not None]
        return value_range(values, reading.kind)

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
        small = font(MONO_FONT, 11)
        metrics = QFontMetrics(small)
        text_height = metrics.height()
        painter.setFont(small)
        points = self.row.stats.window(self.window_seconds, self.now) if self.row else []
        if self.row is None or not points:
            painter.setPen(QColor(theme.muted))
            message = "Collecting readings…" if self.row else "Select a sensor to see its history"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)
            return
        kind = self.row.reading.kind
        reading = self.row.reading
        low, high = self.scale(points)
        start = self.now - self.window_seconds

        # Horizontal gridlines on round values; the axis snaps to the outer ticks, and the plot
        # starts right of the widest label.
        ticks = nice_ticks(*self._display_range(kind, low, high))
        low, high = self._from_display(kind, ticks[0]), self._from_display(kind, ticks[-1])
        labels = [format_value(kind, self._from_display(kind, tick), self.fahrenheit) for tick in ticks]
        self.axis_width = max(px(56), *(metrics.horizontalAdvance(label) for label in labels))
        plot = self.plot_rect()

        def to_xy(t: float, v: float) -> QPointF:
            return QPointF(
                plot.left() + (t - start) / self.window_seconds * plot.width(),
                plot.bottom() - (v - low) / (high - low) * plot.height(),
            )

        grid_pen = QPen(QColor(theme.border), 1)
        for tick, label in zip(ticks, labels, strict=True):
            y = to_xy(start, self._from_display(kind, tick)).y()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(theme.muted))
            box = QRectF(0, y - text_height / 2, plot.left() - 8, text_height)
            painter.drawText(box, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)

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
            width = metrics.horizontalAdvance(text) + 8
            box = QRectF(x - width / 2, plot.bottom() + 6, width, text_height)
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
        if self.focus and reading.crit is not None and low <= reading.crit <= high:
            zone = QColor(theme.critical)
            zone.setAlpha(14)
            painter.fillRect(QRectF(plot.topLeft(), QPointF(plot.right(), to_xy(start, reading.crit).y())), zone)
        named = font(DISPLAY_FONT, 9, QFont.Weight.DemiBold, 1.5)
        for limit, color, name in (
            (reading.high, theme.warning, "high"),
            (reading.crit, theme.critical, "critical"),
            (reading.low, theme.warning, "low"),
            (reading.cap, theme.muted, cap_word(reading.kind)),  # informational: drawn, never alarming
        ):
            if limit is not None and low <= limit <= high:
                pen = QPen(QColor(color), 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                y = to_xy(start, limit).y()
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                if self.focus:
                    painter.setFont(named)
                    label = f"{name} {format_value(kind, limit, self.fahrenheit)}".upper()
                    height = QFontMetrics(named).height()
                    box = QRectF(plot.left(), y - height - 2, plot.width() - 6, height)
                    if box.top() < plot.top():  # no room above the line: write it under
                        box.moveTop(y + 2)
                    painter.drawText(box, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
                    painter.setFont(small)

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

        if self.focus:  # the 1-minute average, dashed, over the reading
            # Average from a minute before the window, so its left edge is as steady as the rest.
            earlier = self.row.stats.window(self.window_seconds + self.AVERAGE_SECONDS, self.now)
            averaged = [p for p in rolling_average(earlier, self.AVERAGE_SECONDS) if p[0] >= start]
            pen = QPen(QColor(theme.accent_text), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for segment in drawable_segments(averaged, start, self.window_seconds, int(plot.width()) // 2, self.gap):
                if len(segment) > 1:
                    path = QPainterPath(to_xy(*segment[0]))
                    for point in segment[1:]:
                        path.lineTo(to_xy(*point))
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
            if self.focus and text == "max":  # the peak, and when it was
                label = f"peak {format_value(kind, point[1], self.fahrenheit)} · {_clock(point[0], self.to_wall)}"
            above = text == "max"
            width = metrics.horizontalAdvance(label) + 8
            box = QRectF(center.x() - width / 2, center.y() + (-(text_height + 6) if above else 6), width, text_height)
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
            bold = font(MONO_FONT, 13, QFont.Weight.Bold)
            bold_height = QFontMetrics(bold).height()
            width = max(QFontMetrics(bold).horizontalAdvance(value_text), metrics.horizontalAdvance(when_text))
            height = bold_height + text_height + 6
            bubble = QRectF(center.x() + 12, center.y() - height - 2, width + 20, height)
            if bubble.right() > plot.right():  # flip to the left of the point near the right edge
                bubble.moveRight(center.x() - 12)
            bubble.moveTop(min(max(bubble.top(), plot.top()), plot.bottom() - bubble.height()))
            painter.setPen(QPen(QColor(theme.border), 1))
            painter.setBrush(QColor(theme.raised))
            painter.drawRect(bubble)
            painter.setFont(bold)
            painter.setPen(theme.value_color(self.row.reading.status))
            value_box = QRectF(bubble.left() + 10, bubble.top() + 3, width, bold_height)
            painter.drawText(value_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value_text)
            painter.setFont(small)
            painter.setPen(QColor(theme.muted))
            when_box = QRectF(bubble.left() + 10, bubble.top() + 3 + bold_height, width, text_height)
            painter.drawText(when_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, when_text)
        painter.end()


class ElidedLabel(QLabel):
    """A label that ends in "…" when it's squeezed, so a long name never sets its card's
    narrowest width (a board called "ROG STRIX Z790-A GAMING WIFI" would keep two cards from
    ever fitting side by side)."""

    def __init__(self, text: str = "", shortest: int = 120, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.shortest = shortest  # px it still shows before it's all "…"

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), px(self.shortest)), hint.height())

    def shown_text(self) -> str:
        return self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width())

    def _sync_tooltip(self) -> None:
        self.setToolTip(self.text() if self.shown_text() != self.text() else "")  # the whole name, when cut

    def setText(self, text: str) -> None:
        super().setText(text)
        self._sync_tooltip()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_tooltip()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.shown_text()
        self.style().drawItemText(
            painter,
            rect,
            int(self.alignment() | Qt.AlignmentFlag.AlignVCenter),
            self.palette(),
            self.isEnabled(),
            text,
            QPalette.ColorRole.WindowText,
        )
        painter.end()


def expand_icon(color: str) -> QIcon:
    """Four corners pointing outwards: open something across the whole window."""
    icon = QIcon()
    for size in (16, 32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), size / 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
        edge, arm = size * 0.12, size * 0.22
        for x, y, dx, dy in (
            (edge, edge, 1, 1),
            (size - edge, edge, -1, 1),
            (edge, size - edge, 1, -1),
            (size - edge, size - edge, -1, -1),
        ):
            corner = QPointF(x, y)
            painter.drawLine(corner, QPointF(x + dx * arm, y))
            painter.drawLine(corner, QPointF(x, y + dy * arm))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def chevron_icon(color: str, up: bool) -> QIcon:
    """A chevron pointing up (the sensor before) or down (the one after)."""
    icon = QIcon()
    for size in (16, 32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), size / 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        rise = size * 0.16 * (1 if up else -1)
        middle = size / 2
        tip = QPointF(middle, middle - rise)
        painter.drawLine(QPointF(size * 0.22, middle + rise), tip)
        painter.drawLine(tip, QPointF(size * 0.78, middle + rise))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def pencil_icon(color: str) -> QIcon:
    """A pencil, point down to the left: edit the name beside it."""
    icon = QIcon()
    for size in (16, 32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), size / 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.translate(size / 2, size / 2)
        painter.rotate(-45)
        half, body, tip = size * 0.11, size * 0.30, size * 0.44
        painter.drawRect(QRectF(-body, -half, body * 2, half * 2))  # the shaft
        painter.drawLine(QPointF(body - size * 0.1, -half), QPointF(body - size * 0.1, half))  # the eraser's band
        painter.drawLine(QPointF(-body, -half), QPointF(-tip, 0))  # the point
        painter.drawLine(QPointF(-body, half), QPointF(-tip, 0))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _clock(timestamp: float | None, to_wall: Callable[[float], float]) -> str:
    return time.strftime("%H:%M:%S", time.localtime(to_wall(timestamp))) if timestamp is not None else ""


class Sparkline(QWidget):
    """A sensor's last minute, small, for the shut drawer's strip."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.row: Row | None = None
        self.now, self.gap = 0.0, 5.0
        self.setFixedSize(px(96), px(28))

    def set_row(self, row: Row | None, now: float, gap: float) -> None:
        self.row, self.now, self.gap = row, now, gap
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.row is None:
            return
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(2, 4, -4, -4)
        draw_sparkline(painter, rect, self.row, self.now, self.gap, current_theme())
        painter.end()


class DetailPanel(QFrame):
    """The lower half of the window: a big chart and every statistic for one sensor."""

    window_changed = Signal(float)
    pin_toggled = Signal(bool)
    focus_requested = Signal()
    expanded_changed = Signal(bool)  # opened or shut with a click on its strip or its arrow

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
        self.expanded = False  # a drawer: shut, it's a one-line strip under the cards
        layout = QVBoxLayout(self)
        self._margins = QMargins(16, 14, 16, 14)
        layout.setContentsMargins(self._margins)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = ElidedLabel("No sensor selected", shortest=90)
        self.title.setObjectName("detailTitle")
        self.subtitle = ElidedLabel("", shortest=90)
        self.subtitle.setObjectName("muted")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        # The titles take all the spare width, so long names aren't clipped.
        header.addLayout(titles, 1)
        self.value = QLabel("")
        self.value.setObjectName("detailValue")
        header.addWidget(self.value)
        self.spark = Sparkline()  # the last minute, while the chart is folded away
        header.addWidget(self.spark)
        header.addSpacing(16)
        self.pin_button = QPushButton("Pin to tray")
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip("Show this sensor's value as its own icon in the system tray")
        self.pin_button.toggled.connect(self._on_pin_toggled)
        header.addWidget(self.pin_button)
        self.focus_button = QPushButton()  # an icon, so the header still fits the narrowest window
        self.focus_button.setObjectName("iconButton")
        self.focus_button.setIcon(expand_icon(current_theme().muted))
        self.focus_button.setAccessibleName("Focus view")
        self.focus_button.setToolTip("Focus view: this sensor across the whole window (or double-click any sensor)")
        self.focus_button.clicked.connect(self.focus_requested)
        header.addWidget(self.focus_button)
        self.toggle = QToolButton()
        self.toggle.setObjectName("drawerToggle")
        self.toggle.clicked.connect(lambda: self.set_expanded(not self.expanded, user=True))
        header.addWidget(self.toggle)
        self.header = QWidget()
        self.header.setLayout(header)
        layout.addWidget(self.header)

        # The chart's minutes get a row of their own over it: in the header, beside a wide value
        # like "326.2 KB/s", they'd make the open drawer wider than the narrowest window.
        self.window_row = QWidget()
        windows = QHBoxLayout(self.window_row)
        windows.setContentsMargins(0, 0, 0, 0)
        windows.addStretch(1)
        self.window_buttons = QButtonGroup(self)
        self.window_buttons.setExclusive(True)
        for seconds, text in self.WINDOWS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setChecked(seconds == self.window_seconds)
            button.setToolTip(f"Show the last {text}")
            button.clicked.connect(lambda _=False, s=seconds: self.set_window(s))
            self.window_buttons.addButton(button)
            windows.addWidget(button)
        layout.addWidget(self.window_row)

        self.chart = HistoryChart()
        layout.addWidget(self.chart, 1)

        self.stats_box = QWidget()
        grid = QGridLayout(self.stats_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        self.stat_values: dict[str, QLabel] = {}
        for i, (key, label, tip) in enumerate(self.STATS):
            box = QVBoxLayout()
            box.setSpacing(1)
            name = QLabel(label.upper())
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
        layout.addWidget(self.stats_box)
        self.limits = QLabel("")
        self.limits.setObjectName("muted")
        layout.addWidget(self.limits)
        self.set_expanded(False)

    def set_expanded(self, expanded: bool, user: bool = False) -> None:
        """Open the drawer to the chart and every statistic, or shut it to its strip: the sensor,
        its value and its last minute. ``user`` marks a click."""
        self.expanded = expanded
        for widget in (self.window_row, self.chart, self.stats_box, self.limits, self.subtitle, self.pin_button):
            widget.setVisible(expanded)
        self.spark.setVisible(not expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.UpArrow)
        self.toggle.setAccessibleName("Hide the chart" if expanded else "Show the chart")
        self.toggle.setToolTip("Hide the chart (Esc)" if expanded else "Show the chart and every statistic")
        self.header.setToolTip("" if expanded else "Click to show the chart")
        # Only the shut strip opens on a click; open, its title and value are just text.
        if expanded:
            self.header.unsetCursor()
        else:
            self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMaximumHeight(QWIDGET_MAX if expanded else self.strip_height())
        if user:
            self.expanded_changed.emit(expanded)

    def strip_height(self) -> int:
        """How tall the drawer is when shut."""
        margins = self._margins
        return self.header.sizeHint().height() + margins.top() + margins.bottom()

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        # The strip's height follows its text, its font and the style, which can all change after
        # it's shut; the window's styling arrives only once it's shown.
        if event.type() == QEvent.Type.LayoutRequest and not self.expanded:
            self.setMaximumHeight(self.strip_height())
        return handled

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # A click on the strip (its margins too), away from its buttons, opens the drawer.
        if event.button() == Qt.MouseButton.LeftButton and not self.expanded:
            self.set_expanded(True, user=True)
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        paint_card(painter, QRectF(self.rect()), current_theme())
        painter.end()

    def _on_pin_toggled(self, pinned: bool) -> None:
        self.pin_button.setText("Pinned to tray" if pinned else "Pin to tray")
        self.pin_toggled.emit(pinned)

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
        pinned: bool = False,
        can_pin: bool = True,
    ) -> None:
        self.chart.set_data(row, self.window_seconds, fahrenheit, gap, now, to_wall)
        self.spark.set_row(row, now, gap)
        self.focus_button.setEnabled(row is not None)
        if row is None:
            self.pin_button.setEnabled(False)
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
        self.title.setText(reading.label)
        self.subtitle.setText(reading.origin or reading.device)
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
