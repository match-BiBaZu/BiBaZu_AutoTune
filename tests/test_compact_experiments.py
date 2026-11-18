"""Direct video capture, media-only deletion and low-space behavior."""

from dataclasses import replace
from pathlib import Path

import pytest
from test_experiments import experiment as experiment
from test_experiments import plan_assets as plan_assets

from bibazu_autotune.experiments.dataset import DatasetReader
from bibazu_autotune.experiments.storage import read_json
from bibazu_autotune.experiments.video import VideoFrames


def compact(experiment, plan_assets, tmp_path):
    runner, pressure, vision, clock, until, events = experiment
    runner.end_campaign(False, runner.store.directory)
    runner.connect()
    until("ready")
    plan, assets = plan_assets
    runner.create(
        tmp_path / "compact", replace(plan, storage_profile="review_video_v1"), assets, {}
    )
    until("gate")
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    until("gate")
    return runner.store


def test_compact_capture_is_not_copied_and_deletion_preserves_training_data(
    experiment, plan_assets, tmp_path, monkeypatch
):
    from bibazu_autotune.experiments import storage

    def no_copy(*args):
        raise AssertionError("Compact capture must not copy images")

    monkeypatch.setattr(storage.shutil, "copyfile", no_copy)
    store = compact(experiment, plan_assets, tmp_path)
    records = store.trials()
    assert len(records) == 2
    assert not list(store.directory.rglob("*.npy"))
    assert len(list(store.directory.rglob("*.mp4"))) == 2
    record = records[0]
    directory = store.trial_directory(record["trial_id"])
    video = directory / "clip/review.mp4"
    assert Path(record["original_clip_path"]) == directory / "clip"
    manifest = read_json(directory / "clip/manifest.json")
    images = VideoFrames(video.parent, manifest)
    assert images[20].shape == (192, 256, 3)
    assert images[0].shape == (192, 256, 3)
    with pytest.raises(ValueError, match="gespeicherter Prüfung"):
        store.delete_review_video(record["trial_id"])
    store.annotate(record["trial_id"], "flipped", False, "Visuell geprüft")
    before = {
        name: (directory / name).read_bytes()
        for name in ("trial.json", "measurements.csv", "annotations.json")
    }
    row_before = list(DatasetReader([store]).classifier_rows())
    assert store.delete_review_video(record["trial_id"]) > 0
    assert not video.exists()
    assert all((directory / name).read_bytes() == data for name, data in before.items())
    assert list(DatasetReader([store]).classifier_rows()) == row_before
    assert read_json(directory / "clip/manifest.json")["media"]["state"] == "deleted"
    assert len(VideoFrames(video.parent, read_json(video.parent / "manifest.json"))) == 0
    assert (store.trial_directory(records[1]["trial_id"]) / "clip/review.mp4").exists()


def test_low_space_pauses_before_recording_or_applying_new_parameters(experiment, monkeypatch):
    from bibazu_autotune.experiments import storage

    runner, pressure, vision, clock, until, events = experiment
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: type("D", (), {"free": 10})())
    runner.confirm()
    until("paused")
    assert not vision.trials
    assert runner.store.trials()[0]["status"] == "interrupted"
    assert not any(a["enabled"] for a in pressure.parameters["arrays"])
    assert pressure.session_id is not None


def test_failed_video_deletion_keeps_file_and_records_failure(
    experiment, plan_assets, tmp_path, monkeypatch
):
    store = compact(experiment, plan_assets, tmp_path)
    trial_id = store.trials()[0]["trial_id"]
    store.annotate(trial_id, "flipped", False, "Geprüft")
    directory = store.trial_directory(trial_id) / "clip"
    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path == directory / "review.mp4":
            raise PermissionError("Video is open elsewhere")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(PermissionError):
        store.delete_review_video(trial_id)
    assert (directory / "review.mp4").exists()
    assert read_json(directory / "manifest.json")["media"]["state"] == "available"


def test_video_deletion_never_selects_legacy_frames(experiment):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    until("gate")
    record = runner.store.trials()[0]
    runner.store.annotate(record["trial_id"], "flipped", False, "Legacy geprüft")
    assert not runner.store.reviewed_videos()
    with pytest.raises(ValueError):
        runner.store.delete_review_video(record["trial_id"])
    assert list(runner.store.trial_directory(record["trial_id"]).rglob("*.npy"))


@pytest.mark.parametrize("published", [False, True])
def test_recovery_accepts_complete_pending_video_without_recopying(
    experiment, plan_assets, tmp_path, published
):
    store = compact(experiment, plan_assets, tmp_path)
    record = store.trials()[0]
    clip = store.trial_directory(record["trial_id"]) / "clip"
    pending = clip.with_name("clip.tmp")
    if not published:
        clip.rename(pending)
    record.pop("clip")
    record.update(status="recording", resolution=None)
    store.save_trial(record)
    store.interrupt_unfinished()
    result = next(r for r in store.trials() if r["trial_id"] == record["trial_id"])
    assert result["status"] == "aborted"
    assert result["clip"] == "clip"
    assert (clip / "review.mp4").is_file()
    assert not pending.exists()


def test_resume_recovers_journal_of_interrupted_new_capture(experiment):
    import json

    from bibazu_autotune.engine import atomic_json

    runner, pressure, vision, clock, until, events = experiment
    store = runner.store
    record = {
        "trial_id": "e" * 32,
        "campaign_id": store.campaign_id,
        "slot": 0,
        "point_index": 0,
        "repetition": 1,
        "created_utc": "2026-01-01",
        "status": "interrupted",
        "storage_profile": "review_video_v1",
        "requested_parameters": store.plan.parameters_at(store.plan.points()[0]),
    }
    store.save_trial(record)
    directory = store.trial_directory(record["trial_id"])
    pending = directory / "clip.tmp"
    pending.mkdir()
    atomic_json(
        directory / "capture-request.json",
        {"format": "review_video_v1", "run_id": store.campaign_id, "trial_id": record["trial_id"]},
    )
    atomic_json(
        pending / "capture.json",
        {
            "trigger_timestamp": 0,
            "metadata": {"run_id": store.campaign_id, "trial_id": record["trial_id"]},
        },
    )
    with (pending / "measurements.jsonl").open("w") as stream:
        for index in range(5):
            stream.write(
                json.dumps(
                    {
                        "video_frame_index": index,
                        "timestamp": index * 0.02,
                        "frame_id": index,
                        "measurement": {"angle_deg": 0.0},
                    }
                )
                + "\n"
            )
        stream.write('{"video_frame_index":')
    store.interrupt_unfinished()
    result = next(r for r in store.trials() if r["trial_id"] == record["trial_id"])
    assert result["status"] == "aborted"
    assert result["clip"] == "clip"
    assert (directory / "measurements.csv").is_file()
    assert read_json(directory / "clip/manifest.json")["frame_count"] == 5
