from pathlib import Path

import pytest


def write_tree(root: Path, files: dict[str, str]) -> None:
    """Create a fake sysfs/procfs tree: {"hwmon0/temp1_input": "45000", ...}."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n")


@pytest.fixture
def proc_root(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    write_tree(root, {"cpuinfo": "processor\t: 0\nmodel name\t: 13th Gen Intel(R) Core(TM) i7-13700K\n"})
    return root


@pytest.fixture(autouse=True)
def private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own config folder, so nothing ever touches the real ~/.config."""
    config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return config
