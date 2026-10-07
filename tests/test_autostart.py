import os
import shutil
import subprocess
from pathlib import Path

import pytest

from corewatch import autostart


def test_path_follows_xdg_config_home_then_home() -> None:
    assert autostart.autostart_path({"XDG_CONFIG_HOME": "/x/conf", "HOME": "/home/u"}) == Path(
        "/x/conf/autostart/corewatch.desktop"
    )
    assert autostart.autostart_path({"HOME": "/home/u"}) == Path("/home/u/.config/autostart/corewatch.desktop")
    # The XDG spec says to ignore a relative XDG_CONFIG_HOME; a relative or empty HOME falls back too.
    assert autostart.autostart_path({"XDG_CONFIG_HOME": "rel/conf", "HOME": "/home/u"}).parent == Path(
        "/home/u/.config/autostart"
    )
    assert autostart.autostart_path({"HOME": ""}).is_absolute()


def test_enable_writes_a_desktop_entry(tmp_path: Path) -> None:
    path = tmp_path / "autostart" / "corewatch.desktop"
    message = autostart.enable(path, ["/home/u/.local/bin/corewatch"])
    text = path.read_text()
    assert "Exec=/home/u/.local/bin/corewatch --login" in text.splitlines()
    assert text.startswith("[Desktop Entry]\n") and "X-Corewatch-Autostart=true" in text
    assert "--minimized" not in text  # whether to start in the tray is its own setting
    assert "runs /home/u/.local/bin/corewatch" in message
    assert autostart.is_enabled(path)
    assert list(path.parent.iterdir()) == [path]  # no temporary file left behind
    assert oct(path.stat().st_mode & 0o777) == "0o644"


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("/home/u/.local/bin/corewatch", "/home/u/.local/bin/corewatch"),
        ("/opt/my apps/corewatch", '"/opt/my apps/corewatch"'),
        ('/opt/a"b$c`d/corewatch', '"/opt/a\\\\"b\\\\$c\\\\`d/corewatch"'),
        ("/opt/back\\slash/corewatch", '"/opt/back\\\\\\\\slash/corewatch"'),
        ("/opt/100%s/corewatch", "/opt/100%%s/corewatch"),
    ],
)
def test_exec_quoting_follows_the_desktop_entry_spec(argument: str, expected: str) -> None:
    assert autostart.quote_exec([argument]) == expected


def test_exec_with_a_line_break_is_refused() -> None:
    with pytest.raises(autostart.AutostartError):
        autostart.quote_exec(["/opt/bad\npath"])


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-utils not installed")
def test_awkward_paths_still_give_a_valid_entry(tmp_path: Path) -> None:
    path = tmp_path / "corewatch.desktop"
    autostart.enable(path, ['/opt/my "odd" $dir\\with~stuff/corewatch'])
    result = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_switched_off_entries_count_as_disabled(tmp_path: Path) -> None:
    path = tmp_path / "corewatch.desktop"
    assert not autostart.is_enabled(path)  # no file
    for switch in (
        "Hidden=true",
        "Hidden = true",
        "X-GNOME-Autostart-enabled=false",
        "X-GNOME-Autostart-enabled = False",
    ):
        path.write_text(f"[Desktop Entry]\nExec=corewatch\n{switch}\n")
        assert not autostart.is_enabled(path), switch


def test_disable_removes_only_our_own_entry(tmp_path: Path) -> None:
    ours = tmp_path / "ours.desktop"
    autostart.enable(ours, ["corewatch"])
    assert autostart.disable(ours) and not ours.exists()
    assert autostart.disable(ours)  # already gone: fine
    theirs = tmp_path / "theirs.desktop"
    theirs.write_text("[Desktop Entry]\nExec=corewatch --some-flag\nComment=X-Corewatch-Autostart=true\n")
    assert not autostart.disable(theirs)  # the marker inside another value doesn't count
    assert theirs.exists()


def test_enable_switches_a_foreign_entry_back_on_in_place(tmp_path: Path) -> None:
    theirs = tmp_path / "corewatch.desktop"
    theirs.write_text("[Desktop Entry]\nExec=corewatch --their-flag\nHidden=true\nX-GNOME-Autostart-enabled=false\n")
    message = autostart.enable(theirs, ["/usr/bin/corewatch"])
    text = theirs.read_text()
    assert "Exec=corewatch --their-flag" in text and "Hidden" not in text and "enabled=false" not in text
    assert autostart.is_enabled(theirs) and "switched back on" in message
    assert not autostart.disable(theirs) and theirs.exists()  # still theirs: turning off never deletes it


def test_an_unreadable_entry_is_left_alone(tmp_path: Path) -> None:
    latin1 = tmp_path / "corewatch.desktop"
    latin1.write_bytes(b"[Desktop Entry]\nName=caf\xe9\nExec=corewatch\n")
    assert not autostart.is_enabled(latin1)
    assert not autostart.disable(latin1) and latin1.exists()
    with pytest.raises(autostart.AutostartError):
        autostart.enable(latin1, ["corewatch"])
    assert latin1.read_bytes().endswith(b"Exec=corewatch\n")


def test_a_failed_write_leaves_nothing_behind(tmp_path: Path) -> None:
    target = tmp_path / "corewatch.desktop"
    target.mkdir()  # can't replace a folder with a file
    with pytest.raises(OSError):
        autostart._write(target, "text")
    assert [p.name for p in tmp_path.iterdir()] == ["corewatch.desktop"]


def test_refresh_only_repoints_a_program_that_is_gone(tmp_path: Path) -> None:
    path = tmp_path / "corewatch.desktop"
    edited = autostart.entry(["/old/place/corewatch"]).replace("--login", "--login -i 5") + "Name[en_US]=corewatch\n"
    path.write_text(edited)
    assert autostart.refresh(path, ["/home/u/.local/bin/corewatch"])
    text = path.read_text()
    assert "Exec=/home/u/.local/bin/corewatch --login -i 5" in text.splitlines()  # your flags stay
    assert "Name[en_US]=corewatch" in text  # and so does whatever an editor added
    assert not autostart.refresh(path, ["/home/u/.local/bin/corewatch"])  # already current


def test_refresh_leaves_a_working_program_and_other_entries_alone(tmp_path: Path) -> None:
    real = make_command(tmp_path / "bin")
    path = tmp_path / "corewatch.desktop"
    path.write_text(autostart.entry([str(real)]))
    assert not autostart.refresh(path, ["/somewhere/else/corewatch"])  # still exists: keep it
    path.write_text(autostart.entry(["/gone/corewatch"]) + "Hidden=true\n")
    assert not autostart.refresh(path, ["/elsewhere/corewatch"])  # switched off: leave it
    theirs = tmp_path / "theirs.desktop"
    theirs.write_text("[Desktop Entry]\nExec=/gone/corewatch\n")
    assert not autostart.refresh(theirs, ["/elsewhere/corewatch"])
    assert not autostart.refresh(tmp_path / "missing.desktop")


def test_refresh_round_trips_awkward_programs(tmp_path: Path) -> None:
    path = tmp_path / "corewatch.desktop"
    path.write_text(autostart.entry(['/gone/my "odd" $dir\\with%stuff/corewatch']))
    assert autostart.refresh(path, ["/new/corewatch"])
    assert "Exec=/new/corewatch --login" in path.read_text().splitlines()


def test_switching_a_foreign_entry_on_keeps_its_mode_and_symlink(tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles" / "corewatch.desktop"
    dotfiles.parent.mkdir()
    dotfiles.write_text("[Desktop Entry]\nExec=corewatch --mine\nHidden=true\n")
    dotfiles.chmod(0o600)
    link = tmp_path / "autostart" / "corewatch.desktop"
    link.parent.mkdir()
    link.symlink_to(dotfiles)
    autostart.enable(link, ["corewatch"])
    assert link.is_symlink() and "Hidden" not in dotfiles.read_text()
    assert oct(dotfiles.stat().st_mode & 0o777) == "0o600"


def test_no_autostart_folder_yet_reads_as_off_and_is_reported_as_a_write_problem(tmp_path: Path) -> None:
    (tmp_path / "autostart").write_text("a file where the folder should be")
    path = tmp_path / "autostart" / "corewatch.desktop"
    assert not autostart.is_enabled(path)
    with pytest.raises(OSError):
        autostart.enable(path, ["corewatch"])


def make_command(directory: Path) -> Path:
    directory.mkdir(parents=True)
    command = directory / "corewatch"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    return command


def test_launch_command_prefers_an_install_outside_the_running_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    venv = tmp_path / "project" / ".venv"
    make_command(venv / "bin")
    installed = make_command(tmp_path / "home" / ".local" / "bin")
    monkeypatch.setattr(autostart.sys, "prefix", str(venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv / "bin"), str(installed.parent)]))
    assert autostart.launch_command() == [str(installed)]
    monkeypatch.setenv("PATH", str(venv / "bin"))  # only the project's own: better than nothing
    assert autostart.launch_command() == [str(venv / "bin" / "corewatch")]


def test_launch_command_falls_back_to_the_running_script_then_python(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    script = make_command(tmp_path / "somewhere")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(autostart.sys, "argv", [str(script)])
    assert autostart.launch_command() == [str(script)]
    monkeypatch.setattr(autostart.sys, "argv", ["/nowhere/python-thing"])
    assert autostart.launch_command()[1:] == ["-m", "corewatch"]


def test_launch_command_from_the_installed_tool_points_at_that_install(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tool_env = tmp_path / "uv" / "tools" / "corewatch"
    (tool_env / "bin").mkdir(parents=True)
    (tool_env / "uv-receipt.toml").write_text("")
    real = tool_env / "bin" / "corewatch"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    (link_dir / "corewatch").symlink_to(real)
    dev = make_command(tmp_path / "project" / ".venv" / "bin")
    monkeypatch.setattr(autostart.sys, "prefix", str(tool_env))
    monkeypatch.setenv("PATH", os.pathsep.join([str(dev.parent), str(link_dir)]))  # a dev venv first on PATH
    assert autostart.launch_command() == [str(link_dir / "corewatch")]


def test_relative_home_falls_back_to_the_accounts_real_home() -> None:
    import pwd

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert autostart.autostart_path({"HOME": "relhome"}) == real_home / ".config" / "autostart" / "corewatch.desktop"
