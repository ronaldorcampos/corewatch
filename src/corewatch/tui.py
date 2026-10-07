"""Terminal user interface built on Textual."""

from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Static

from corewatch.model import Row, Status, format_limit, format_value
from corewatch.monitor import Monitor, gather_fans, group_rows

INTERVAL_STEPS = [0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
COLUMNS = [
    ("Sensor", "sensor"),
    ("Value", "value"),
    ("Min", "min"),
    ("Max", "max"),
    ("Average", "avg"),
    ("Limit", "limit"),
]
STATUS_STYLES = {Status.OK: "", Status.WARNING: "bold yellow", Status.CRITICAL: "bold red"}


class CorewatchApp(App[None]):
    TITLE = "corewatch"
    CSS = """
    #notes { color: $warning; padding: 0 1; }
    #notes.empty { display: none; }
    DataTable { height: 1fr; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "reset", "Reset min/max"),
        Binding("f", "toggle_unit", "°C / °F"),
        Binding("plus,equals_sign", "faster", "Faster"),
        Binding("minus", "slower", "Slower"),
        Binding("u", "toggle_unused", "Unused sensors"),
    ]

    def __init__(self, monitor: Monitor, interval: float = 1.0, fahrenheit: bool = False) -> None:
        super().__init__()
        self.monitor = monitor
        self.interval = interval
        self.fahrenheit = fahrenheit
        self.show_unused = False
        self._layout: list[str] = []
        self._all_rows: list[Row] = []
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="notes", classes="empty")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for label, key in COLUMNS:
            table.add_column(label, key=key)
        self.refresh_readings()
        self._restart_timer()

    def _restart_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_interval(self.interval, self.refresh_readings)
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        self.sub_title = f"updating every {self.interval:g} s · {'°F' if self.fahrenheit else '°C'}"

    def refresh_readings(self) -> None:
        self._all_rows = self.monitor.sample()
        self._show(self._all_rows)
        notes = self.monitor.notes()
        widget = self.query_one("#notes", Static)
        widget.update("\n".join(f"• {note}" for note in notes))
        widget.set_class(not notes, "empty")

    def _show(self, rows: list[Row]) -> None:
        self.render_rows(gather_fans(self.monitor.visible_rows(rows, self.show_unused)))

    def _cells(self, row: Row) -> dict[str, Text]:
        reading, stats = row.reading, row.stats
        style = STATUS_STYLES[reading.status]
        return {
            "value": Text(format_value(reading.kind, reading.value, self.fahrenheit), style=style, justify="right"),
            "min": Text(format_value(reading.kind, stats.minimum, self.fahrenheit), justify="right"),
            "max": Text(format_value(reading.kind, stats.maximum, self.fahrenheit), justify="right"),
            "avg": Text(format_value(reading.kind, stats.average, self.fahrenheit), justify="right"),
            "limit": Text(format_limit(reading, self.fahrenheit), style="dim"),
        }

    def render_rows(self, rows: list[Row]) -> None:
        table = self.query_one(DataTable)
        groups = group_rows(rows)
        layout = [
            key for device, device_rows in groups for key in [f"device:{device}"] + [r.reading.key for r in device_rows]
        ]
        if layout != self._layout:
            cursor = table.cursor_row
            # Keep the cursor on the same sensor, not the same row number.
            current_key = self._layout[cursor] if 0 <= cursor < len(self._layout) else None
            table.clear()
            for device, device_rows in groups:
                table.add_row(Text(device, style="bold"), *[""] * (len(COLUMNS) - 1), key=f"device:{device}")
                for row in device_rows:
                    cells = self._cells(row)
                    table.add_row(
                        # Text, not str: a label with "[" in it must not be parsed as markup.
                        Text(f"  {row.reading.label}"),
                        *[cells[key] for _, key in COLUMNS[1:]],
                        key=row.reading.key,
                    )
            self._layout = layout
            if table.row_count:
                target = layout.index(current_key) if current_key in layout else min(cursor, table.row_count - 1)
                table.move_cursor(row=target)
            return
        for _, device_rows in groups:
            for row in device_rows:
                for column, cell in self._cells(row).items():
                    table.update_cell(row.reading.key, column, cell)

    def action_reset(self) -> None:
        # Redraw without a new reading: one taken right after the last tick measures CPU
        # load over a sliver of time and would seed min/max with noise.
        self.monitor.reset()
        self._show(self._all_rows)

    def action_toggle_unused(self) -> None:
        self.show_unused = not self.show_unused
        self._show(self._all_rows)

    def action_toggle_unit(self) -> None:
        self.fahrenheit = not self.fahrenheit
        self._show(self._all_rows)  # same readings in the other unit; no extra sample
        self._update_subtitle()

    def _step_interval(self, direction: int) -> None:
        steps = sorted({*INTERVAL_STEPS, self.interval})
        index = steps.index(self.interval) + direction
        self.interval = steps[max(0, min(index, len(steps) - 1))]
        self._restart_timer()

    def action_faster(self) -> None:
        self._step_interval(-1)

    def action_slower(self) -> None:
        self._step_interval(+1)
