"""Failure paths must preserve data and inhibit automatic continuation."""

import pytest
from test_experiments import experiment as experiment
from test_experiments import plan_assets as plan_assets

from bibazu_autotune.clients import ApiFailure
from bibazu_autotune.experiments.dataset import DatasetReader


def test_disk_failure_during_checkpoint_still_releases_sessions(experiment, monkeypatch):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()

    def disk_full(*args):
        raise OSError("Disk full")

    monkeypatch.setattr(runner.store, "checkpoint", disk_full)
    runner.fail(OSError("Disk full"))
    assert runner.state == "disconnected"
    assert pressure.session_id is None and vision.session_id is None
    assert not any(a["enabled"] for a in pressure.parameters["arrays"])
    assert any(name == "error" and "Fortschritt" in value for name, value in events)


def test_ambiguous_camera_start_request_is_aborted_on_failure(experiment):
    runner, pressure, vision, clock, until, events = experiment
    original = vision.start

    def accepted_then_timeout(*args):
        original(*args)
        raise RuntimeError("Response lost after accepting trial")

    vision.start = accepted_then_timeout
    runner.confirm()
    with pytest.raises(RuntimeError, match="Response lost"):
        until("waiting")
    trial_id = runner.record["trial_id"]
    runner.fail(RuntimeError("Response lost"))
    assert vision.trials[trial_id]["status"] == "aborted"
    assert runner.state == "disconnected"
    assert runner.store.trials()[0]["status"] == "interrupted"


def test_write_readback_mismatch_does_not_arm_camera(experiment):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    pressure.operations[runner.operation]["snapshot"]["parameters"]["arrays"][1][
        "pressure_mbar"
    ] = 999
    with pytest.raises(RuntimeError, match="Rücklesen"):
        runner.tick()
    assert not vision.trials


def test_archive_failure_preserves_attempt_and_requires_reconnect(experiment, monkeypatch):
    runner, pressure, vision, clock, until, events = experiment

    def disk_full(*args):
        raise OSError("Disk full during original copy")

    monkeypatch.setattr(runner.store, "finish", disk_full)
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    with pytest.raises(OSError, match="Disk full"):
        until("gate")
    runner.fail(OSError("Disk full"))
    assert runner.slot == 0
    assert len(vision.trials) == 1
    assert runner.store.trials()[0]["status"] == "interrupted"
    assert runner.state == "disconnected" and not pressure.moving


def test_pause_between_combinations_cannot_skip_a_previous_success(experiment):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    until("gate")
    runner.pause()
    until("paused")
    with pytest.raises(RuntimeError, match="Kein pausierter Einzelversuch"):
        runner.skip()
    assert runner.slot == 2
    assert all(row["resolution"] == "accepted" for row in runner.store.trials())


def test_export_cannot_overwrite_original_measurements(experiment):
    runner, pressure, vision, clock, until, events = experiment
    target = runner.store.directory / "trials" / ("a" * 32) / "measurements.csv"
    with pytest.raises(ValueError, match="Originaldaten"):
        DatasetReader([runner.store]).export_csv(target)
    assert not target.exists()


def test_completion_racing_pause_is_archived_instead_of_lost(experiment):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    until("waiting")
    pressure.moving = True
    clock[0] += 2
    trial_id = runner.record["trial_id"]
    assert vision.trial(trial_id)["status"] == "completed"

    def already_finished(*args):
        raise ApiFailure("409: Versuch ist nicht aktiv")

    vision.abort = already_finished
    runner.pause()
    until("paused")
    assert runner.record["status"] == "completed"
    assert runner.record["clip"] == "clip"
    assert runner.record["evaluation"]["label"] == "flipped"
