"""Your own arrangement of the overview's panels (the gauges and every device card): their order,
which take the whole width, and which are hidden. Changed in edit-layout mode, kept in settings."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QHideEvent, QIcon, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from corewatch.gui.cards import card_rank, is_wide
from corewatch.gui.theme import current_theme, paint_card, px
from corewatch.gui.widgets import ElidedLabel, chevron_icon

# The gauge row's panel name. Devices are named "Kind · model", "Fans" or "Storage", so it can't clash.
GAUGES = "Gauges"


def default_rank(panel: str) -> int:
    """Where a panel goes until it's moved: the gauges first, then the cards in their usual order."""
    return -1 if panel == GAUGES else card_rank(panel)


def default_wide(panel: str) -> bool:
    return panel == GAUGES or is_wide(panel)


@dataclass
class Layout:
    order: list[str] = field(default_factory=list)  # every panel ever arranged, first to last
    widths: dict[str, bool] = field(default_factory=dict)  # panel -> whole width, where it was changed
    hidden: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, text: object) -> "Layout":
        """From its settings value; anything unreadable is the default layout."""
        try:
            data = json.loads(str(text)) if text else {}
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        order = data.get("order")
        widths = data.get("widths")
        hidden = data.get("hidden")
        return cls(
            list(dict.fromkeys(p for p in order if isinstance(p, str))) if isinstance(order, list) else [],
            {p: w == "full" for p, w in widths.items() if w in ("full", "half")} if isinstance(widths, dict) else {},
            {p for p in hidden if isinstance(p, str)} if isinstance(hidden, list) else set(),
        )

    def dump(self) -> str:
        widths = {panel: "full" if wide else "half" for panel, wide in self.widths.items()}
        return json.dumps({"order": self.order, "widths": widths, "hidden": sorted(self.hidden)})

    def arrange(self, panels: Sequence[str]) -> list[str]:
        """``panels`` in your order. One never arranged yet (a new card) goes where it would by
        default: before the first panel that by default comes after it."""
        present = set(panels)
        arranged = [panel for panel in self.order if panel in present]
        for panel in sorted((p for p in panels if p not in arranged), key=default_rank):
            rank = default_rank(panel)
            at = next((i for i, other in enumerate(arranged) if default_rank(other) > rank), len(arranged))
            arranged.insert(at, panel)
        return arranged

    def wide(self, panel: str) -> bool:
        return self.widths.get(panel, default_wide(panel))

    def _keep(self, arranged: list[str], moved: str) -> None:
        """Take ``arranged`` as the order. A panel not on screen now (a card whose device is
        unplugged) stays just after the panel it followed, so it comes back where it was; if
        that's ``moved``, the panel being moved, after the one before that, as it stays put."""
        order = list(arranged)
        previous: str | None = None
        for panel in self.order:
            if panel not in arranged:
                order.insert(order.index(previous) + 1 if previous is not None else 0, panel)
            if panel != moved:
                previous = panel
        self.order = order

    def move(self, panel: str, step: int, arranged: Sequence[str], shown: Sequence[str]) -> None:
        """Move ``panel`` past the shown panel ``step`` places from it, as dropping it there
        would: before the one above, or after the one below. Hidden panels stay where they are."""
        if panel not in shown:
            return
        index = shown.index(panel) + step
        if not 0 <= index < len(shown):
            return
        other = shown[index]
        order = [p for p in arranged if p != panel]
        order.insert(order.index(other) + (0 if step < 0 else 1), panel)
        self._keep(order, panel)

    def drop(self, panel: str, before: str | None, arranged: Sequence[str]) -> None:
        """Put ``panel`` just before ``before`` (or last, for None)."""
        if panel == before or panel not in arranged:
            return
        order = [p for p in arranged if p != panel]
        order.insert(order.index(before) if before in order else len(order), panel)
        self._keep(order, panel)


# ----- edit-layout widgets --------------------------------------------------------------


def eye_off_icon(color: str) -> QIcon:
    """An eye struck through: hide this."""
    icon = QIcon()
    for size in (16, 32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), size / 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        eye = QRectF(size * 0.1, size * 0.28, size * 0.8, size * 0.44)
        painter.drawEllipse(eye)
        painter.drawEllipse(eye.center(), size * 0.1, size * 0.1)
        painter.drawLine(QPointF(size * 0.16, size * 0.84), QPointF(size * 0.84, size * 0.16))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class FlowRow(QLayout):
    """Widgets one after another, wrapping onto a new line when the width runs out."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        # The widest single item: never the whole row, or it couldn't wrap.
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect: QRect, move: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, line = rect.x(), y + line + self.spacing(), 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line = max(line, hint.height())
        return y + line - rect.y()


class CardFrame(QFrame):
    """A plain card: the frame and corner marks every card has."""

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        paint_card(painter, QRectF(self.rect()), current_theme())
        painter.end()


class Grip(QWidget):
    """Six dots to drag a panel by. Dragging is plain mouse tracking, held by the press."""

    started = Signal()
    moved = Signal(QPoint)  # where the pointer is, on screen
    ended = Signal(QPoint)
    cancelled = Signal()  # let go of without a drop

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(px(20), px(28))
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to move")
        self.lifted = False
        self._pressed: QPoint | None = None

    def cancel(self) -> None:
        """Let go of a drag without dropping anything (edit mode ended under it)."""
        self._pressed = None
        if self.lifted:
            self._land()
            self.cancelled.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        # Hidden in the middle of a drag (the window closed to the tray with the button held):
        # it can't see the release, so it lets go now rather than keep the closed hand app-wide.
        self.cancel()
        super().hideEvent(event)

    def _land(self) -> None:
        self.lifted = False
        QApplication.restoreOverrideCursor()
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pressed is None:
            return
        point = event.position().toPoint()
        if not self.lifted:
            if (point - self._pressed).manhattanLength() < QApplication.startDragDistance():
                return
            self.lifted = True
            # The closed hand wherever the pointer goes, not only over the grip.
            QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()
            self.started.emit()
        self.moved.emit(self.mapToGlobal(point))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._pressed is None:
            return
        self._pressed = None
        if self.lifted:
            self._land()
            self.ended.emit(self.mapToGlobal(event.position().toPoint()))

    def paintEvent(self, event: QPaintEvent) -> None:
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.accent if self.lifted else theme.muted))
        middle_x, middle_y, gap, dot = self.width() / 2, self.height() / 2, px(4), px(1.6)
        for dx in (-gap * 0.75, gap * 0.75):
            for dy in (-gap * 1.75, 0, gap * 1.75):
                painter.drawEllipse(QPointF(middle_x + dx, middle_y + dy), dot, dot)
        painter.end()


class LayoutBar(QWidget):
    """A panel's controls in edit-layout mode: its grip, name, earlier and later, half or whole
    width, and hide."""

    step = Signal(int)  # -1 earlier, 1 later
    width_chosen = Signal(bool)  # True: the whole width
    hide_requested = Signal()

    def __init__(self, name: str, sub: str, panel: str | None = None, parent: QWidget | None = None) -> None:
        """``name`` and ``sub`` as shown; ``panel`` is its full name, for screen readers."""
        super().__init__(parent)
        self.name = name
        full = panel or name
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.moving = QLabel("MOVING")  # while it's being dragged
        self.moving.setObjectName("moving")
        self.moving.setVisible(False)
        self.grip = Grip()
        self.grip.started.connect(lambda: self.moving.setVisible(True))
        self.grip.ended.connect(lambda _: self.moving.setVisible(False))
        self.grip.cancelled.connect(lambda: self.moving.setVisible(False))
        row.addWidget(self.grip)
        self.title = ElidedLabel(name, shortest=60)
        self.title.setObjectName("sectionTitle")
        row.addWidget(self.title)
        self.sub = ElidedLabel(sub, shortest=30)
        self.sub.setObjectName("muted")
        row.addWidget(self.sub, 1)
        row.addWidget(self.moving)
        self.up = QPushButton()
        self.down = QPushButton()
        for button, step, word in ((self.up, -1, "earlier"), (self.down, 1, "later")):
            button.setObjectName("iconButton")
            button.setAccessibleName(f"Move {full} {word}")
            button.setToolTip(f"Move {word}")
            button.clicked.connect(lambda _=False, step=step: self.step.emit(step))
            row.addWidget(button)
        self.widths = QButtonGroup(self)
        self.widths.setExclusive(True)
        self.half = QPushButton("½")
        self.full = QPushButton("Full")
        for button, wide, tip in ((self.half, False, "Half the width"), (self.full, True, "The whole width")):
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, wide=wide: self.width_chosen.emit(wide))
            self.widths.addButton(button)
        segments = QHBoxLayout()
        segments.setSpacing(0)
        segments.addWidget(self.half)
        segments.addWidget(self.full)
        row.addLayout(segments)
        self.hide_button = QPushButton()
        self.hide_button.setObjectName("iconButton")
        self.hide_button.setAccessibleName(f"Hide {full}")
        self.hide_button.setToolTip("Hide this panel (Show brings it back)")
        self.hide_button.clicked.connect(self.hide_requested)
        row.addWidget(self.hide_button)
        self.restyle()

    def cancel_drag(self) -> None:
        self.grip.cancel()
        self.moving.setVisible(False)

    def restyle(self) -> None:
        muted = current_theme().muted
        self.up.setIcon(chevron_icon(muted, up=True))
        self.down.setIcon(chevron_icon(muted, up=False))
        self.hide_button.setIcon(eye_off_icon(muted))

    def set_state(self, first: bool, last: bool, wide: bool) -> None:
        self.up.setEnabled(not first)
        self.down.setEnabled(not last)
        (self.full if wide else self.half).setChecked(True)

    def set_sub(self, sub: str) -> None:
        if self.sub.text() != sub:
            self.sub.setText(sub)


class GaugePanel(QFrame):
    """The gauge row as a panel of the deck: the gauges alone, or, while editing the layout, a
    card with the edit bar over them."""

    def __init__(self, strip: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editing = False
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(10)
        self.layout_bar: LayoutBar | None = None  # made the first time the layout is edited
        self.box.addWidget(strip)

    def edit_bar(self) -> LayoutBar:
        if self.layout_bar is None:
            self.layout_bar = LayoutBar(GAUGES, "CPU · GPU · load · power", panel=GAUGES)
            self.box.insertWidget(0, self.layout_bar)
        return self.layout_bar

    def set_editing(self, editing: bool) -> None:
        self.editing = editing
        if editing or self.layout_bar is not None:
            self.edit_bar().setVisible(editing)
        self.box.setContentsMargins(*((14, 10, 14, 14) if editing else (0, 0, 0, 0)))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.editing:
            painter = QPainter(self)
            paint_card(painter, QRectF(self.rect()), current_theme())
            painter.end()
