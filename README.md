# corewatch

A Core Temp-style hardware monitor for Linux. It shows every temperature, fan speed, voltage, clock,
load and power reading your machine exposes, refreshes them at an interval you choose (0.25 s to
60 s), and keeps a minimum, maximum, average and 15-minute history for each one.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshot.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/screenshot-light.png">
  <img alt="corewatch showing CPU core temperatures with sparklines and a detail chart" src="docs/screenshot.png">
</picture>

## Features

- **One card per device** (CPU, GPU, motherboard, each NVMe drive, network card), plus a **Fans**
  card that gathers every fan from the motherboard and the GPU (the detail panel still names the
  device each fan belongs to). Within a card, sensors are grouped under Temperatures, Load, Clocks,
  Power, Fan speed, Fan control, Voltages and Traffic and laid out in two columns.
  Each row has a 60-second sparkline, the current value, min, max and average. Click anywhere on a
  card's header to fold it.
- **Detail panel** for the selected sensor: a 1, 5 or 15 minute chart with the hardware's limits
  drawn as dashed lines. Hover over the chart to read any point's value and time. Beside the chart:
  lowest and highest (with the time each happened), averages for the session, the last minute and
  the last 5 minutes, variation, trend, and time spent past a limit or at critical.
- **Readable names.** Intel hybrid CPUs show P-core 0-7 and E-core 0-7 instead of the kernel's
  gappy core IDs. Nuvoton motherboard chips get named voltage rails (Vcore, +12V, +5V, +3.3V, ...).
  Right-click any sensor and choose **Rename…** to give it your own name (handy for "Fan 2" →
  "CPU fan"); **Reset name** undoes it.
- **Pin sensors to the tray.** Right-click a sensor and choose **Pin to tray**, or use the button
  in the detail panel. Each pinned sensor gets its own tray icon showing its number, like Core
  Temp's per-core icons. With nothing pinned, one icon shows the CPU temperature. Right-click a
  pinned icon to unpin it, which also works for a sensor that has stopped reporting (it shows "?").
- **Noise hidden by default.** Empty motherboard fan headers and unconnected temperature probes are
  hidden (⋯ → Show unused sensors brings them back). A fan that stops after spinning is never
  hidden, and neither is anything in a warning state or a GPU fan idling at 0 RPM.
- **Comfortable to leave open.** Light and dark themes (following the desktop by default), a filter
  box (Ctrl+F), °C/°F and a Min / max toggle. **⋯ → Start when I log in** starts corewatch at
  every login (it adds `~/.config/autostart/corewatch.desktop`, which your desktop's Startup
  Applications settings also show), and **⋯ → Start minimized in the tray** starts it without
  opening the window, at login or any other time (it needs a system tray; at login corewatch
  waits up to 30 s for the panel's tray to appear, and opens the window if none does). Opening corewatch while it's already running
  brings up the running one instead of starting a second copy. Sensors are read on a background thread, so a slow
  driver never freezes the window.
  Settings are remembered in `~/.config/corewatch/corewatch.conf`.
- **Terminal view** (`corewatch tui`) for SSH sessions, and a **one-shot dump**
  (`corewatch dump`, or `--json` for scripts).

## What it reads

| Source | What you get |
|---|---|
| Kernel hwmon (`/sys/class/hwmon`) | CPU package and per-core temperatures, motherboard temperatures, fans, fan control, voltages, NVMe and network card temperatures, and anything else a driver publishes |
| `/proc/stat`, cpufreq | Load and clock speed per physical core |
| RAPL (`/sys/class/powercap`) | CPU package and core power draw (needs one permission tweak, see below) |
| NVIDIA NVML | GPU temperature, fan speeds (RPM and %), power draw (the power limit is shown for reference, not as a warning), graphics and memory clocks, load, video memory |
| NVIDIA NvAPI (`libnvidia-api.so.1`) | GPU hotspot and memory (VRAM) temperatures, which NVML doesn't expose. Uses the same driver calls as [LACT](https://github.com/ilya-zlobintsev/LACT); works as a normal user on RTX 20-40 cards |
| `/sys/class/net` | Download and upload rates for each physical network interface with a link (Docker bridges, loopback and unplugged ports are left out) |

## Install

You need Linux, [uv](https://docs.astral.sh/uv/), and Python 3.13 (uv fetches it if needed). The
NVIDIA driver is optional; without it the GPU card simply doesn't appear.

```bash
git clone git@github.com:ronaldorcampos/corewatch.git
cd corewatch
uv tool install .
```

That puts `corewatch` in `~/.local/bin`. To add it to your app launcher (GNOME, KDE and others),
install the desktop entry, pointing it at the full path because launchers don't always search
`~/.local/bin`:

```bash
sed "s|^Exec=corewatch$|Exec=$HOME/.local/bin/corewatch|" packaging/corewatch.desktop > ~/.local/share/applications/corewatch.desktop
```

`uv tool install` takes a snapshot of the code. After pulling changes, refresh it with:

```bash
uv tool install --reinstall .
```

The tray icon needs a system tray. On GNOME that means the AppIndicator extension, which Ubuntu
ships enabled.

## Usage

```bash
corewatch                # desktop window
corewatch tui            # terminal view
corewatch dump           # print every reading once
corewatch dump --json    # the same, for scripts
```

Options: `-i/--interval SECONDS` (0.25-60, for this run only), `-f/--fahrenheit`, `--minimized`
(start in the tray without opening the window, whatever the setting says), and for `dump`,
`--all` to include unused inputs.

Terminal view keys: `q` quit · `r` reset min/max · `f` toggle °C/°F · `u` show/hide unused sensors ·
`+`/`-` faster/slower updates.

## Getting fan speeds and voltages

Most desktop boards report fans and voltages through a "Super I/O" chip, whose kernel driver often
isn't loaded by default. corewatch says so in a banner when it finds no fans or voltages.

On ASUS, MSI and ASRock boards from the last few years that chip is a Nuvoton, handled by
`nct6775` (newer ASUS boards, such as the ROG STRIX Z790 series, are reached through ASUS WMI):

```bash
sudo modprobe nct6775
```

To load it at every boot:

```bash
echo nct6775 | sudo tee /etc/modules-load.d/nct6775.conf
```

For other boards, `sudo sensors-detect` (from `lm-sensors`) finds the right module and offers to
add it to `/etc/modules`. corewatch picks up a newly loaded driver without a restart.

The driver doesn't name voltage inputs. On NCT679x chips corewatch names the ones the chip wires
internally on every board (AVCC, +3.3V, +3.3V standby, CMOS battery). On ASUS boards it also names
Vcore, +5V and +12V, and multiplies the last two back up from their 1:5 and 1:12 resistor
dividers. Check those three against your BIOS's Monitor page once. The remaining inputs are
board-specific and stay "Voltage N".

## Getting CPU power draw

Since 2020 the kernel only lets root read the RAPL energy counters, because power readings can leak
information through a side channel (CVE-2020-8694, "PLATYPUS"). On a personal desktop it is common
to make them readable anyway. Install the udev rule, then apply it without rebooting:

```bash
sudo cp packaging/99-corewatch-rapl.rules /etc/udev/rules.d/
```

```bash
sudo udevadm trigger --subsystem-match=powercap --action=add
```

Restart corewatch afterwards; it checks for the counters when it starts.

## Development

```bash
uv sync
prek install
uv run corewatch         # run from the checkout
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Sensor sources take their sysfs and procfs roots as constructor arguments, so the tests build fake
trees in a temporary directory and never touch real hardware. The GUI tests run on Qt's offscreen
platform.
