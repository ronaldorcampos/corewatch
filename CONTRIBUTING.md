# Contributing to corewatch

Pull requests and issues are welcome. Hardware reports are the most useful issue of all,
especially from AMD CPUs and GPUs, which corewatch supports from the kernel drivers' documentation
but hasn't yet been tried on.

## Reporting a bug or your hardware

Open an [issue](https://github.com/ronaldorcampos/corewatch/issues) with:

- what you did, what you expected, and what happened;
- your distribution, kernel (`uname -r`), and how you installed corewatch (`.deb`, `.rpm`, uv);
- the output of `corewatch dump --json --all`, which lists every sensor corewatch found, with
  the device and driver it comes from, and its readings and limits. For a sensor that's missing or misnamed, this is usually all that's
  needed to fix it.

Security problems don't go in issues: see [SECURITY.md](SECURITY.md).

## Setting up

You need [uv](https://docs.astral.sh/uv/) and [prek](https://prek.j178.dev/) (or `pre-commit`).

```bash
git clone https://github.com/ronaldorcampos/corewatch.git
cd corewatch
uv sync
prek install
uv run corewatch         # run from the checkout
```

`prek install` runs ruff, mypy and the tests before every commit, the same checks CI runs.

## Checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Sensor sources take their sysfs and procfs roots as constructor arguments, so the tests build fake
trees in a temporary directory (see `tests/fakes.py`) and never touch real hardware. A new sensor
or driver gets a test built the same way, from what the real driver publishes. The GUI tests run
on Qt's offscreen platform (`QT_QPA_PLATFORM=offscreen`).

## Pull requests

- Keep each one to one change, and add or update tests for it.
- Write commit messages as a sentence saying what the change does, in the imperative
  ("Add Intel integrated graphics and Arc support"), with the why in the body when it isn't
  obvious.
- Update `README.md` when what users see or do changes.
- CI (lint, formatting, mypy and the tests) must pass, and the maintainer reviews every pull
  request before it's merged. Merges are squashed or rebased, so `main`'s history stays linear.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
