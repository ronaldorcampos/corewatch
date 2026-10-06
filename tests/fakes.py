from corewatch.model import Kind, Reading


class FakeSource:
    """A source that replays scripted readings, one list per sample() call."""

    def __init__(self, name: str, script: list[list[Reading]], notes: list[str] | None = None) -> None:
        self.name = name
        self.script = script
        self._notes = notes or []
        self.calls = 0
        self.closed = False

    def sample(self) -> list[Reading]:
        readings = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return readings

    def notes(self) -> list[str]:
        return self._notes

    def close(self) -> None:
        self.closed = True


class BrokenSource(FakeSource):
    def __init__(self) -> None:
        super().__init__("broken", [[]])

    def sample(self) -> list[Reading]:
        raise OSError("device went away")


def temp(key: str, device: str, value: float | None, label: str | None = None, **limits: float | None) -> Reading:
    return Reading(key, device, label or key, Kind.TEMPERATURE, value, **limits)


def load(key: str, device: str, value: float | None, label: str | None = None) -> Reading:
    return Reading(key, device, label or key, Kind.LOAD, value)
