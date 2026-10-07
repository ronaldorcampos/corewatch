from fakes import BrokenSource, FakeSource, load, temp

from corewatch.monitor import Monitor, group_rows, headline
from corewatch.sources import default_sources


def test_stats_accumulate_and_reset() -> None:
    source = FakeSource("s", [[temp("t", "CPU", 50.0)], [temp("t", "CPU", 70.0)], [temp("t", "CPU", None)]])
    monitor = Monitor([source])
    monitor.sample()
    monitor.sample()
    [row] = monitor.sample()
    assert (row.stats.minimum, row.stats.maximum, row.stats.average) == (50.0, 70.0, 60.0)
    monitor.reset()
    [row] = monitor.sample()
    assert row.stats.count == 0  # last scripted value is None, and history was cleared


def test_broken_source_is_reported_and_others_keep_working() -> None:
    good = FakeSource("good", [[temp("t", "CPU", 50.0)]], notes=["hint"])
    monitor = Monitor([BrokenSource(), good])
    rows = monitor.sample()
    assert [r.reading.key for r in rows] == ["t"]
    assert monitor.notes() == ["hint", "Could not read broken sensors: device went away"]


def test_error_note_clears_once_source_recovers() -> None:
    class Flaky(FakeSource):
        def sample(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise OSError("boom")
            return []

    monitor = Monitor([Flaky("flaky", [[]])])
    monitor.sample()
    assert monitor.notes() == ["Could not read flaky sensors: boom"]
    monitor.sample()
    assert monitor.notes() == []


def test_close_closes_every_source() -> None:
    sources = [FakeSource("a", [[]]), FakeSource("b", [[]])]
    Monitor(sources).close()
    assert all(s.closed for s in sources)


def test_group_rows_orders_devices_and_kinds() -> None:
    source = FakeSource(
        "s",
        [
            [
                temp("disk", "NVMe nvme0 · Samsung", 40.0),
                load("cpu-load", "CPU · i7", 5.0),
                temp("weird", "spd5118-ish", 30.0),
                temp("gpu", "GPU · RTX", 41.0),
                temp("cpu-temp", "CPU · i7", 57.0),
            ]
        ],
    )
    groups = group_rows(Monitor([source]).sample())
    assert [(device, [r.reading.key for r in rows]) for device, rows in groups] == [
        ("CPU · i7", ["cpu-temp", "cpu-load"]),
        ("GPU · RTX", ["gpu"]),
        ("NVMe nvme0 · Samsung", ["disk"]),
        ("spd5118-ish", ["weird"]),
    ]


def test_headline_prefers_cpu_package_then_hottest_cpu_then_hottest() -> None:
    def rows(*readings):  # type: ignore[no-untyped-def]
        return Monitor([FakeSource("s", [list(readings)])]).sample()

    package = temp("p", "CPU · i7", 50.0, label="CPU package")
    hot_core = temp("c", "CPU · i7", 60.0, label="Core 4")
    gpu = temp("g", "GPU · RTX", 80.0)
    assert headline(rows(hot_core, package, gpu)).reading.key == "p"  # type: ignore[union-attr]
    assert headline(rows(hot_core, gpu)).reading.key == "c"  # type: ignore[union-attr]
    assert headline(rows(gpu, temp("d", "NVMe", 40.0))).reading.key == "g"  # type: ignore[union-attr]
    assert headline(rows(temp("x", "CPU", None))) is None


def test_default_sources_skips_factories_that_fail() -> None:
    def explode() -> FakeSource:
        raise RuntimeError("no driver")

    ok = FakeSource("ok", [[]])
    assert default_sources([explode, lambda: ok]) == [ok]


def test_unused_inputs_are_hidden_and_stay_hidden_after_one_glitch() -> None:
    from corewatch.model import Kind, Reading

    def fan(rpm: float) -> Reading:
        return Reading("fan2", "Motherboard", "Fan 2", Kind.FAN, rpm, empty_if_idle=True)

    def dead_fan() -> Reading:
        return Reading("fan1", "Motherboard", "Fan 1", Kind.FAN, 0.0, empty_if_idle=True)

    def control() -> Reading:
        return Reading("pwm1", "Motherboard", "Fan control 1", Kind.FAN_DUTY, 69.0, companion="fan1")

    def probe(value: float, glitch: bool) -> Reading:
        return Reading("aux", "Motherboard", "AUXTIN0", Kind.TEMPERATURE, value, unused=glitch)

    source = FakeSource(
        "s",
        [
            [fan(1200.0), dead_fan(), control(), probe(127.0, True)],
            [fan(0.0), dead_fan(), control(), probe(17.0, False)],  # fan 2 stopped; probe looks normal
        ],
    )
    monitor = Monitor([source])
    monitor.sample()
    rows = monitor.sample()
    visible = [r.reading.key for r in monitor.visible_rows(rows, False)]
    assert visible == ["fan2"]  # a fan that stopped after spinning must stay visible
    assert len(monitor.visible_rows(rows, True)) == 4
    monitor.reset()
    rows = monitor.sample()
    assert [r.reading.key for r in monitor.visible_rows(rows, False)] == ["fan2"]  # Reset doesn't forget it spun


def test_collect_touches_only_sources_and_ingest_records() -> None:
    source = FakeSource("s", [[temp("t", "CPU", 50.0)]], notes=["hint"])
    monitor = Monitor([source, BrokenSource()], clock=lambda: 42.0)
    collected = monitor.collect()
    assert collected.at == 42.0 and collected.notes == ["hint"]
    assert monitor.notes() == [] and monitor.last_sample_at is None  # nothing recorded yet
    [row] = monitor.ingest(collected)
    assert row.stats.count == 1 and monitor.last_sample_at == 42.0
    assert monitor.notes() == ["hint", "Could not read broken sensors: device went away"]


def test_to_wall_converts_monotonic_timestamps() -> None:
    monitor = Monitor([], clock=lambda: 100.0, wall_clock=lambda: 1_700_000_000.0)
    assert monitor.to_wall(90.0) == 1_699_999_990.0


def test_fans_in_warning_are_never_hidden() -> None:
    from corewatch.model import Kind, Reading

    stopped_below_minimum = Reading("fan1", "Motherboard", "CPU fan", Kind.FAN, 0.0, low=300.0)
    monitor = Monitor([FakeSource("s", [[stopped_below_minimum]])])
    rows = monitor.sample()
    assert [r.reading.key for r in monitor.visible_rows(rows, False)] == ["fan1"]


def test_a_raising_notes_call_does_not_stop_the_readings() -> None:
    class Noisy(FakeSource):
        def notes(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("bad hint")

    monitor = Monitor(
        [Noisy("n", [[temp("t", "CPU", 50.0)]]), FakeSource("ok", [[temp("u", "CPU", 40.0)]], notes=["hi"])]
    )
    rows = monitor.sample()
    assert [r.reading.key for r in rows] == ["t", "u"]
    assert monitor.notes() == ["hi"]


def test_fans_card_keeps_origin_and_tells_same_named_fans_apart() -> None:
    from corewatch.model import Kind, Reading, Row
    from corewatch.monitor import gather_fans

    rows = [
        Row(Reading("a", "Motherboard · IT8689", "Fan 1", Kind.FAN, 900.0)),
        Row(Reading("b", "Motherboard · IT8689 (2)", "Fan 1", Kind.FAN, 950.0)),
        Row(Reading("c", "GPU · AMD", "Fan 1", Kind.FAN, 1100.0)),
        Row(Reading("d", "GPU · RTX 4070", "Fan 1", Kind.FAN, 2300.0)),
        Row(Reading("e", "Motherboard · IT8689", "Fan 2", Kind.FAN, 800.0)),
    ]
    gathered = {r.reading.key: r.reading for r in gather_fans(rows)}
    assert {k: r.label for k, r in gathered.items()} == {
        "a": "Fan 1 (IT8689)",
        "b": "Fan 1 (IT8689 (2))",
        "c": "GPU fan 1 (AMD)",
        "d": "GPU fan 1 (RTX 4070)",
        "e": "Fan 2",  # unique: left alone
    }
    assert {r.device for r in gathered.values()} == {"Fans"}
    assert gathered["d"].origin == "GPU · RTX 4070"
