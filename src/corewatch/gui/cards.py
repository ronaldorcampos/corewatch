"""Each device card's compact view, shown before its full sensor list: a CPU's heat map and
headline numbers, every fan's speed, each drive's temperature against its limit, the board's
voltage rails, a network card's traffic. And the deck that lays the cards out, two to a line
where they fit."""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, Signal
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
    QResizeEvent,
)
from PySide6.QtWidgets import QBoxLayout, QGridLayout, QScrollArea, QSizePolicy, QToolTip, QWidget

from corewatch.gui.overview import CoreHeatMap, core_groups, tile_name
from corewatch.gui.theme import BODY_FONT, DISPLAY_FONT, MONO_FONT, Theme, current_theme, font, px
from corewatch.gui.widgets import draw_sparkline
from corewatch.model import Kind, Reading, Row, Status, format_value
from corewatch.monitor import FANS, STORAGE, drive_name

KEYBOARD_FOCUS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
# Voltage inputs a board chip reports without a name ("Voltage 9"): left to the full list.
UNNAMED_RAIL = re.compile(r"^(Voltage|in) ?\d+$")
SEVERITY = (Status.OK, Status.WARNING, Status.CRITICAL)


def _natural(text: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key putting nvme2 before nvme10."""
    parts = re.split(r"(\d+)", text.casefold())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts if part)


@dataclass(frozen=True)
class ViewData:
    rows: Sequence[Row]  # this card's sensors, under their original names
    everything: Sequence[Row]  # every sensor (the CPU's tiles read the board's Vcore)
    labels: Mapping[str, str]  # key -> the name shown in the list, your renames included
    fahrenheit: bool = False
    now: float = 0.0
    gap: float = 5.0


@dataclass(frozen=True)
class Item:
    """One clickable thing in a compact view; ``key`` is the sensor it opens."""

    key: str
    tooltip: str = ""


def _label(data: ViewData, row: Row) -> str:
    return data.labels.get(row.reading.key, row.reading.label)


def _value_text(row: Row, fahrenheit: bool) -> str:
    return format_value(row.reading.kind, row.reading.value, fahrenheit)


class CardView(QWidget):
    """What a card shows before its full list. ``is_empty`` when there's nothing to show (a
    board with no named rails): the card then shows its list as before."""

    selected = Signal(str)
    opened = Signal(str)  # a double-click: the sensor's focus view

    def set_data(self, data: ViewData) -> None:
        raise NotImplementedError

    def set_selected(self, key: str | None) -> None:
        raise NotImplementedError

    def is_empty(self) -> bool:
        raise NotImplementedError


class CompactView(CardView):
    """A list or grid of painted items, each opening its sensor on a click, or with the arrow
    keys and Enter. Subclasses build ``items`` in ``load`` and lay them out in ``place``."""

    ACCESSIBLE_NAME = ""  # what a screen reader calls the view

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.data = ViewData((), (), {})
        self.items: list[Item] = []
        self.selected_key: str | None = None
        self.focus_index = 0
        self.keyboard_focus = False  # a focus ring is for keyboard users; a click needs none
        self._shape: tuple[int, int] = (0, 0)
        self.setAccessibleName(self.ACCESSIBLE_NAME)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ----- for subclasses -------------------------------------------------------------

    def load(self, data: ViewData) -> list[Item]:
        raise NotImplementedError

    def place(self, width: int) -> list[QRectF]:
        """Where each item goes at this width, in ``items`` order."""
        raise NotImplementedError

    def footer_height(self) -> int:
        return 0

    def columns(self) -> int:
        """Items to a line, for Up and Down."""
        return 1

    def paint_item(self, painter: QPainter, rect: QRectF, item: Item, theme: Theme) -> None:
        raise NotImplementedError

    def paint_footer(self, painter: QPainter, rect: QRectF, theme: Theme) -> None:
        pass

    # ----- the rest -------------------------------------------------------------------

    def set_data(self, data: ViewData) -> None:
        self.data = data
        self.items = self.load(data)
        self.focus_index = min(self.focus_index, max(len(self.items) - 1, 0))
        # Only more or fewer items, or a footer coming or going, changes the height. A reading
        # doesn't, nor does a tile naming a different core, so most ticks relayout nothing.
        shape = (len(self.items), self.footer_height())
        if shape != self._shape:
            self._shape = shape
            self.updateGeometry()
        # What's here, not the readings: a description that changed every tick would keep a
        # screen reader busy. A tooltip is "name: value", so the name is what's before the last ": ".
        description = "; ".join(item.tooltip.rpartition(": ")[0] for item in self.items if item.tooltip)
        if description != self.accessibleDescription():
            self.setAccessibleDescription(description)
        self.update()

    def set_selected(self, key: str | None) -> None:
        if key != self.selected_key:
            self.selected_key = key
            self.update()

    def is_empty(self) -> bool:
        return not self.items

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        rects = self.place(width)
        bottom = max((r.bottom() for r in rects), default=0.0)
        return math.ceil(bottom) + self.footer_height()

    def sizeHint(self) -> QSize:
        width = max(self.width(), px(360))
        return QSize(px(360), self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(px(240), self.heightForWidth(px(240)))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.updateGeometry()

    def item_at(self, point: QPointF) -> int | None:
        return next((i for i, rect in enumerate(self.place(self.width())) if rect.contains(point)), None)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        index = self.item_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and index is not None:
            self.focus_index = index
            self.selected.emit(self.items[index].key)
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        index = self.item_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and index is not None:
            self.opened.emit(self.items[index].key)
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        over = self.item_at(event.position()) is not None
        self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)

    def focusInEvent(self, event: QFocusEvent) -> None:
        self.keyboard_focus = event.reason() in KEYBOARD_FOCUS
        super().focusInEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.items:
            super().keyPressEvent(event)
            return
        key = event.key()
        last = len(self.items) - 1
        steps = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: -self.columns(),
            Qt.Key.Key_Down: self.columns(),
        }
        if key in steps:
            moved = self.focus_index + steps[Qt.Key(key)]
            columns = self.columns()
            if key == Qt.Key.Key_Down and moved > last and self.focus_index // columns < last // columns:
                moved = last  # the last line is short: down to its last item
            if not 0 <= moved <= last:  # past the first or last line: let the page scroll
                super().keyPressEvent(event)
                return
            self.focus_index = moved
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.focus_index = 0 if key == Qt.Key.Key_Home else last
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.selected.emit(self.items[min(self.focus_index, last)].key)
        else:
            super().keyPressEvent(event)
            return
        self.keyboard_focus = True
        self._show_focused()
        self.update()

    def _show_focused(self) -> None:
        """Scroll the page so the item with the focus ring is in sight."""
        area = self.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        page = area.widget() if area is not None else None
        rects = self.place(self.width())
        if area is None or page is None or not rects:
            return
        rect = rects[min(self.focus_index, len(rects) - 1)]
        center = self.mapTo(page, rect.center().toPoint())
        area.ensureVisible(center.x(), center.y(), int(rect.width() / 2) + px(8), int(rect.height() / 2) + px(8))

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            index = self.item_at(QPointF(event.pos()))
            text = self.items[index].tooltip if index is not None else ""
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rects = self.place(self.width())
        for index, (item, rect) in enumerate(zip(self.items, rects, strict=True)):
            if item.key == self.selected_key:  # like a selected row in the list
                painter.fillRect(rect, QColor(theme.accent_soft))
                painter.fillRect(QRectF(rect.left(), rect.top(), 2, rect.height()), QColor(theme.accent))
            self.paint_item(painter, rect, item, theme)
            if index == self.focus_index and self.hasFocus() and self.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.accent), 1, Qt.PenStyle.DashLine))
                painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        if self.footer_height():
            bottom = max((r.bottom() for r in rects), default=0.0)
            self.paint_footer(painter, QRectF(0, bottom, self.width(), self.footer_height()), theme)
        painter.end()


def _caption_font() -> QFont:
    return font(DISPLAY_FONT, 10, QFont.Weight.DemiBold, 1.5)


def draw_text(
    painter: QPainter, rect: QRectF, text: str, qfont: QFont, color: QColor | str, align: Qt.AlignmentFlag
) -> None:
    painter.setFont(qfont)
    painter.setPen(QColor(color))
    shown = QFontMetrics(qfont).elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()))
    painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, shown)


# ----- tiles: a CPU's headline numbers, a board's voltage rails ------------------------------


@dataclass(frozen=True)
class Tile(Item):
    title: str = ""
    value: str = ""
    unit: str = ""
    status: Status = Status.OK


class TileView(CompactView):
    TILE_H = 54
    TILE_W = 150  # the narrowest a tile gets before the view drops a column
    GAP = 8
    MAX_COLUMNS = 3

    def columns_for(self, width: int) -> int:
        fits = (width + px(self.GAP)) // (px(self.TILE_W) + px(self.GAP))
        return max(1, min(self.MAX_COLUMNS, fits, len(self.items) or 1))

    def columns(self) -> int:
        return self.columns_for(self.width())

    def place(self, width: int) -> list[QRectF]:
        columns = self.columns_for(width)
        gap = px(self.GAP)
        tile_w = (width - gap * (columns - 1)) / columns
        tile_h = max(px(self.TILE_H), self._text_height() + px(16))
        return [
            QRectF((i % columns) * (tile_w + gap), (i // columns) * (tile_h + gap), tile_w, tile_h)
            for i in range(len(self.items))
        ]

    @staticmethod
    def _fonts() -> tuple[QFont, QFont, QFont]:
        return _caption_font(), font(MONO_FONT, 17, QFont.Weight.Bold), font(MONO_FONT, 12)

    def _text_height(self) -> int:
        title, value, _ = self._fonts()
        return QFontMetrics(title).height() + QFontMetrics(value).height()

    def paint_item(self, painter: QPainter, rect: QRectF, item: Item, theme: Theme) -> None:
        assert isinstance(item, Tile)
        painter.setPen(QPen(QColor(theme.edge), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        title_font, value_font, unit_font = self._fonts()
        inner = rect.adjusted(px(12), px(8), -px(12), -px(8))
        title_h = QFontMetrics(title_font).height()
        draw_text(
            painter,
            QRectF(inner.left(), inner.top(), inner.width(), title_h),
            item.title.upper(),
            title_font,
            theme.muted,
            Qt.AlignmentFlag.AlignLeft,
        )
        value_box = QRectF(inner.left(), inner.top() + title_h, inner.width(), inner.height() - title_h)
        color = theme.value_color(item.status)
        draw_text(painter, value_box, item.value, value_font, color, Qt.AlignmentFlag.AlignLeft)
        if item.unit:
            advance = QFontMetrics(value_font).horizontalAdvance(item.value) + px(5)
            draw_text(
                painter,
                value_box.adjusted(advance, 0, 0, 0),
                item.unit,
                unit_font,
                theme.muted,
                Qt.AlignmentFlag.AlignLeft,
            )


def _tile(row: Row, title: str, fahrenheit: bool, prefix: str = "") -> Tile:
    number, _, unit = _value_text(row, fahrenheit).rpartition(" ")
    shown, unit = (number, unit) if number else ("—", "")
    if prefix:
        shown = f"{prefix} {shown}"
    return Tile(row.reading.key, f"{title}: {_value_text(row, fahrenheit)}", title, shown, unit, row.reading.status)


class RailView(TileView):
    """A board's named voltage rails (Vcore, +12V, +5V, +3.3V, the CMOS battery...)."""

    ACCESSIBLE_NAME = "Voltage rails"

    def load(self, data: ViewData) -> list[Item]:
        return [
            _tile(row, _label(data, row), data.fahrenheit)
            for row in data.rows
            if row.reading.kind is Kind.VOLTAGE and not UNNAMED_RAIL.match(row.reading.label)
        ]


class CpuTileView(TileView):
    """A CPU's headline numbers beside its heat map: power, clock, Vcore, load, hottest core."""

    ACCESSIBLE_NAME = "CPU summary"
    MAX_COLUMNS = 2

    def load(self, data: ViewData) -> list[Item]:
        rows = data.rows

        def first(kind: Kind, *labels: str, pool: Sequence[Row] = rows) -> Row | None:
            return next(
                (r for label in labels for r in pool if r.reading.kind is kind and r.reading.label == label), None
            )

        tiles: list[Item] = []
        for title, row in (
            ("Package power", first(Kind.POWER, "Package power")),
            ("Cores power", first(Kind.POWER, "Cores power", "Core power")),
        ):
            if row is not None:
                tiles.append(_tile(row, title, data.fahrenheit))
        clocks = [r for r in rows if r.reading.kind is Kind.CLOCK and r.reading.value is not None]
        if clocks:
            fastest = max(clocks, key=lambda r: r.reading.value or 0.0)
            tiles.append(_tile(fastest, "Fastest clock", data.fahrenheit))
        # The core voltage comes from the board's chip, or from zenpower on the CPU itself.
        vcore = first(Kind.VOLTAGE, "CPU core (Vcore)", pool=data.everything) or first(Kind.VOLTAGE, "Core voltage")
        if vcore is not None:
            tiles.append(_tile(vcore, "Vcore", data.fahrenheit))
        load = next((r for r in rows if r.reading.kind is Kind.LOAD and r.reading.label.startswith("CPU load")), None)
        if load is not None:
            tiles.append(_tile(load, "Load", data.fahrenheit))
        cores = [r for _, group in core_groups(rows) for r in group if r.reading.value is not None]
        if cores:
            hottest = max(cores, key=lambda r: r.reading.value or 0.0)
            tiles.append(_tile(hottest, "Hottest", data.fahrenheit, prefix=tile_name(hottest.reading.label)))
        return tiles


class CpuView(CardView):
    """A CPU card's view: the core heat map, with its headline numbers beside it (or below it,
    when the card is too narrow for both)."""

    SIDE_BY_SIDE = 820  # px the card needs for the tiles to sit beside the heat map

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.heat_map = CoreHeatMap()
        self.tiles = CpuTileView()
        self.heat_map.selected.connect(self.selected)
        self.tiles.selected.connect(self.selected)
        self.heat_map.opened.connect(self.opened)
        self.tiles.opened.connect(self.opened)
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(px(18))
        self.box.addWidget(self.heat_map, 3)
        self.box.addWidget(self.tiles, 2, Qt.AlignmentFlag.AlignTop)

    def set_data(self, data: ViewData) -> None:
        groups = core_groups(data.rows)
        self.heat_map.set_cores(groups, data.fahrenheit, dict(data.labels))
        self.heat_map.setVisible(self.heat_map.core_count() >= 2)
        self.tiles.set_data(data)
        self.tiles.setVisible(not self.tiles.is_empty())

    def set_selected(self, key: str | None) -> None:
        self.heat_map.set_selected(key)
        self.tiles.set_selected(key)

    def is_empty(self) -> bool:
        return self.heat_map.core_count() < 2 and self.tiles.is_empty()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        wide = self.width() >= px(self.SIDE_BY_SIDE)
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        if self.box.direction() != direction:
            self.box.setDirection(direction)


# ----- fans --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FanItem(Item):
    name: str = ""
    rpm: str = ""
    share: float | None = None  # 0-1: its duty, or else its speed against its own fastest
    where: str = ""
    spinning: bool = True
    status: Status = Status.OK
    unit: str = "RPM"
    driven: bool = False  # ``share`` is how hard it's driven, not a guess from its speed


def _fan_name(reading: Reading) -> str:
    """A fan's label without the device that two same-named fans gain in the Fans card
    ("GPU fan 1 speed (RTX 4070)" -> "GPU fan 1 speed")."""
    device = reading.origin or reading.device
    return reading.label.removesuffix(f" ({device.split(' · ', 1)[-1]})")


def fan_duties(rows: Sequence[Row]) -> dict[str, Row]:
    """Each fan's control setting (its duty, in %), by the fan's key."""
    duty_by_fan: dict[str, Row] = {}
    for row in rows:
        if row.reading.kind is not Kind.FAN_DUTY:
            continue
        if row.reading.companion:  # a board's "Fan control 2" names its "Fan 2"
            duty_by_fan[row.reading.companion] = row
        else:  # a graphics card's "GPU fan 1 speed" goes with its "GPU fan 1"
            fan = next(
                (
                    r
                    for r in rows
                    if r.reading.kind is Kind.FAN
                    and r.reading.origin == row.reading.origin
                    and f"{_fan_name(r.reading)} speed" == _fan_name(row.reading)
                ),
                None,
            )
            if fan is not None:
                duty_by_fan.setdefault(fan.reading.key, row)
    # A duty counts as paired only when its fan is here: a pump header with no speed input
    # names a fan that doesn't exist, and is shown on its own like a duty-only fan.
    fans = {row.reading.key for row in rows if row.reading.kind is Kind.FAN}
    return {fan: duty for fan, duty in duty_by_fan.items() if fan in fans}


class FanView(CompactView):
    """Every fan: its speed, a ten-step bar of how hard it's working, and where it is."""

    ACCESSIBLE_NAME = "Fans"
    ROW_H = 58

    def load(self, data: ViewData) -> list[Item]:
        rows = list(data.rows)
        duty_by_fan = fan_duties(rows)
        paired = {duty.reading.key for duty in duty_by_fan.values()}
        items: list[Item] = []
        for row in rows:
            if row.reading.kind is Kind.FAN_DUTY and row.reading.key not in paired:
                items.append(self._duty_only(data, row))  # a fan that reports no speed, only its %
            if row.reading.kind is not Kind.FAN:
                continue
            rpm = row.reading.value
            duty = duty_by_fan.get(row.reading.key)
            duty_value = duty.reading.value if duty is not None else None
            if duty_value is not None:
                share: float | None = duty_value / 100
            elif rpm is not None:
                share = rpm / max(row.stats.maximum or rpm, rpm, 1.0)
            else:
                share = None
            origin = row.reading.origin or row.reading.device
            where = "board header" if origin.startswith("Motherboard") else origin.partition(" · ")[2] or origin
            if duty_value is not None:
                where += f" · set to {duty_value:.0f} %"
            name = _label(data, row)
            items.append(
                FanItem(
                    row.reading.key,
                    f"{name}: {_value_text(row, False)}",
                    name,
                    format_value(Kind.FAN, rpm).removesuffix(" RPM") if rpm is not None else "—",
                    None if share is None else min(max(share, 0.0), 1.0),
                    where,
                    bool(rpm),
                    row.reading.status,
                    driven=duty_value is not None,
                )
            )
        return items

    @staticmethod
    def _duty_only(data: ViewData, row: Row) -> FanItem:
        duty = row.reading.value
        origin = row.reading.origin or row.reading.device
        name = _label(data, row)
        return FanItem(
            row.reading.key,
            f"{name}: {_value_text(row, False)}",
            name,
            f"{duty:.0f}" if duty is not None else "—",
            None if duty is None else min(max(duty / 100, 0.0), 1.0),
            "board header" if origin.startswith("Motherboard") else origin.partition(" · ")[2] or origin,
            bool(duty),
            row.reading.status,
            unit="%",
            driven=True,
        )

    def place(self, width: int) -> list[QRectF]:
        height = max(px(self.ROW_H), self._text_height() + px(14))
        return [QRectF(0, i * height, width, height) for i in range(len(self.items))]

    @staticmethod
    def _fonts() -> tuple[QFont, QFont, QFont, QFont]:
        return font(BODY_FONT, 14), font(MONO_FONT, 14, QFont.Weight.Bold), font(MONO_FONT, 11), font(BODY_FONT, 12)

    def _text_height(self) -> int:
        name, _, _, sub = self._fonts()
        return QFontMetrics(name).height() + px(8) + QFontMetrics(sub).height() + px(6)

    def paint_item(self, painter: QPainter, rect: QRectF, item: Item, theme: Theme) -> None:
        assert isinstance(item, FanItem)
        name_font, rpm_font, unit_font, sub_font = self._fonts()
        icon = px(26)
        left = rect.left() + px(10)
        _paint_fan(
            painter,
            QPointF(left + icon / 2, rect.center().y()),
            icon / 2,
            QColor(theme.accent if item.spinning else theme.muted),
        )
        x = left + icon + px(12)
        width = rect.right() - px(10) - x
        name_h, sub_h = QFontMetrics(name_font).height(), QFontMetrics(sub_font).height()
        top = rect.top() + (rect.height() - (name_h + px(8) + px(6) + sub_h)) / 2
        unit_w = QFontMetrics(unit_font).horizontalAdvance(item.unit)
        rpm_w = QFontMetrics(rpm_font).horizontalAdvance(item.rpm)
        line = QRectF(x, top, width, name_h)
        draw_text(
            painter,
            line.adjusted(0, 0, -(rpm_w + unit_w + px(10)), 0),
            item.name,
            name_font,
            theme.text,
            Qt.AlignmentFlag.AlignLeft,
        )
        draw_text(painter, line, item.unit, unit_font, theme.muted, Qt.AlignmentFlag.AlignRight)
        draw_text(
            painter,
            line.adjusted(0, 0, -(unit_w + px(5)), 0),
            item.rpm,
            rpm_font,
            theme.value_color(item.status),
            Qt.AlignmentFlag.AlignRight,
        )
        bar_top = top + name_h + px(3)
        # Amber means driven hard; a bar guessed from a fan's own top speed can't say.
        draw_segments(
            painter, QRectF(x, bar_top, width, px(6)), item.share or 0.0, theme, HARD if item.driven else None
        )
        draw_text(
            painter,
            QRectF(x, bar_top + px(6) + px(4), width, sub_h),
            item.where,
            sub_font,
            theme.muted,
            Qt.AlignmentFlag.AlignLeft,
        )


SEGMENTS = 10
HARD = 0.8  # a bar's last two steps, where a fan or a load counts as driven hard


def draw_segments(
    painter: QPainter,
    rect: QRectF,
    share: float,
    theme: Theme,
    warn_from: float | None = None,
    crit_from: float | None = None,
) -> None:
    """A bar of ten steps, lit as far as ``share`` (0 to 1) of it. A lit step reaching past
    ``warn_from`` (a share too) turns amber, and past ``crit_from`` red."""
    gap = px(3)
    step = (rect.width() - gap * (SEGMENTS - 1)) / SEGMENTS
    lit = 0 if math.isnan(share) else round(min(max(share, 0.0), 1.0) * SEGMENTS)  # a NaN reading: unlit
    for index in range(SEGMENTS):
        color = theme.edge
        if index < lit:
            reach = (index + 1) / SEGMENTS - 1e-9  # where the step ends
            if crit_from is not None and reach > crit_from:
                color = theme.critical
            elif warn_from is not None and reach > warn_from:
                color = theme.warning
            else:
                color = theme.accent
        painter.fillRect(QRectF(rect.left() + index * (step + gap), rect.top(), step, rect.height()), QColor(color))


def _paint_fan(painter: QPainter, center: QPointF, radius: float, color: QColor) -> None:
    """Four blades around a hub."""
    painter.save()
    painter.setPen(QPen(color, 1.4))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.translate(center)
    for _ in range(4):
        painter.drawEllipse(QRectF(radius * 0.1, -radius * 0.32, radius * 0.85, radius * 0.6))
        painter.rotate(90)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(0, 0), radius * 0.16, radius * 0.16)
    painter.restore()


# ----- storage -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DriveItem(Item):
    model: str = ""
    name: str = ""  # nvme0
    temperature: float | None = None
    high: float | None = None
    crit: float | None = None
    hottest: float | None = None
    status: Status = Status.OK


class StorageView(CompactView):
    """Each drive's temperature on a bar, with a mark where it starts to warn."""

    ACCESSIBLE_NAME = "Drive temperatures"
    ROW_H = 62
    SCALE = 90.0  # °C a full bar stands for, unless a drive's limits go higher

    def load(self, data: ViewData) -> list[Item]:
        drives: dict[str, list[Row]] = {}
        for row in data.rows:
            if row.reading.kind is Kind.TEMPERATURE:
                drives.setdefault(row.reading.origin or row.reading.device, []).append(row)
        items: list[Item] = []
        for origin, temps in sorted(drives.items(), key=lambda pair: _natural(drive_name(pair[0]))):
            main = temps[0]  # an NVMe drive lists its "Composite" first
            others = [r.reading.value for r in temps[1:] if r.reading.value is not None]
            # Another sensor only earns a mention when it's hotter than the one on the bar.
            hottest = max(others, default=None)
            if hottest is not None and main.reading.value is not None and hottest <= main.reading.value:
                hottest = None
            worst = max((r.reading.status for r in temps), key=SEVERITY.index)
            short = drive_name(origin)
            model = origin.partition(" · ")[2] or short
            named = f"{model} ({short})" if model != short else model
            items.append(
                DriveItem(
                    main.reading.key,
                    f"{named}: {_value_text(main, data.fahrenheit)}",
                    model,
                    short if model != short else "",
                    main.reading.value,
                    main.reading.high,
                    main.reading.crit,
                    hottest,
                    worst,
                )
            )
        return items

    def place(self, width: int) -> list[QRectF]:
        height = max(px(self.ROW_H), self._text_height() + px(14))
        return [QRectF(0, i * height, width, height) for i in range(len(self.items))]

    @staticmethod
    def _fonts() -> tuple[QFont, QFont, QFont]:
        return font(BODY_FONT, 14), font(MONO_FONT, 14, QFont.Weight.Bold), font(BODY_FONT, 12)

    def _text_height(self) -> int:
        name, _, sub = self._fonts()
        return QFontMetrics(name).height() + px(14) + QFontMetrics(sub).height()

    def paint_item(self, painter: QPainter, rect: QRectF, item: Item, theme: Theme) -> None:
        assert isinstance(item, DriveItem)
        name_font, value_font, sub_font = self._fonts()
        fahrenheit = self.data.fahrenheit
        x, width = rect.left() + px(10), rect.width() - px(20)
        name_h, sub_h = QFontMetrics(name_font).height(), QFontMetrics(sub_font).height()
        top = rect.top() + (rect.height() - (name_h + px(14) + sub_h)) / 2
        value = format_value(Kind.TEMPERATURE, item.temperature, fahrenheit)
        value_w = QFontMetrics(value_font).horizontalAdvance(value)
        line = QRectF(x, top, width, name_h)
        model_w = QFontMetrics(name_font).horizontalAdvance(item.model)
        room = width - value_w - px(12)
        draw_text(painter, QRectF(x, top, room, name_h), item.model, name_font, theme.text, Qt.AlignmentFlag.AlignLeft)
        if item.name and model_w + px(8) < room:
            draw_text(
                painter,
                QRectF(x + model_w + px(8), top, room - model_w - px(8), name_h),
                item.name,
                sub_font,
                theme.muted,
                Qt.AlignmentFlag.AlignLeft,
            )
        draw_text(painter, line, value, value_font, theme.value_color(item.status), Qt.AlignmentFlag.AlignRight)

        scale = max(self.SCALE, (item.crit or item.high or 0.0) + 5)
        bar = QRectF(x, top + name_h + px(5), width, px(5))
        painter.fillRect(bar, QColor(theme.edge))
        if item.temperature is not None:
            filled = bar.width() * min(max(item.temperature / scale, 0.0), 1.0)
            color = QColor(theme.accent) if item.status is Status.OK else theme.status(item.status)
            painter.fillRect(QRectF(bar.left(), bar.top(), filled, bar.height()), color)
        if item.high is not None:
            mark = bar.left() + bar.width() * min(item.high / scale, 1.0)
            painter.fillRect(QRectF(mark - 1, bar.top() - px(3), 2, bar.height() + px(6)), QColor(theme.warning))

        parts = []
        if item.high is not None:
            parts.append(f"warns at {format_value(Kind.TEMPERATURE, item.high, fahrenheit)}")
        if item.hottest is not None:
            parts.append(f"hottest sensor {format_value(Kind.TEMPERATURE, item.hottest, fahrenheit)}")
        draw_text(
            painter,
            QRectF(x, bar.bottom() + px(4), width, sub_h),
            " · ".join(parts),
            sub_font,
            theme.muted,
            Qt.AlignmentFlag.AlignLeft,
        )


# ----- network -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TrafficItem(Item):
    title: str = ""
    row: Row | None = field(default=None, compare=False)
    value: str = ""


class NetworkView(CompactView):
    """Download and upload, each with its last minute drawn; the card's temperatures under them."""

    ACCESSIBLE_NAME = "Network traffic"
    ROW_H = 38

    def load(self, data: ViewData) -> list[Item]:
        items: list[Item] = []
        for row in data.rows:
            if row.reading.kind is Kind.THROUGHPUT:
                name = _label(data, row)
                items.append(
                    TrafficItem(
                        row.reading.key, f"{name}: {_value_text(row, False)}", name, row, _value_text(row, False)
                    )
                )
        return items

    def temperatures(self) -> str:
        return " · ".join(
            f"{_label(self.data, r).removesuffix(' Temperature').removesuffix(' temperature')} "
            f"{_value_text(r, self.data.fahrenheit)}"
            for r in self.data.rows
            if r.reading.kind is Kind.TEMPERATURE
        )

    def place(self, width: int) -> list[QRectF]:
        height = max(px(self.ROW_H), QFontMetrics(font(MONO_FONT, 14, QFont.Weight.Bold)).height() + px(14))
        return [QRectF(0, i * height, width, height) for i in range(len(self.items))]

    def footer_height(self) -> int:
        return QFontMetrics(font(BODY_FONT, 12)).height() + px(8) if self.temperatures() else 0

    def paint_item(self, painter: QPainter, rect: QRectF, item: Item, theme: Theme) -> None:
        assert isinstance(item, TrafficItem)
        title_font, value_font = _caption_font(), font(MONO_FONT, 14, QFont.Weight.Bold)
        metrics = QFontMetrics(title_font)
        titles = (i.title.upper() for i in self.items if isinstance(i, TrafficItem))
        title_w = max((metrics.horizontalAdvance(t) + 1 for t in titles), default=0)
        title_w = max(title_w, px(60))
        value_w = QFontMetrics(value_font).horizontalAdvance("888.8 MB/s")
        x = rect.left() + px(10)
        draw_text(
            painter,
            QRectF(x, rect.top(), title_w, rect.height()),
            item.title.upper(),
            title_font,
            theme.muted,
            Qt.AlignmentFlag.AlignLeft,
        )
        spark = QRectF(
            x + title_w + px(12), rect.top() + px(8), rect.width() - title_w - value_w - px(44), rect.height() - px(16)
        )
        if item.row is not None and spark.width() > 0:
            draw_sparkline(painter, spark, item.row, self.data.now, self.data.gap, theme)
        draw_text(
            painter,
            QRectF(rect.right() - px(10) - value_w, rect.top(), value_w, rect.height()),
            item.value,
            value_font,
            theme.text,
            Qt.AlignmentFlag.AlignRight,
        )

    def paint_footer(self, painter: QPainter, rect: QRectF, theme: Theme) -> None:
        draw_text(
            painter,
            rect.adjusted(px(10), px(4), -px(10), 0),
            self.temperatures(),
            font(BODY_FONT, 12),
            theme.muted,
            Qt.AlignmentFlag.AlignLeft,
        )


# ----- which view, and how wide ------------------------------------------------------------


def view_for(device: str) -> CardView | None:
    """The compact view a device's card opens on, or None for a card that's just its list."""
    if device.startswith("CPU"):
        return CpuView()
    if device == FANS:
        return FanView()
    if device == STORAGE:
        return StorageView()
    if device.startswith("Motherboard"):
        return RailView()
    if device.startswith("Network"):
        return NetworkView()
    return None


def is_wide(device: str) -> bool:
    """CPU and GPU cards take the deck's whole width; the rest go two to a line where they fit."""
    return device.startswith(("CPU", "GPU"))


# Cards in the order the deck shows them: the wide ones first, then the half-width ones.
CARD_ORDER = ("CPU", "GPU", FANS, STORAGE, "Motherboard", "Network", "Memory", "Wi-Fi")


def card_rank(device: str) -> int:
    return next((i for i, prefix in enumerate(CARD_ORDER) if device.startswith(prefix)), len(CARD_ORDER))


def _narrowest(widget: QWidget) -> int:
    """How narrow a layout will let ``widget`` get: its hint, or a minimum set on it."""
    return widget.minimumSizeHint().expandedTo(widget.minimumSize()).width()


class CardDeck(QWidget):
    """The device cards: wide ones across the whole deck, the rest two to a line when both fit,
    else one under another."""

    MIN_HALF = 420  # px a half-width card needs for its compact view
    SPACING = 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(self.SPACING)
        self.cards: list[tuple[QWidget, bool]] = []  # (card, wide)
        self.needs: dict[int, int] = {}  # id(card) -> the width it needs beside another
        self._arrangement: tuple[tuple[int, int, int, int, int], ...] | None = None
        # Where a dragged panel would land, while one is dragged in edit-layout mode.
        self.drop_line = QWidget(self)
        self.drop_line.setObjectName("dropLine")
        self.drop_line.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.drop_line.setVisible(False)
        self._drop: tuple[QWidget, bool] | None = None
        self._drop_wide = False  # whether the panel dragged is a wide one

    def set_cards(self, cards: Sequence[tuple[QWidget, bool]]) -> None:
        self.cards = list(cards)
        if self._drop is not None and not any(self._drop[0] is card for card, _ in self.cards):
            self.mark_drop(None)  # its card is gone
        for card, _ in self.cards:
            if card.parentWidget() is not self:
                card.setParent(self)  # a card with no parent would open as a window of its own
        current = {id(card) for card, _ in self.cards}
        self.needs = {key: need for key, need in self.needs.items() if key in current}
        self.reflow()

    def set_need(self, card: QWidget, width: int) -> None:
        """How wide ``card`` must be to sit beside another (wider when it shows its full list)."""
        if self.needs.get(id(card)) != width:
            self.needs[id(card)] = width
            self.reflow()

    def columns_for(self, width: int) -> int:
        halves = [card for card, wide in self.cards if not wide and not card.isHidden()]
        # Never narrower than a card can go (a long board name, its header's button).
        need = max(
            (max(self.needs.get(id(card), px(self.MIN_HALF)), _narrowest(card)) for card in halves),
            default=px(self.MIN_HALF),
        )
        return 2 if width >= need * 2 + self.SPACING else 1

    def arrangement(self, width: int) -> list[tuple[QWidget, int, int, int, int]]:
        """(card, row, column, row span, column span) for every shown card."""
        columns = self.columns_for(width)
        placed: list[tuple[QWidget, int, int, int, int]] = []
        row = column = 0
        for card, wide in self.cards:
            if card.isHidden():
                continue
            if wide or columns == 1:
                if column:
                    row, column = row + 1, 0
                placed.append((card, row, 0, 1, columns))
                row += 1
            else:
                placed.append((card, row, column, 1, 1))
                column += 1
                if column == columns:
                    row, column = row + 1, 0
        return placed

    def reflow(self) -> None:
        placed = self.arrangement(self.width())
        key = tuple((id(card), r, c, rs, cs) for card, r, c, rs, cs in placed)
        if key == self._arrangement:
            return
        self._arrangement = key
        for card, _ in self.cards:
            self.grid.removeWidget(card)
        for column in range(self.grid.columnCount()):
            self.grid.setColumnStretch(column, 0)
        for card, r, c, rs, cs in placed:
            self.grid.addWidget(card, r, c, rs, cs)
        columns = max((c + cs for _, _, c, _, cs in placed), default=1)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)

    def drop_at(self, point: QPoint, wide: bool = False) -> tuple[QWidget, bool] | None:
        """The shown card a panel (``wide`` or not) dropped at ``point`` would land beside, and
        whether before it (over its upper half, or its left half when they'd sit side by side)
        or after it. None away from the deck."""
        if not self.rect().adjusted(-40, -40, 40, 40).contains(point):
            return None
        shown = [card for card, *_ in self.arrangement(self.width())]
        if not shown:
            return None

        def distance(card: QWidget) -> int:
            rect = card.geometry()
            dx = max(rect.left() - point.x(), 0, point.x() - rect.right())
            dy = max(rect.top() - point.y(), 0, point.y() - rect.bottom())
            return dx + dy

        card = min(shown, key=distance)  # the one under the pointer, or the nearest
        middle = card.geometry().center()
        if self._beside(card, wide):  # its left half is before it, its right after
            return card, point.x() < middle.x()
        return card, point.y() < middle.y()

    def _beside(self, card: QWidget, wide: bool) -> bool:
        """Whether a panel (``wide`` or not) dropped next to ``card`` would sit beside it: both
        half-width, two to a line. Not whether ``card`` has a neighbour now: one alone on its
        line (the next is wide, or it's the last) takes a half-width panel beside it too."""
        if wide:
            return False
        placed = self.arrangement(self.width())
        span = next((cs for c, _, _, _, cs in placed if c is card), None)
        return span == 1 and self.columns_for(self.width()) == 2

    def mark_drop(self, target: tuple[QWidget, bool] | None, wide: bool = False) -> None:
        """Draw the drop line before or after a card, or take it away (None): across the card
        over or under it, or, where the panel dragged (``wide`` or not) would sit beside it, down
        its side."""
        self._drop = target if target is not None and any(target[0] is c for c, _ in self.cards) else None
        self._drop_wide = wide
        if self._drop is None:
            self.drop_line.setVisible(False)
            return
        card, before = self._drop
        if wide and before and self._beside(card, False):
            # A wide panel before the right-hand card of a pair goes under the left-hand one,
            # so the line goes there too: the spot its lower half marks.
            placed = self.arrangement(self.width())
            spot = next(((r, c) for w, r, c, *_ in placed if w is card), None)
            left = next((w for w, r, c, *_ in placed if spot == (r, c + 1)), None)  # None in column 0
            if left is not None:
                card, before = left, False
        rect = card.geometry()
        thickness = 4
        ahead, behind = (self.SPACING + thickness) // 2, (self.SPACING - thickness) // 2  # in the gap
        if self._beside(card, wide):
            x = rect.left() - ahead if before else rect.right() + behind
            x = min(max(x, 0), self.width() - thickness)
            self.drop_line.setGeometry(x, rect.top(), thickness, rect.height())
        else:
            y = rect.top() - ahead if before else rect.bottom() + behind
            y = min(max(y, 0), self.height() - thickness)  # after the last card: still inside the deck
            self.drop_line.setGeometry(rect.left(), y, rect.width(), thickness)
        self.drop_line.setVisible(True)
        self.drop_line.raise_()

    def minimumSizeHint(self) -> QSize:
        # One card's width, not the current lines side by side, or the deck could never narrow
        # enough to put them one under another. And the height its cards take at the width it
        # has: the grid's own minimum stacks every card at its narrowest, which the scroll area
        # would honour as a long blank tail under the last line.
        widths = [card.minimumSizeHint().width() for card, _ in self.cards if not card.isHidden()]
        width = max(self.width(), *widths, 0)
        return QSize(
            max(widths, default=0),
            self.heightForWidth(width) if self.hasHeightForWidth() else self.grid.minimumSize().height(),
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.reflow()
        if self._drop is not None:  # the cards moved under it
            self.mark_drop(self._drop, self._drop_wide)
