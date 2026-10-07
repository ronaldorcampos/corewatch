"""The sensor list: one card per device, its sensors laid out in two columns."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFont,
    QFontMetrics,
    QHelpEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from corewatch.gui.theme import Theme, current_theme
from corewatch.gui.widgets import drawable_segments, value_range
from corewatch.model import Kind, Row, format_limit, format_value
from corewatch.monitor import KIND_ORDER

SPARKLINE_SECONDS = 60.0
COLUMNS = 2

KIND_TITLES = {
    Kind.TEMPERATURE: "Temperatures",
    Kind.LOAD: "Load",
    Kind.CLOCK: "Clocks",
    Kind.POWER: "Power",
    Kind.FAN: "Fan speed",
    Kind.FAN_DUTY: "Fan control",
    Kind.VOLTAGE: "Voltages",
    Kind.CURRENT: "Current",
    Kind.THROUGHPUT: "Traffic",
}

# Cell geometry, in pixels. The label takes whatever width is left.
PAD = 10
GAP = 8
SPARK_MIN_W = 96  # the sparkline gets every pixel the label doesn't need, never less than this
STAT_PAD = 12  # breathing room added to the widest text in a numbers column
STATS = ("value", "min", "max", "avg")
STAT_TITLES = {"value": "Value", "min": "Min", "max": "Max", "avg": "Average"}
MIN_LABEL_W = 96
MAX_LABEL_W = 220  # longer names are elided rather than starving the sparkline
# Smallest cell for typical numbers ("45.0 °C"); real cells grow to fit their own values.
CELL_MIN_W = PAD * 2 + MIN_LABEL_W + SPARK_MIN_W + 4 * 64 + GAP * 5
COLUMN_GAP = 18
HEADER_H = 24
SUBHEAD_H = 28
ROW_H = 30


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


def short_label(row: Row) -> str:
    """Under a "Load" or "Clocks" heading, "P-core 3 load" reads better as "P-core 3"."""
    label = row.reading.label
    for kind, suffix in ((Kind.LOAD, " load"), (Kind.CLOCK, " clock")):
        if row.reading.kind is kind and label.endswith(suffix) and len(label) > len(suffix):
            return label.removesuffix(suffix)
    return label


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


@dataclass(frozen=True)
class Cell:
    key: str
    rect: QRect


class SensorGrid(QWidget):
    """Paints a device's sensors as a grid of cells, grouped under one heading per kind."""

    selected = Signal(str)
    context_requested = Signal(str, QPoint)  # sensor key, global position

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rows: list[Row] = []
        self.columns = COLUMNS
        self.fahrenheit = False
        self.selected_key: str | None = None
        self.show_min_max = True
        self.now = 0.0
        self.gap = 5.0
        self._hover: str | None = None
        self._cells: list[Cell] = []
        self._subheads: list[tuple[QRect, str]] = []
        self._headers: list[QRect] = []
        self._layout_width = -1
        # Width of each numbers column, sized to the widest text it has shown. It only grows
        # (until the sensors or unit change) so columns don't jiggle as values tick over.
        self.stat_widths: dict[str, int] = dict.fromkeys(STATS, 0)
        self.label_width = MIN_LABEL_W

    # ----- data ---------------------------------------------------------------------

    def set_rows(self, rows: Sequence[Row], now: float, fahrenheit: bool, gap: float) -> None:
        keys_changed = [r.reading.key for r in rows] != [r.reading.key for r in self.rows]
        if keys_changed or fahrenheit != self.fahrenheit:
            self.stat_widths = dict.fromkeys(STATS, 0)
        self.rows, self.now, self.fahrenheit, self.gap = list(rows), now, fahrenheit, gap
        if keys_changed:
            metrics = QFontMetrics(self._fonts()[0])
            longest = max((metrics.horizontalAdvance(short_label(r)) for r in self.rows), default=0)
            self.label_width = max(MIN_LABEL_W, min(MAX_LABEL_W, longest + 12))
        if self._fit_stat_widths() or keys_changed:
            self._relayout()
        self.update()

    def set_columns(self, columns: int) -> None:
        self.columns = max(1, columns)
        self._relayout()
        self.update()

    def shown_stats(self) -> tuple[str, ...]:
        return STATS if self.show_min_max else tuple(name for name in STATS if name not in ("min", "max"))

    def set_show_min_max(self, show: bool) -> None:
        if show != self.show_min_max:
            self.show_min_max = show
            self._relayout()
            self.update()

    def set_selected(self, key: str | None) -> None:
        if key != self.selected_key:
            self.selected_key = key
            self.update()

    def effective_columns(self, width: int | None = None) -> int:
        """Two columns, or one when the card is too narrow for two to fit."""
        width = self.width() if width is None else width
        fit = max(1, (width + COLUMN_GAP) // (self.cell_min_width() + COLUMN_GAP))
        return max(1, min(self.columns, fit))

    def blocks(self) -> list[tuple[Kind, list[Row]]]:
        grouped: dict[Kind, list[Row]] = {}
        for row in self.rows:
            grouped.setdefault(row.reading.kind, []).append(row)
        return sorted(grouped.items(), key=lambda item: KIND_ORDER.index(item[0]))

    def cell_texts(self, key: str) -> list[str]:
        """What a cell shows: label, value, min, max, average (used for painting and tests)."""
        row = next(r for r in self.rows if r.reading.key == key)
        return [short_label(row), *(self._stat_text(row, name) for name in STATS)]

    def cells(self) -> list[Cell]:
        return list(self._cells)

    # ----- geometry -----------------------------------------------------------------

    def _fit_stat_widths(self) -> bool:
        """Grow numbers columns to fit their widest text; True if any column changed."""
        metrics = QFontMetrics(self._fonts()[2])  # bold: the widest rendering
        header = QFontMetrics(self._fonts()[1])
        changed = False
        for name in STATS:
            widest = header.horizontalAdvance(STAT_TITLES[name])
            for row in self.rows:
                widest = max(widest, metrics.horizontalAdvance(self._stat_text(row, name)))
            if widest + STAT_PAD > self.stat_widths[name]:
                self.stat_widths[name] = widest + STAT_PAD
                changed = True
        return changed

    def cell_min_width(self) -> int:
        shown = self.shown_stats()
        return PAD * 2 + MIN_LABEL_W + SPARK_MIN_W + sum(self.stat_widths[n] for n in shown) + GAP * (len(shown) + 1)

    def _fonts(self) -> tuple[QFont, QFont, QFont]:
        """(regular, small, bold), all with equal-width digits."""
        base = QFont(self.font())
        base.setFeature(QFont.Tag("tnum"), 1)
        small = QFont(base)
        small.setPixelSize(11)
        bold = QFont(base)
        bold.setBold(True)
        return base, small, bold

    def _stat_text(self, row: Row, name: str) -> str:
        kind, stats, f = row.reading.kind, row.stats, self.fahrenheit
        value = {"value": row.reading.value, "min": stats.minimum, "max": stats.maximum, "avg": stats.average}[name]
        return format_value(kind, value, f)

    def _relayout(self) -> None:
        width = max(self.width(), self.cell_min_width())
        columns = self.effective_columns(width)
        cell_w = (width - COLUMN_GAP * (columns - 1)) // columns
        self._cells, self._subheads, self._headers = [], [], []
        self._headers = [QRect(c * (cell_w + COLUMN_GAP), 0, cell_w, HEADER_H) for c in range(columns)]
        y = HEADER_H
        blocks = self.blocks()
        for kind, rows in blocks:
            if len(blocks) > 1:
                self._subheads.append((QRect(0, y, width, SUBHEAD_H), f"{KIND_TITLES[kind]}  ·  {len(rows)}"))
                y += SUBHEAD_H
            # Fill top-to-bottom, then the next column, so each list reads straight down.
            per_column = math.ceil(len(rows) / columns)
            for index, row in enumerate(rows):
                column, line = divmod(index, per_column)
                rect = QRect(column * (cell_w + COLUMN_GAP), y + line * ROW_H, cell_w, ROW_H)
                self._cells.append(Cell(row.reading.key, rect))
            y += per_column * ROW_H
        self._layout_width = self.width()
        self.setFixedHeight(y + 4)

    def resizeEvent(self, event: object) -> None:
        if self.width() != self._layout_width:
            self._relayout()

    def sizeHint(self) -> QSize:
        return QSize(self.cell_min_width() * self.columns, self.height())

    # ----- painting -----------------------------------------------------------------

    def _parts(self, rect: QRect) -> dict[str, QRect]:
        """Split a cell into label | sparkline | value | min | max | average."""
        x = rect.right() - PAD
        parts = {}
        for name in reversed(self.shown_stats()):
            width = self.stat_widths[name]
            parts[name] = QRect(x - width + 1, rect.top(), width, rect.height())
            x -= width + GAP
        # What's left is shared: the label takes what its longest name needs, the sparkline the rest.
        left = rect.left() + PAD
        remaining = max(0, x - left + 1)
        label_w = max(MIN_LABEL_W, min(self.label_width, remaining - GAP - SPARK_MIN_W))
        label_w = min(label_w, remaining)
        parts["label"] = QRect(left, rect.top(), label_w, rect.height())
        spark_w = max(0, remaining - label_w - GAP)
        parts["spark"] = QRect(left + label_w + GAP, rect.top(), spark_w, rect.height())
        return parts

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base, small, bold = self._fonts()
        muted, text, border = QColor(theme.muted), QColor(theme.text), QColor(theme.border)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        painter.setFont(small)
        painter.setPen(muted)
        for header in self._headers:
            parts = self._parts(header)
            painter.drawText(parts["label"], left, "Sensor")
            painter.drawText(parts["spark"], left, "Last 60 s")
            for name in self.shown_stats():
                painter.drawText(parts[name], right, STAT_TITLES[name])

        for rect, title in self._subheads:
            painter.setFont(small)
            painter.setPen(muted)
            painter.drawText(rect.adjusted(PAD, 6, 0, 0), left, title)
            painter.setPen(QPen(border, 1))
            painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        by_key = {r.reading.key: r for r in self.rows}
        metrics = QFontMetrics(base)
        for cell in self._cells:
            row = by_key.get(cell.key)
            if row is None:
                continue
            rect = cell.rect
            if cell.key == self.selected_key:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.accent_soft))
                painter.drawRoundedRect(QRectF(rect).adjusted(0, 1, 0, -1), 6, 6)
            elif cell.key == self._hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.raised))
                painter.drawRoundedRect(QRectF(rect).adjusted(0, 1, 0, -1), 6, 6)
            selected = cell.key == self.selected_key
            label, value, minimum, maximum, average = self.cell_texts(cell.key)
            parts = self._parts(rect)
            painter.setFont(base)
            painter.setPen(QColor(theme.accent_text) if selected else text)
            painter.drawText(
                parts["label"], left, metrics.elidedText(label, Qt.TextElideMode.ElideRight, parts["label"].width())
            )
            draw_sparkline(painter, QRectF(parts["spark"]).adjusted(0, 7, 0, -7), row, self.now, self.gap, theme)
            painter.setFont(bold)
            painter.setPen(theme.value_color(row.reading.status))
            painter.drawText(parts["value"], right, value)
            painter.setFont(base)
            painter.setPen(muted)
            for name, content in (("min", minimum), ("max", maximum), ("avg", average)):
                if name in parts:
                    painter.drawText(parts[name], right, content)
        painter.end()

    # ----- interaction --------------------------------------------------------------

    def key_at(self, point: QPoint) -> str | None:
        return next((cell.key for cell in self._cells if cell.rect.contains(point)), None)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        key = self.key_at(event.position().toPoint())
        if key is not None and event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(key)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        key = self.key_at(event.pos())
        if key is not None:
            self.selected.emit(key)
            self.context_requested.emit(key, event.globalPos())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        key = self.key_at(event.position().toPoint())
        if key != self._hover:
            self._hover = key
            self.setCursor(Qt.CursorShape.PointingHandCursor if key else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = None
        self.update()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            key = self.key_at(event.pos())
            row = next((r for r in self.rows if r.reading.key == key), None)
            if row is None:
                QToolTip.hideText()
                return True
            limits = format_limit(row.reading, self.fahrenheit)
            QToolTip.showText(event.globalPos(), row.reading.label + (f"\n{limits}" if limits else ""), self)
            return True
        return super().event(event)


class CategorySection(QFrame):
    """A card for one device: a header that folds it, then its sensor grid."""

    collapse_toggled = Signal(str, bool)

    def __init__(self, device: str, collapsed: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.collapsed: bool = collapsed
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        # The whole header row folds the card, not just the chevron.
        self.header = QWidget()
        self.header.setObjectName("cardHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setToolTip("Click to fold or unfold")
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.chevron = QToolButton()
        self.chevron.setObjectName("chevron")
        self.chevron.setAutoRaise(True)
        self.chevron.clicked.connect(lambda: self.set_collapsed(not self.collapsed, user=True))
        header.addWidget(self.chevron)
        self.title = QLabel(device)
        self.title.setObjectName("sectionTitle")
        header.addWidget(self.title)
        self.count = QLabel("")
        self.count.setObjectName("muted")
        header.addWidget(self.count)
        header.addStretch(1)
        layout.addWidget(self.header)

        self.grid = SensorGrid()
        layout.addWidget(self.grid)
        self.set_collapsed(collapsed)

    def set_collapsed(self, collapsed: bool, user: bool = False) -> None:
        """``user`` marks a click (remembered); filtering expands sections without remembering it."""
        self.collapsed = collapsed
        self.grid.setVisible(not collapsed)
        self.chevron.setArrowType(Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow)
        if user:
            self.collapse_toggled.emit(self.device, collapsed)

    def set_rows(self, rows: Sequence[Row], now: float, fahrenheit: bool, gap: float) -> None:
        self.count.setText(f"{len(rows)} sensor{'s' if len(rows) != 1 else ''}")
        self.grid.set_rows(rows, now, fahrenheit, gap)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # Clicks on the header's labels and empty space land here; the chevron handles its own.
        if event.button() == Qt.MouseButton.LeftButton and self.header.geometry().contains(event.position().toPoint()):
            self.set_collapsed(not self.collapsed, user=True)
            return
        super().mouseReleaseEvent(event)
