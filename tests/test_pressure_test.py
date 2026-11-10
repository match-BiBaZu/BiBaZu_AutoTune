import copy
import csv
import json
import threading
from collections import Counter

import pytest
from test_engine import parameters

from bibazu_autotune.pressure_test import (
    FILETIME_MS,
    SIGNALS,
    PressureRecording,
    analyze_pressure_trace,
    pressure_matrix,
)
from bibazu_autotune.pressure_test_runner import PressureTestRunner


def events_for(pulse, *, offset=1_000_000_000, through=None):
    end = pulse.pulse_ms + 201 if through is None else through
    events = []
    for ms in range(-100, end):
        opened = 0 <= ms < pulse.pulse_ms
        pressure = pulse.pressure_mbar / 1000 * 0.6 if opened else 0.025
        values = (0, round(pressure * 32767 / 10), 0.0, pressure, opened)
        for signal, value in zip(SIGNALS, values, strict=True):
            events.append((signal, offset + ms * FILETIME_MS, value))
    return events


def test_fixed_matrix_order_and_repetitions():
    matrix = pressure_matrix()
    assert len(matrix) == 270
    assert [p.number for p in matrix] == list(range(1, 271))
    assert Counter((p.pulse_ms, p.pressure_mbar) for p in matrix) == {
        (duration, pressure): 5
        for duration in range(20, 101, 10)
        for pressure in range(1000, 6001, 1000)
    }
    assert [(p.pulse_ms, p.pressure_mbar, p.repetition) for p in matrix[:6]] == [
        (20, 1000, i) for i in range(1, 6)
    ] + [(20, 2000, 1)]
    assert list(matrix) == sorted(matrix, key=lambda p: (p.pulse_ms, p.pressure_mbar, p.repetition))


@pytest.mark.parametrize("index", [*range(0, 270, 30), 269])
def test_trace_reference_pressure_mean_and_native_duration(index):
    pulse = pressure_matrix()[index]
    result, rows = analyze_pressure_trace(pulse, events_for(pulse))
    assert result["complete"], result["error"]
    assert result["observed_pulse_ms"] == pulse.pulse_ms
    assert result["baseline_bar"] == pytest.approx(0.025)
    assert result["peak_bar"] == pytest.approx(pulse.pressure_mbar / 1000 * 0.6)
    assert result["mean_open_bar"] == result["peak_bar"]
    assert sum(row["valve"] for row in rows) == pulse.pulse_ms


@pytest.mark.parametrize("mode", ["gap", "channel", "post", "edge", "abort", "duration", "nan"])
def test_incomplete_trace_never_produces_regular_statistics(tmp_path, mode):
    pulse = pressure_matrix()[0]
    events = events_for(pulse)
    if mode == "gap":
        events = [e for e in events if not (e[0] == "bar2" and e[1] == 1_000_010_000)]
    elif mode == "channel":
        events = [e for e in events if e[0] != "raw2"]
    elif mode == "post":
        events = events_for(pulse, through=50)
    elif mode == "edge":
        events = [(s, t, False if s == "valve" else v) for s, t, v in events]
    elif mode == "duration":
        events = [(s, t, False if s == "valve" and t == 1_000_000_000 else v) for s, t, v in events]
    elif mode == "nan":
        events.append(("bar2", 1_000_000_000, float("nan")))
    recording = PressureRecording(tmp_path, "run", parameters())
    result, _ = recording.add(pulse, events, error="Abbruch" if mode == "abort" else None)
    assert not result["complete"]
    assert result["peak_bar"] is None and result["mean_open_bar"] is None
    assert "valid_count,peak_bar_mean" in (recording.path / "combinations.csv").read_text()
    recording.finish("aborted")
    assert json.loads((recording.path / "run.json").read_text())["status"] == "aborted"


class MemoryPressureBackend:
    def __init__(self):
        self.parameters = parameters()
        self.applied = []

    def snapshot(self):
        return {"parameters": copy.deepcopy(self.parameters)}

    def apply(self, values, guard):
        guard()
        self.applied.append(copy.deepcopy(values))
        self.parameters = copy.deepcopy(values)


class ClockedPulsePlc:
    """Contract simulation: native pulse timing proceeds independently of runner ticks."""

    def __init__(self, clock):
        self.clock = clock
        self.sequence = 0
        self.active = False
        self.current = None
        self.fired = []
        self.pulses = []
        self.entered = []
        self.stopped = False
        self.subscribed = False
        self.fail_streams = False
        self.elapsed = 0

    def check(self):
        return {"feature_version": 2}

    def subscribe(self):
        self.subscribed = True

    def unsubscribe(self):
        self.subscribed = False

    def enter(self, pulse, guard):
        guard()
        self.active = True
        self.current = pulse
        self.entered.append(self.clock())
        self.elapsed = 0
        self.sequence += 1
        return self.sequence

    def beat(self, guard):
        guard()

    def fire(self, guard):
        guard()
        self.fired.append((self.clock(), self.current))
        self.pulses.append((round(self.clock() * 1000), self.current.pulse_ms))
        self.sequence += 1
        return self.sequence

    def stop(self):
        self.stopped = True

    def leave(self):
        self.active = False
        self.sequence += 1
        return self.sequence

    def info(self):
        opened = bool(
            self.fired
            and self.active
            and self.clock() < self.fired[-1][0] + self.current.pulse_ms / 1000
        )
        if self.fired and not opened:
            self.elapsed = self.fired[-1][1].pulse_ms
        return {
            "fault": 0,
            "active": self.active,
            "ack": self.sequence,
            "status": 3 if opened else 1,
            "open": opened,
            "elapsed_ms": self.elapsed,
            "pressure_mbar": self.current.pressure_mbar if self.current else 0,
            "pulse_ms": self.current.pulse_ms if self.current else 0,
            "array": 2,
            "nozzle": 1,
            "timed": True,
        }

    def latest_tick(self):
        return 1_000_000_000 + round(self.clock() * 1000) * FILETIME_MS

    def snapshot(self, after=0):
        now = round(self.clock() * 1000)
        events = []
        start = max(0, now - 700)
        if after:
            start = max(start, (after - 1_000_000_000) // FILETIME_MS)
        for ms in range(start, now + 1):
            opened = any(begin <= ms < begin + duration for begin, duration in self.pulses)
            pressure = (
                (self.current.pressure_mbar / 1000 * 0.6 if opened else 0.025)
                if self.current
                else 0.025
            )
            values = (0, round(pressure * 32767 / 10), 0.0, pressure, opened)
            for signal, value in zip(SIGNALS, values, strict=True):
                if not (self.fail_streams and signal == "bar2"):
                    events.append((signal, 1_000_000_000 + ms * FILETIME_MS, value))
        return events


def runner_for(tmp_path):
    now = [0.0]

    def clock():
        return now[0]

    adapter = ClockedPulsePlc(clock)
    backend = MemoryPressureBackend()
    cancel = threading.Event()
    runner = PressureTestRunner(
        backend, tmp_path, cancel, lambda _: None, clock=clock, adapter=adapter
    )
    return runner, adapter, backend, now, cancel


def advance(runner, now, predicate, limit=60_000):
    for _ in range(limit):
        if predicate():
            return
        now[0] = round(now[0] + 0.02, 6)
        runner.tick()
    raise AssertionError(runner.snapshot())


def test_full_270_pulse_run_and_safe_end_state(tmp_path):
    runner, plc, backend, now, _ = runner_for(tmp_path)
    baseline = copy.deepcopy(backend.parameters)
    runner.start("token")
    assert plc.fired == []  # Start only subscribes; no immediate valve command.
    advance(runner, now, lambda: not runner.active)
    assert runner.snapshot()["status"] == "completed", runner.snapshot()["error"]
    assert len(plc.fired) == 270
    assert [pulse for _, pulse in plc.fired] == list(pressure_matrix())
    for (first_time, pulse), (next_time, _) in zip(plc.fired, plc.fired[1:], strict=False):
        assert round((next_time - first_time) * 1000) - pulse.pulse_ms >= 3000
    for index, (fired, entered) in enumerate(zip(plc.fired, plc.entered, strict=True)):
        first_in_combination = index % 5 == 0
        assert round((fired[0] - entered) * 1000) >= (5000 if first_in_combination else 1000)
    assert not plc.active and not plc.subscribed
    assert all(not a["enabled"] for a in backend.parameters["arrays"])
    for original, restored in zip(baseline["arrays"], backend.parameters["arrays"], strict=True):
        assert {k: v for k, v in original.items() if k != "enabled"} == {
            k: v for k, v in restored.items() if k != "enabled"
        }
    assert runner.snapshot()["completed"] == 270
    assert runner.snapshot()["total"] == 270
    with (runner.recording.path / "combinations.csv").open(encoding="utf-8", newline="") as stream:
        combinations = list(csv.DictReader(stream))
    assert len(combinations) == 54
    assert {int(row["pulse_ms"]) for row in combinations} == set(range(20, 101, 10))
    assert {int(row["valid_count"]) for row in combinations} == {5}
    assert runner.recording.manifest["timed_pulse_feature_version"] == 2
    assert runner.recording.manifest["pause_ms"] == 3000
    assert runner.recording.manifest["settle_ms"] == 1000
    assert runner.recording.manifest["combination_settle_ms"] == 5000
    with (runner.recording.path / "samples.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert {int(row["number"]) for row in rows} == set(range(1, 271))
    assert {row["complete"] for row in rows} == {"True"}


def test_abort_during_new_combination_settling_never_fires_next_pulse(tmp_path):
    runner, plc, _, now, cancel = runner_for(tmp_path)
    # Adjacent combinations, including a duration change with the same pressure.
    runner.matrix = tuple(pressure_matrix()[i] for i in (0, 5, 35))
    runner.start("token")
    advance(runner, now, lambda: runner.index == 2 and runner.phase == "settling")
    assert len(plc.fired) == 2
    now[0] = round(now[0] + 4, 6)
    runner.tick()
    assert len(plc.fired) == 2
    cancel.set()
    advance(runner, now, lambda: not runner.active)
    assert plc.stopped and len(plc.fired) == 2
    assert runner.snapshot()["status"] == "aborted"


@pytest.mark.parametrize(
    "phase", ["preflight", "entering", "settling", "capturing", "post", "leaving"]
)
def test_abort_in_each_stage_stops_and_never_continues(tmp_path, phase):
    runner, plc, _, now, cancel = runner_for(tmp_path)
    runner.start("token")
    advance(runner, now, lambda: runner.phase == phase)
    fired = len(plc.fired)
    cancel.set()
    advance(runner, now, lambda: not runner.active)
    assert plc.stopped and not plc.active
    assert len(plc.fired) == fired
    assert runner.snapshot()["status"] == "aborted"
    assert not plc.subscribed


def test_missing_notifications_prevents_first_pulse(tmp_path):
    runner, plc, _, now, _ = runner_for(tmp_path)
    plc.fail_streams = True
    runner.start("token")
    advance(runner, now, lambda: not runner.active)
    assert not plc.fired
    assert runner.snapshot()["status"] == "failed"


def test_disk_failure_stops_before_next_pulse(tmp_path, monkeypatch):
    runner, plc, _, now, _ = runner_for(tmp_path)
    runner.start("token")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runner.recording, "add", fail)
    advance(runner, now, lambda: not runner.active)
    assert len(plc.fired) == 1
    assert plc.stopped and runner.snapshot()["status"] == "failed"
