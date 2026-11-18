"""Failure paths must preserve data and inhibit automatic continuation."""

import pytest
from test_experiments import experiment as experiment
from test_experiments import plan_assets as plan_assets

from bibazu_autotune.clients import ApiFailure
from bibazu_autotune.experiments.dataset import DatasetReader
from bibazu_autotune.experiments.storage import CampaignStore, read_json


def test_disk_failure_during_checkpoint_still_releases_sessions(experiment, monkeypatch):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()

    def disk_full(*args, **kwargs):
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

    def accepted_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
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


def test_cancel_keeps_data_and_allows_new_campaign(experiment):
    runner, pressure, vision, clock, until, events = experiment
    directory = runner.store.directory
    runner.confirm()
    until("waiting")
    runner.pause()
    until("paused")
    runner.end_campaign(False, directory)
    assert runner.state == "disconnected" and runner.store is None
    assert read_json(directory / "progress.json")["state"] == "cancelled"
    assert CampaignStore(directory).trials()[0]["status"] == "aborted"
    assert not pressure.moving
    runner.connect()
    until("ready")


def test_delete_only_selected_campaign_after_stop(experiment, tmp_path, plan_assets):
    runner, pressure, vision, clock, until, events = experiment
    plan, assets = plan_assets
    neighbour = CampaignStore.create(tmp_path / "other", plan, assets=assets)
    directory = runner.store.directory
    with pytest.raises(RuntimeError, match="nicht mehr aktuell"):
        runner.end_campaign(True, neighbour.directory)
    runner.end_campaign(True, directory)
    assert not directory.exists()
    neighbour.verify_assets()
    assert runner.store is None


def test_delete_rejects_changed_campaign_identity(experiment):
    from bibazu_autotune.engine import atomic_json

    runner, pressure, vision, clock, until, events = experiment
    store = runner.store
    manifest = dict(store.manifest, campaign_id="f" * 32)
    atomic_json(store.directory / "campaign.json", manifest)
    with pytest.raises(ValueError, match="Kennung|kennung"):
        store.delete()
    assert (store.directory / "campaign.json").exists()


def test_delete_failure_reports_failure_without_restarting_campaign(experiment, monkeypatch):
    runner, pressure, vision, clock, until, events = experiment
    directory = runner.store.directory

    def locked():
        raise OSError("Datei gesperrt")

    monkeypatch.setattr(runner.store, "delete", locked)
    with pytest.raises(OSError, match="gesperrt"):
        runner.end_campaign(True, directory)
    assert runner.state == "disconnected" and runner.store is None
    ended = [value for name, value in events if name == "campaign_ended"][-1]
    assert ended["delete_requested"] and not ended["deleted"]
    assert (directory / "campaign.json").exists()


def test_pending_external_band_stop_defers_parameter_write(experiment):
    runner, pressure, vision, clock, until, events = experiment
    pending = [True]
    runner.external_stop = lambda: pending[0]
    runner.pause()
    assert runner.state == "stopping"
    count = len(pressure.operations)
    for _ in range(5):
        runner.tick()
    assert len(pressure.operations) == count
    pending[0] = False
    until("paused")
    assert not any(a["enabled"] for a in pressure.parameters["arrays"])
    assert runner.pressure.session_id is not None


def test_missing_presence_capability_stops_before_starting_trial(experiment):
    runner, pressure, vision, clock, until, events = experiment
    vision.capabilities = lambda: {}
    count = len(pressure.operations)
    with pytest.raises(RuntimeError, match="Kamera-GUI neu starten"):
        runner.confirm()
    assert len(pressure.operations) == count
    assert not vision.trials
