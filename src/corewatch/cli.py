"""Command line entry point: ``corewatch [gui|tui|dump]``."""

import argparse
import json
import sys
import time
from collections.abc import Collection, Sequence
from typing import TextIO

from corewatch.model import Row, format_limit, format_value
from corewatch.monitor import Monitor, group_rows
from corewatch.sources import default_sources

MIN_INTERVAL = 0.25
MAX_INTERVAL = 60.0


def interval_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise argparse.ArgumentTypeError(f"must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corewatch",
        description="Real-time temperatures, fan speeds, voltages, clocks and power for Linux.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["gui", "tui", "dump"],
        default="gui",
        help="gui: desktop window (default); tui: live view in the terminal; dump: print one reading and exit",
    )
    parser.add_argument("-i", "--interval", type=interval_arg, default=None, help="seconds between updates (0.25-60)")
    parser.add_argument("-f", "--fahrenheit", action="store_true", default=None, help="show temperatures in °F")
    parser.add_argument("--json", action="store_true", help="dump: print machine-readable JSON")
    parser.add_argument(
        "--all", action="store_true", help="dump: include unused inputs (empty fan headers, unconnected probes)"
    )
    return parser


def row_to_dict(row: Row, unused: bool = False) -> dict[str, object]:
    reading, stats = row.reading, row.stats
    return {
        "key": reading.key,
        "device": reading.device,
        "label": reading.label,
        "kind": reading.kind.value,
        "value": reading.value,
        "low": reading.low,
        "high": reading.high,
        "crit": reading.crit,
        "cap": reading.cap,
        "status": reading.status.value,
        "min": stats.minimum,
        "max": stats.maximum,
        "unused": unused,
    }


def write_dump(
    rows: Sequence[Row],
    notes: Sequence[str],
    out: TextIO,
    as_json: bool,
    fahrenheit: bool,
    unused_keys: Collection[str] = frozenset(),
) -> None:
    if as_json:
        ordered = [row for _, device_rows in group_rows(rows) for row in device_rows]
        json.dump(
            {"sensors": [row_to_dict(r, r.reading.key in unused_keys) for r in ordered], "notes": list(notes)},
            out,
            indent=2,
            ensure_ascii=False,
        )
        out.write("\n")
        return
    for device, device_rows in group_rows(rows):
        out.write(f"{device}\n")
        width = max(len(r.reading.label) for r in device_rows)
        for row in device_rows:
            value = format_value(row.reading.kind, row.reading.value, fahrenheit)
            limit = format_limit(row.reading, fahrenheit)
            out.write(f"  {row.reading.label:<{width}}  {value:>12}  {limit}".rstrip() + "\n")
        out.write("\n")
    for note in notes:
        out.write(f"Note: {note}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "gui":
        from corewatch.gui.app import run_gui

        return run_gui(interval=args.interval, fahrenheit=args.fahrenheit)

    monitor = Monitor(default_sources())
    try:
        if args.mode == "tui":
            from corewatch.tui import CorewatchApp

            CorewatchApp(monitor, interval=args.interval or 1.0, fahrenheit=bool(args.fahrenheit)).run()
            return 0
        # Load and power are measured over time, so give the counters a moment.
        time.sleep(args.interval or 0.5)
        everything = monitor.sample()
        rows = monitor.visible_rows(everything, args.all)
        write_dump(rows, monitor.notes(), sys.stdout, args.json, bool(args.fahrenheit), monitor.unused_rows(everything))
        return 0
    finally:
        monitor.close()
