"""Start corewatch when you log in, through the desktop's standard autostart folder.

GNOME, KDE, Xfce and the rest all run the ``.desktop`` files in
``$XDG_CONFIG_HOME/autostart`` (normally ``~/.config/autostart``) at login, and their
"Startup Applications" settings show and edit the same files. So the file itself is the
setting: whether it's there, and not switched off, is whether corewatch starts at login.
Whether it then opens its window or stays in the tray is the separate "Start minimized"
setting, which applies to every start.
"""

import contextlib
import os
import pwd
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

FILE_NAME = "corewatch.desktop"
# Marks the file as ours, so turning the option off never deletes someone else's entry.
MARKER_KEY = "X-Corewatch-Autostart"
# Keys a desktop uses to switch an autostart entry off without deleting it.
OFF_SWITCHES = {"Hidden": "true", "X-GNOME-Autostart-enabled": "false"}
# Tells corewatch it was started by the desktop at login (it then waits for the tray).
LOGIN_FLAG = "--login"
# Characters that need an argument quoted in an Exec line (Desktop Entry spec, "Exec key").
RESERVED = re.compile(r"""[\s"'\\><~|&;$*?#()`]""")


class AutostartError(Exception):
    """The entry can't be changed safely; the message says why, in plain words."""


def autostart_path(environ: Mapping[str, str] = os.environ) -> Path:
    """``$XDG_CONFIG_HOME/autostart/corewatch.desktop``, ignoring a relative value as the
    XDG spec requires, else ``~/.config/autostart/corewatch.desktop``."""
    config = environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).is_absolute():
        home = environ.get("HOME", "")
        if not home or not Path(home).is_absolute():
            home = pwd.getpwuid(os.getuid()).pw_dir  # the account's real home, not a relative $HOME
        config = str(Path(home) / ".config")
    return Path(config) / "autostart" / FILE_NAME


def launch_command() -> list[str]:
    """How to start corewatch again at login: the installed command if there is one.

    Under ``uv run`` the project's own environment comes first on PATH; prefer a
    ``corewatch`` from anywhere else (the ``uv tool install`` one), so the login entry
    doesn't depend on a development checkout. The path is absolute but not
    symlink-resolved: ``~/.local/bin/corewatch`` is a link, and the link is what stays put.
    """
    running_env = Path(sys.prefix).resolve()
    found = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and (candidate := shutil.which("corewatch", path=directory)):
            found.append(Path(candidate).absolute())
    inside = [path for path in found if path.resolve().is_relative_to(running_env)]
    outside = [path for path in found if path not in inside]
    # Running the uv-installed tool (its environment carries uv's receipt): point at the
    # link to this very install, even if some development environment is earlier on PATH.
    preferred = inside if (running_env / "uv-receipt.toml").exists() else outside
    if preferred or found:
        return [str((preferred or found)[0])]
    script = Path(sys.argv[0])
    if script.name == "corewatch" and script.exists():
        return [str(script.absolute())]
    return [sys.executable, "-m", "corewatch"]


def quote_exec(arguments: list[str]) -> str:
    """An Exec value: each argument quoted where needed, then the key file's own escaping."""
    quoted = []
    for argument in arguments:
        if "\n" in argument or "\r" in argument:
            raise AutostartError("The corewatch command's path contains a line break, which a login entry can't hold.")
        argument = argument.replace("%", "%%")  # a single % starts a field code like %f
        if not argument or RESERVED.search(argument):
            argument = '"' + re.sub(r'(["`$\\])', r"\\\1", argument) + '"'
        quoted.append(argument)
    # Key files unescape backslashes once before the Exec rules see them, so double them.
    return " ".join(quoted).replace("\\", "\\\\")


def entry(command: list[str]) -> str:
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=corewatch",
            "Comment=Hardware monitor: temperatures, fans, voltages, clocks and power",
            f"Exec={quote_exec([*command, LOGIN_FLAG])}",
            "Icon=utilities-system-monitor",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            f"{MARKER_KEY}=true",
            "",
        ]
    )


def _keys(text: str) -> dict[str, str]:
    """``key=value`` pairs, allowing the spaces around ``=`` that the spec permits."""
    pairs = {}
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if equals and not line.lstrip().startswith(("#", "[")):
            pairs[key.strip()] = value.strip()
    return pairs


def _is_off_switch(line: str) -> bool:
    keys = _keys(line)
    return any(keys.get(key, "").lower() == value for key, value in OFF_SWITCHES.items())


def _read(path: Path) -> str | None:
    """The entry's text; None if there's no file. Raises AutostartError if it can't be read."""
    try:
        return path.read_text(encoding="utf-8")  # the spec requires UTF-8
    except (FileNotFoundError, NotADirectoryError):  # nothing there (or no autostart folder)
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise AutostartError(f"{path} can't be read, so corewatch left it alone ({error}).") from error


def is_enabled(path: Path) -> bool:
    """True if an autostart entry exists and hasn't been switched off."""
    try:
        text = _read(path)
    except AutostartError:
        return False
    return text is not None and not any(_is_off_switch(line) for line in text.splitlines())


def is_ours(text: str) -> bool:
    return _keys(text).get(MARKER_KEY, "").lower() == "true"


def enable(path: Path, command: list[str] | None = None) -> str:
    """Turn starting at login on. Returns what happened, in plain words.

    An entry corewatch didn't write is never replaced: if it was only switched off, it is
    switched back on in place, keeping everything else in it.
    """
    text = _read(path)
    if text is not None and not is_ours(text):
        kept = [line for line in text.splitlines() if not _is_off_switch(line)]
        # Edit it where it really lives (a dotfiles symlink stays a symlink), keeping its mode.
        target = path.resolve()
        _write(target, "\n".join(kept) + "\n", mode=target.stat().st_mode & 0o777)
        return f"Your own login entry ({path}) was switched back on"
    command = command or launch_command()
    _write(path, entry(command))
    return f"corewatch will start when you log in (runs {command[0]})"


def _program(exec_value: str) -> tuple[str, str] | None:
    """Split an Exec value into (program, the rest as written), undoing quote_exec."""
    text = exec_value.replace("\\\\", "\x00").replace("\\", "").replace("\x00", "\\")  # key-file level
    if text.startswith('"'):
        program, index = "", 1
        while index < len(text) and text[index] != '"':
            if text[index] == "\\" and index + 1 < len(text):
                index += 1
            program += text[index]
            index += 1
        if index >= len(text):
            return None  # unbalanced quotes: not something we wrote
        consumed = index + 1
    else:
        program, _, _ = text.partition(" ")
        consumed = len(program)
    # Map back onto the raw value to keep the rest exactly as it was written.
    raw_quoted = quote_exec([program.replace("%%", "%")])
    if not exec_value.startswith(raw_quoted) or consumed < 1:
        return None
    return program.replace("%%", "%"), exec_value[len(raw_quoted) :]


def refresh(path: Path, command: list[str] | None = None) -> bool:
    """If our own enabled entry starts a corewatch that no longer exists (moved or removed
    install), point it at this one. Only the program path changes: any flags you added and
    anything a desktop's editor wrote stay as they are. Returns True if the file was changed."""
    try:
        text = _read(path)
    except AutostartError:
        return False
    if text is None or not is_ours(text) or not is_enabled(path):
        return False
    lines = text.splitlines()
    for number, line in enumerate(lines):
        key, equals, value = line.partition("=")
        if not equals or key.strip() != "Exec":
            continue
        parsed = _program(value.strip())
        if parsed is None:
            return False
        program, rest = parsed
        new_program = (command or launch_command())[0]
        if program == new_program or Path(program).exists():
            return False
        lines[number] = f"Exec={quote_exec([new_program])}{rest}"
        target = path.resolve()
        _write(target, "\n".join(lines) + "\n", mode=target.stat().st_mode & 0o777)
        return True
    return False


def disable(path: Path) -> bool:
    """Remove our entry. A file corewatch didn't write (or can't read) is left alone; returns
    False then, and the desktop's Startup Applications settings are the place to change it."""
    try:
        text = _read(path)
    except AutostartError:
        return False
    if text is None:
        return True
    if not is_ours(text):
        return False
    path.unlink(missing_ok=True)
    return True


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write the whole file, then move it into place, so there's never a half-written entry;
    a failed write leaves nothing behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".corewatch-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
