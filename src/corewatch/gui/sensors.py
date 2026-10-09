"""The sensor list: one card per device, its sensors laid out in two columns."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFont,
    QFontMetrics,
    QHelpEvent,
    QMouseEvent,
    QPainter,
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

from corewatch.gui.cards import CardView, ViewData
from corewatch.gui.theme import BODY_FONT, DISPLAY_FONT, MONO_FONT, current_theme, font, paint_card
from corewatch.gui.widgets import ElidedLabel, draw_sparkline
from corewatch.model import Kind, Row, format_limit, format_value
from corewatch.monitor import KIND_ORDER

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


def short_label(row: Row) -> str:
    """Under a "Load" or "Clocks" heading, "P-core 3 load" reads better as "P-core 3"."""
    label = row.reading.label
    for kind, suffix in ((Kind.LOAD, " load"), (Kind.CLOCK, " clock")):
        if row.reading.kind is kind and label.endswith(suffix) and len(label) > len(suffix):
            return label.removesuffix(suffix)
    return label


@dataclass(frozen=True)
class Cell:
    key: str
    rect: QRect


class SensorGrid(QWidget):
    """Paints a device's sensors as a grid of cells, grouped under one heading per kind."""

    selected = Signal(str)
    opened = Signal(str)  # a double-click: the sensor's focus view
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
            metrics = QFontMetrics(self._fonts()[0])  # the label font
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
            widest = header.horizontalAdvance(STAT_TITLES[name].upper())
            for row in self.rows:
                widest = max(widest, metrics.horizontalAdvance(self._stat_text(row, name)))
            if widest + STAT_PAD > self.stat_widths[name]:
                self.stat_widths[name] = widest + STAT_PAD
                changed = True
        return changed

    def cell_min_width(self) -> int:
        shown = self.shown_stats()
        return PAD * 2 + MIN_LABEL_W + SPARK_MIN_W + sum(self.stat_widths[n] for n in shown) + GAP * (len(shown) + 1)

    def _fonts(self) -> tuple[QFont, QFont, QFont, QFont]:
        """(label, header, value, other numbers): names in the body font, column and group
        headings in spaced caps, numbers in the monospaced font (bold for the current value)."""
        return (
            font(BODY_FONT, 13),
            font(DISPLAY_FONT, 10, QFont.Weight.DemiBold, 1.5),
            font(MONO_FONT, 13, QFont.Weight.Bold),
            font(MONO_FONT, 12),
        )

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
        base, small, bold, numbers = self._fonts()
        muted, text, border = QColor(theme.muted), QColor(theme.text), QColor(theme.edge)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        painter.setFont(small)
        painter.setPen(muted)
        for header in self._headers:
            parts = self._parts(header)
            painter.drawText(parts["label"], left, "SENSOR")
            painter.drawText(parts["spark"], left, "LAST 60 S")
            for name in self.shown_stats():
                painter.drawText(parts[name], right, STAT_TITLES[name].upper())

        for rect, title in self._subheads:
            painter.setFont(small)
            painter.setPen(muted)
            painter.drawText(rect.adjusted(PAD, 6, 0, 0), left, title.upper())
            painter.setPen(QPen(border, 1))
            painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        by_key = {r.reading.key: r for r in self.rows}
        metrics = QFontMetrics(base)
        for cell in self._cells:
            row = by_key.get(cell.key)
            if row is None:
                continue
            rect = cell.rect
            if cell.key == self.selected_key:  # tinted, with an accent bar down its left edge
                band = QRectF(rect).adjusted(0, 1, 0, -1)
                painter.fillRect(band, QColor(theme.accent_soft))
                painter.fillRect(QRectF(band.left(), band.top(), 2, band.height()), QColor(theme.accent))
            elif cell.key == self._hover:
                painter.fillRect(QRectF(rect).adjusted(0, 1, 0, -1), QColor(theme.raised))
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
            painter.setFont(numbers)
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

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        key = self.key_at(event.position().toPoint())
        if key is not None and event.button() == Qt.MouseButton.LeftButton:
            self.opened.emit(key)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        key = self.key_at(event.pos())
        if key is not None:
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
    """A card for one device: a header, then its compact view (if it has one) and its full
    sensor list, shown on demand under the view."""

    table_toggled = Signal(str, bool)  # "All sensors" switched on or off by a click

    def __init__(
        self,
        device: str,
        parent: QWidget | None = None,
        view: CardView | None = None,
        show_table: bool = False,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.show_table = show_table
        self.filtering = False
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        self.header = QWidget()
        self.header.setObjectName("cardHeader")
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.title = ElidedLabel(device)
        self.title.setObjectName("sectionTitle")
        header.addWidget(self.title)
        self.count = QLabel("")
        self.count.setObjectName("muted")
        header.addWidget(self.count)
        header.addStretch(1)
        # A card with a compact view opens on it; this shows its full list under it too.
        self.table_button = QToolButton()
        self.table_button.setObjectName("tableToggle")
        self.table_button.setText("All sensors")
        self.table_button.setCheckable(True)
        self.table_button.setChecked(show_table)
        self.table_button.setToolTip("Show every sensor on this card, with its min, max and average")
        self.table_button.clicked.connect(lambda shown: self.set_show_table(shown, user=True))
        header.addWidget(self.table_button)
        layout.addWidget(self.header)

        self.view = view
        if view is not None:
            layout.addWidget(view)
        self.grid = SensorGrid()
        layout.addWidget(self.grid)
        layout.addStretch(1)  # beside a taller card, keep this one's content at the top
        self._sync()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        paint_card(painter, QRectF(self.rect()), current_theme())
        painter.end()

    def has_view(self) -> bool:
        """True while the compact view is what the card opens on (not while filtering: then the
        card lists the sensors that match)."""
        return self.view is not None and not self.view.is_empty() and not self.filtering

    def table_shown(self) -> bool:
        return not self.has_view() or self.show_table

    def _sync(self) -> None:
        if self.view is not None:
            self.view.setVisible(self.has_view())
        self.grid.setVisible(self.table_shown())
        self.table_button.setVisible(self.has_view())
        # As tall with the button as without it, so cards side by side line their titles up.
        self.header.setMinimumHeight(self.table_button.sizeHint().height())

    def set_show_table(self, shown: bool, user: bool = False) -> None:
        self.show_table = shown
        self.table_button.setChecked(shown)
        self._sync()
        if user:
            self.table_toggled.emit(self.device, shown)

    def set_view_data(self, data: ViewData, filtering: bool) -> None:
        self.filtering = filtering
        if self.view is not None:
            self.view.set_data(data)
        self._sync()

    def set_selected(self, key: str | None) -> None:
        self.grid.set_selected(key)
        if self.view is not None:
            self.view.set_selected(key)

    def set_rows(self, rows: Sequence[Row], now: float, fahrenheit: bool, gap: float) -> None:
        self.count.setText(f"{len(rows)} sensor{'s' if len(rows) != 1 else ''}")
        self.grid.set_rows(rows, now, fahrenheit, gap)
