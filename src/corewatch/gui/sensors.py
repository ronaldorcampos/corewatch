"""The sensor list: one card per device, its sensors as rows with a bar each, in two columns
on a wide card."""

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

from corewatch.gui.cards import HARD, CardView, ViewData, draw_segments, draw_text, fan_duties
from corewatch.gui.layout import LayoutBar
from corewatch.gui.theme import BODY_FONT, MONO_FONT, Theme, current_theme, font, paint_card, px
from corewatch.gui.widgets import ElidedLabel, draw_sparkline
from corewatch.model import Kind, Reading, Row, cap_word, format_stall, format_typical, format_value
from corewatch.monitor import FANS, KIND_ORDER

COLUMNS = 2  # on a wide card; a half-width card lists its sensors in one

# Row geometry, before px() scaling. The name takes whatever the value doesn't need.
PAD = 10
GAP = 12
MIN_LABEL_W = 140
BAND_H = 12  # the bar, or the sparkline in its place
ROW_H = 62
STATS = ("value", "min", "max", "avg")
VALUE_MIN_W = 96  # room for a typical value ("45.0 °C"); real rows grow to fit their own
SUB_MAX_W = 440  # the most a row's grey line asks for: a longer one is cut rather than widen the card
# The smallest a row gets with typical numbers; real rows grow to fit their values and grey line.
CELL_MIN_W = PAD * 2 + MIN_LABEL_W + GAP + VALUE_MIN_W
COLUMN_GAP = 18
TEMPERATURE_SCALE = 90.0  # °C a full bar stands for, unless the sensor's limits go higher


def bar_scale(reading: Reading) -> float | None:
    """What a full bar stands for, in the reading's own unit, or None for a sensor with no
    natural top (a voltage, a fan's speed): it gets its last minute drawn instead, except in
    the Fans card (see ``fill_scale``)."""
    kind = reading.kind
    if kind is Kind.TEMPERATURE:
        return max(TEMPERATURE_SCALE, (reading.crit or reading.high or 0.0) + 5)
    if kind in (Kind.LOAD, Kind.FAN_DUTY):
        return 100.0
    if kind in (Kind.CLOCK, Kind.POWER) and reading.cap:
        return reading.cap
    return None


def limit_text(reading: Reading, fahrenheit: bool) -> str:
    """A sensor's limits as its row and tooltip name them, e.g. ``warns at 80.0 °C · critical at
    100.0 °C``, or ``max clock 5,300 MHz``; a stalled fan says so first."""
    f, parts = fahrenheit, [stall] if (stall := format_stall(reading)) else []
    if reading.high is not None:
        parts.append(f"warns at {format_value(reading.kind, reading.high, f)}")
    if reading.crit is not None:
        parts.append(f"critical at {format_value(reading.kind, reading.crit, f)}")
    if reading.low is not None:
        parts.append(f"low under {format_value(reading.kind, reading.low, f)}")
    if reading.cap is not None:
        word = "max clock" if reading.kind is Kind.CLOCK else cap_word(reading.kind)
        parts.append(f"{word} {format_value(reading.kind, reading.cap, f)}")
    typical = format_typical(reading, f)
    return " · ".join([*parts, typical] if typical else parts)


def split_unit(text: str) -> tuple[str, str]:
    """``"1,614 RPM"`` -> ``("1,614", "RPM")``; a dash or a bare number keeps no unit."""
    number, _, unit = text.rpartition(" ")
    return (number, unit) if number else (text, "")


def fill_scale(row: Row) -> float | None:
    """What ``row``'s bar fills against in the Fans card, where every row has the card's stepped
    bar: its natural top, else its warning limit, else the highest it has read this session (so
    a fan's bar is its speed against its fastest yet)."""
    reading = row.reading
    for scale in (bar_scale(reading), reading.high, row.stats.maximum):
        if scale is not None and scale > 0:
            return scale
    return None


def bar_limits(reading: Reading, scale: float) -> tuple[float | None, float | None]:
    """Where along a bar (as shares) its steps turn amber and red: at the sensor's limits, or,
    for a fan's control, in the last two steps (driven hard), as the Fans card has it."""
    warn = reading.high / scale if reading.high is not None else None
    crit = reading.crit / scale if reading.crit is not None else None
    if warn is None and crit is None and reading.kind is Kind.FAN_DUTY:
        warn = HARD
    return warn, crit


@dataclass(frozen=True)
class Cell:
    key: str
    rect: QRect


class SensorGrid(QWidget):
    """Paints a device's sensors as rows, like the Storage and Fans cards: the name and value,
    a bar filled against what the sensor can reach (or its last minute, when nothing caps it),
    and its min, max, average and limits under that. Sorted by kind; two columns on a wide card."""

    selected = Signal(str)
    opened = Signal(str)  # a double-click: the sensor's focus view
    context_requested = Signal(str, QPoint)  # sensor key, global position

    def __init__(self, parent: QWidget | None = None, stepped: bool = False) -> None:
        super().__init__(parent)
        # The Fans card's list draws every row with the stepped bar its view has, for one look.
        self.stepped = stepped
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
        self._layout_width = -1
        self._duties: dict[str, Row] = {}  # a fan's control setting, by the fan's key
        # The widest value shown so far. It only grows (until the sensors or unit change), so a
        # value ticking from 900 to 1,200 RPM doesn't make the card ask for a new width each time.
        # The same for the widest grey line, up to SUB_MAX_W.
        self.value_width = self.sub_width = 0

    # ----- data ---------------------------------------------------------------------

    def set_rows(self, rows: Sequence[Row], now: float, fahrenheit: bool, gap: float) -> None:
        keys_changed = [r.reading.key for r in rows] != [r.reading.key for r in self.rows]
        if keys_changed or fahrenheit != self.fahrenheit:
            self.value_width = self.sub_width = 0
        self.rows, self.now, self.fahrenheit, self.gap = list(rows), now, fahrenheit, gap
        if self._fit_widths() or keys_changed:
            self._relayout()
        self.update()

    def set_duties(self, duties: dict[str, Row]) -> None:
        """Each fan's control setting, by the fan's key, paired among all the card's sensors: a
        filter that shows a fan but not its control leaves its bar as it was."""
        self._duties = duties
        self.update()

    def set_columns(self, columns: int) -> None:
        if max(1, columns) != self.columns:
            self.columns = max(1, columns)
            self._relayout()
            self.update()

    def shown_stats(self) -> tuple[str, ...]:
        return STATS if self.show_min_max else tuple(name for name in STATS if name not in ("min", "max"))

    def set_show_min_max(self, show: bool) -> None:
        if show != self.show_min_max:
            self.show_min_max = show
            self.sub_width = 0  # the grey line got shorter, or longer
            self._fit_widths()
            self._relayout()
            self.update()

    def set_selected(self, key: str | None) -> None:
        if key != self.selected_key:
            self.selected_key = key
            self.update()

    def effective_columns(self, width: int | None = None) -> int:
        """Two columns, or one when the card is too narrow for two to fit."""
        width = self.width() if width is None else width
        fit = max(1, (width + px(COLUMN_GAP)) // (self.cell_min_width() + px(COLUMN_GAP)))
        return max(1, min(self.columns, fit))

    def blocks(self) -> list[tuple[Kind, list[Row]]]:
        grouped: dict[Kind, list[Row]] = {}
        for row in self.rows:
            grouped.setdefault(row.reading.kind, []).append(row)
        return sorted(grouped.items(), key=lambda item: KIND_ORDER.index(item[0]))

    def _row(self, key: str) -> Row:
        return next(r for r in self.rows if r.reading.key == key)

    def cell_texts(self, key: str) -> list[str]:
        """A row's name, value, min, max and average (for tests and the tooltip)."""
        row = self._row(key)
        return [row.reading.label, *(self._stat_text(row, name) for name in STATS)]

    def sub_text(self, key: str) -> str:
        """The grey line under a row's bar: min, max (unless hidden) and average, then its limits,
        e.g. ``min 30.0 · max 31.0 · avg 30.1 °C · warns at 80.0 °C``."""
        row = self._row(key)
        reading, f = row.reading, self.fahrenheit
        names = [name for name in self.shown_stats() if name != "value"]
        texts = [split_unit(self._stat_text(row, name)) for name in names]
        if all(number == "—" for number, _ in texts):  # nothing read yet: no row of dashes
            names, texts = [], []
        parts: list[str] = []
        if reading.kind is Kind.THROUGHPUT:  # each in its own unit: KB/s, MB/s
            parts = [f"{name} {number} {unit}".rstrip() for name, (number, unit) in zip(names, texts, strict=True)]
        elif texts:
            unit = next((u for _, u in texts if u), "")
            stats = " · ".join(f"{name} {number}" for name, (number, _) in zip(names, texts, strict=True))
            parts = [f"{stats} {unit}".rstrip()]
        limits = limit_text(reading, f)
        return " · ".join([*parts, limits] if limits else parts)

    def cells(self) -> list[Cell]:
        return list(self._cells)

    # ----- geometry -----------------------------------------------------------------

    def _fit_widths(self) -> bool:
        """Grow the room for the widest value and the widest grey line; True if either grew."""
        value_font, unit_font, sub_font = self._fonts()[1:4]
        widest = sub = 0
        for row in self.rows:
            number, unit = split_unit(self._stat_text(row, "value"))
            widest = max(
                widest,
                QFontMetrics(value_font).horizontalAdvance(number)
                + (QFontMetrics(unit_font).horizontalAdvance(unit) + px(5) if unit else 0),
            )
            sub = max(sub, QFontMetrics(sub_font).horizontalAdvance(self.sub_text(row.reading.key)))
        grew = widest > self.value_width or min(sub, px(SUB_MAX_W)) > self.sub_width
        self.value_width = max(self.value_width, widest)
        self.sub_width = max(self.sub_width, min(sub, px(SUB_MAX_W)))
        return grew

    def cell_min_width(self) -> int:
        # Wide enough for the name and value, and for the grey line, so its limits (last on it)
        # aren't the part cut off: a card goes to one column, or a line of its own, first.
        named = px(PAD * 2 + MIN_LABEL_W + GAP) + max(self.value_width, px(VALUE_MIN_W))
        return max(named, px(PAD * 2) + self.sub_width)

    @staticmethod
    def _fonts() -> tuple[QFont, QFont, QFont, QFont]:
        """(name, value, unit, the grey line): as the Storage and Fans cards have them."""
        return font(BODY_FONT, 14), font(MONO_FONT, 14, QFont.Weight.Bold), font(MONO_FONT, 11), font(BODY_FONT, 12)

    def row_height(self) -> int:
        name, _, _, sub = self._fonts()
        text = QFontMetrics(name).height() + px(4) + px(BAND_H) + px(3) + QFontMetrics(sub).height()
        return max(px(ROW_H), text + px(12))

    def _stat_text(self, row: Row, name: str) -> str:
        kind, stats, f = row.reading.kind, row.stats, self.fahrenheit
        value = {"value": row.reading.value, "min": stats.minimum, "max": stats.maximum, "avg": stats.average}[name]
        return format_value(kind, value, f)

    def _relayout(self) -> None:
        width = max(self.width(), self.cell_min_width())
        columns = self.effective_columns(width)
        gap = px(COLUMN_GAP)
        cell_w = (width - gap * (columns - 1)) // columns
        height = self.row_height()
        ordered = [row for _, rows in self.blocks() for row in rows]
        # Fill top-to-bottom, then the next column, so the list reads straight down.
        per_column = max(1, math.ceil(len(ordered) / columns))
        self._cells = []
        for index, row in enumerate(ordered):
            column, line = divmod(index, per_column)
            self._cells.append(Cell(row.reading.key, QRect(column * (cell_w + gap), line * height, cell_w, height)))
        self._layout_width = self.width()
        self.setFixedHeight(min(len(ordered), per_column) * height + px(4))

    def resizeEvent(self, event: object) -> None:
        if self.width() != self._layout_width:
            self._relayout()

    def sizeHint(self) -> QSize:
        return QSize(self.cell_min_width() * self.columns, self.height())

    # ----- painting -----------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        by_key = {r.reading.key: r for r in self.rows}
        for cell in self._cells:
            row = by_key.get(cell.key)
            if row is None:
                continue
            rect = QRectF(cell.rect)
            if cell.key == self.selected_key:  # tinted, with an accent bar down its left edge
                painter.fillRect(rect, QColor(theme.accent_soft))
                painter.fillRect(QRectF(rect.left(), rect.top(), 2, rect.height()), QColor(theme.accent))
            elif cell.key == self._hover:
                painter.fillRect(rect, QColor(theme.raised))
            self._paint_row(painter, rect, row, theme)
        painter.end()

    def _paint_row(self, painter: QPainter, rect: QRectF, row: Row, theme: Theme) -> None:
        reading = row.reading
        name_font, value_font, unit_font, sub_font = self._fonts()
        name_h, sub_h = QFontMetrics(name_font).height(), QFontMetrics(sub_font).height()
        x, width = rect.left() + px(PAD), rect.width() - px(PAD * 2)
        top = rect.top() + (rect.height() - (name_h + px(4) + px(BAND_H) + px(3) + sub_h)) / 2
        left, right = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight
        selected = reading.key == self.selected_key

        number, unit = split_unit(self._stat_text(row, "value"))
        unit_w = QFontMetrics(unit_font).horizontalAdvance(unit) if unit else 0
        number_w = QFontMetrics(value_font).horizontalAdvance(number)
        line = QRectF(x, top, width, name_h)
        name_room = line.adjusted(0, 0, -(number_w + unit_w + px(5) + px(GAP)), 0)
        draw_text(painter, name_room, reading.label, name_font, theme.accent_text if selected else theme.text, left)
        if unit:
            draw_text(painter, line, unit, unit_font, theme.muted, right)
        draw_text(
            painter,
            line.adjusted(0, 0, -(unit_w + px(5) if unit else 0), 0),
            number,
            value_font,
            theme.value_color(reading.status),
            right,
        )

        band = QRectF(x, line.bottom() + px(4), width, px(BAND_H))
        if self.stepped:
            share, warn, crit = self._steps(row)
            draw_segments(painter, QRectF(x, band.center().y() - px(3), width, px(6)), share, theme, warn, crit)
        else:
            self._paint_bar(painter, band, row, theme)

        draw_text(
            painter,
            QRectF(x, band.bottom() + px(3), width, sub_h),
            self.sub_text(reading.key),
            sub_font,
            theme.muted,
            left,
        )

    def _steps(self, row: Row) -> tuple[float, float | None, float | None]:
        """A stepped bar's share lit, and where it turns amber and red. A fan's speed shows how
        hard it's set to work, as the card's view above has it."""
        reading = row.reading
        duty = self._duties.get(reading.key)
        if duty is not None and duty.reading.value is not None:
            return duty.reading.value / 100, HARD, None
        scale = fill_scale(row)
        if scale is None or reading.value is None:
            return 0.0, None, None
        return (reading.value / scale, *bar_limits(reading, scale))

    def _paint_bar(self, painter: QPainter, band: QRectF, row: Row, theme: Theme) -> None:
        """A bar filled against the sensor's natural top, with a tick where it starts to warn,
        or, with nothing to fill against, its last minute."""
        reading = row.reading
        scale = bar_scale(reading)
        if scale is None:
            draw_sparkline(painter, band.adjusted(0, 1, 0, -1), row, self.now, self.gap, theme)
            return
        bar = QRectF(band.left(), band.center().y() - px(5) / 2, band.width(), px(5))
        painter.fillRect(bar, QColor(theme.edge))
        if reading.value is not None:
            share = min(max(reading.value / scale, 0.0), 1.0)
            painter.fillRect(
                QRectF(bar.left(), bar.top(), bar.width() * share, bar.height()), theme.status(reading.status)
            )
        if reading.high is not None and reading.high > 0:  # past the bar's end: at its end, as Storage has it
            mark = bar.left() + bar.width() * min(reading.high / scale, 1.0)
            painter.fillRect(QRectF(mark - 1, bar.top() - px(3), 2, bar.height() + px(6)), QColor(theme.warning))

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
            limits = limit_text(row.reading, self.fahrenheit)
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
        # In edit-layout mode this takes the header's place: the card's name, and how to move it.
        # Made the first time it's wanted: every widget costs time each time the style is set.
        self.layout_bar: LayoutBar | None = None

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
        self.grid = SensorGrid(stepped=device == FANS)
        layout.addWidget(self.grid)
        layout.addStretch(1)  # beside a taller card, keep this one's content at the top
        self._sync()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        paint_card(painter, QRectF(self.rect()), current_theme())
        painter.end()

    def edit_bar(self) -> LayoutBar:
        if self.layout_bar is None:
            name, _, model = self.device.partition(" · ")
            self.layout_bar = LayoutBar(name, model or self.count.text(), panel=self.device)
            box = self.layout()
            if isinstance(box, QVBoxLayout):
                box.insertWidget(0, self.layout_bar)
        return self.layout_bar

    def set_editing(self, editing: bool) -> None:
        if editing or self.layout_bar is not None:
            self.edit_bar().setVisible(editing)
        self.header.setVisible(not editing)

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
        if self.grid.stepped:
            self.grid.set_duties(fan_duties(data.rows))
        self._sync()

    def set_selected(self, key: str | None) -> None:
        self.grid.set_selected(key)
        if self.view is not None:
            self.view.set_selected(key)

    def set_rows(self, rows: Sequence[Row], now: float, fahrenheit: bool, gap: float) -> None:
        self.count.setText(f"{len(rows)} sensor{'s' if len(rows) != 1 else ''}")
        if self.layout_bar is not None and not self.device.partition(" · ")[2]:  # "Fans": its count
            self.layout_bar.set_sub(self.count.text())
        self.grid.set_rows(rows, now, fahrenheit, gap)
