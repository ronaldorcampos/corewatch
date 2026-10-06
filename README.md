# corewatch

A Core Temp-style hardware monitor for Linux. It shows every temperature, fan speed, voltage, clock,
load and power reading your machine exposes, refreshing at an interval you choose (0.25 s to 60 s),
and keeps a minimum, maximum, average and short history for each one.

- **Desktop window** (default): one card per device, sensors in two columns under Temperatures /
  Load / Clocks / Fans / Voltages headings, each with a 60-second sparkline, value, min, max and
  average (the number columns size themselves to their values). Selecting a sensor opens a detail
  panel with a 1, 5 or 15 minute chart (limits drawn as dashed lines) and full statistics:
  lowest and highest with the time each happened, averages over the session, the last minute and
  the last 5 minutes, variation, trend, time spent past a limit, number of readings and how long
  it has been watching. CPU cores are numbered the way you'd count them (P-core 0-7, E-core 0-7
  on Intel hybrid chips) rather than by the kernel's gappy core IDs. Light and dark themes
  (follows the desktop by default), a filter box, °C/°F, and a tray icon that shows the CPU
  temperature as a number. Sensors are read on a background thread, so a slow driver never
  freezes the window.
- **Terminal view**: `corewatch tui`, the same data in your terminal (works over SSH).
- **One-shot dump**: `corewatch dump`, or `corewatch dump --json` for scripts.

## What it reads

| Source | What you get |
|---|---|
| Kernel hwmon (`/sys/class/hwmon`) | CPU package and per-core temperatures, motherboard temperatures, fans, fan control, voltages, NVMe and network card temperatures, and anything else a driver publishes |
| `/proc/stat`, cpufreq | Load and clock speed per physical core |
| RAPL (`/sys/class/powercap`) | CPU package and core power draw (needs a permission tweak, see below) |
| NVIDIA NVML | GPU temperature, fan speeds, power draw (its power limit is shown for reference, not as a warning), graphics and memory clocks, load, video memory |

## Install and run

```bash
uv sync
uv run corewatch            # desktop window
uv run corewatch tui        # terminal
uv run corewatch dump --json
```

Options: `-i/--interval SECONDS`, `-f/--fahrenheit`. Window settings (interval, unit, theme, layout,
selected sensor) are remembered in `~/.config/corewatch/corewatch.conf`.

To put it on your PATH and in the app launcher:

```bash
uv tool install .
cp packaging/corewatch.desktop ~/.local/share/applications/
```

### Terminal keys

`q` quit · `r` reset min/max · `f` toggle °C/°F · `u` show/hide unused sensors · `+`/`-` faster/slower updates.

## Getting fan speeds and voltages

Most desktop boards report fans and voltages through a "Super I/O" chip, whose kernel driver often
isn't loaded by default. corewatch says so in a banner when it finds no fans or voltages.

On ASUS, MSI and ASRock boards from the last few years that chip is a Nuvoton, handled by `nct6775`
(this includes the ROG STRIX Z790-A GAMING WIFI, which the driver accesses through ASUS WMI):

```bash
sudo modprobe nct6775
```

To load it at every boot:

```bash
echo nct6775 | sudo tee /etc/modules-load.d/nct6775.conf
```

For other boards, `sudo sensors-detect` (from `lm-sensors`) finds the right module.

The driver doesn't name voltage inputs. On Nuvoton NCT679x chips corewatch names the ones the chip
wires internally on every board (AVCC, +3.3V, +3.3V standby, CMOS battery). On ASUS boards it also
names Vcore, +5V and +12V and multiplies the last two back up from their 1:5 and 1:12 resistor
dividers. Check those three against your BIOS's Monitor page once. The remaining inputs are
board-specific and stay "Voltage N".

Empty fan headers, the speed controls for them, and temperature inputs with no probe attached
(they read 127 °C or 0 °C) are hidden by default. A fan only counts as empty if it hasn't spun
since corewatch started, so a fan that stops later stays on screen. Turn on
**⋯ → Show unused sensors** (`u` in the terminal view, `--all` for `dump`) to see everything.

## Getting CPU power draw

Since 2020 the kernel only lets root read the RAPL energy counters, because power readings can leak
information through a side channel (CVE-2020-8694, "PLATYPUS"). On a personal desktop it is common
to make them readable anyway. To do it once, until the next reboot:

```bash
sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj /sys/class/powercap/intel-rapl:*:*/energy_uj
```

To make it permanent:

```bash
sudo cp packaging/99-corewatch-rapl.rules /etc/udev/rules.d/
```

The rule applies from the next boot.

## Development

```bash
uv sync
prek install
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Sensor sources take their sysfs/procfs roots as constructor arguments, so the tests build fake trees
in a temporary directory and never touch real hardware. The GUI tests run on Qt's offscreen platform.
