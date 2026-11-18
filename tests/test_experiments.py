"""Acquisition acceptance tests without Qt, PLC, camera or sibling repositories."""

import hashlib
import shutil
import time
from dataclasses import replace

import pytest

from bibazu_autotune.experiments.classification import evaluate
from bibazu_autotune.experiments.dataset import DatasetReader
from bibazu_autotune.experiments.demo import DemoPressure, DemoVision
from bibazu_autotune.experiments.models import (
    ClassificationConfig,
    ExperimentPlan,
    SweepRange,
)
from bibazu_autotune.experiments.runner import ExperimentRunner
from bibazu_autotune.experiments.storage import CampaignStore, read_json
from bibazu_autotune.profiles import empty_parameters
from bibazu_autotune.trial_context import manual_context


@pytest.fixture
def plan_assets(tmp_path):
    mesh = tmp_path / "part.stl"
    mesh.write_bytes(b"archived STL fixture")
    digest = hashlib.sha256(mesh.read_bytes()).hexdigest()
    context = manual_context(
        "part",
        digest,
        [0, 0, 0, 1],
        [0, 2**-0.5, 0, 2**-0.5],
        rotation_axis="y",
        signed_angle_deg=90,
    )
    parameters = empty_parameters()
    parameters["conveyor_speed_mm_per_sec"] = 50
    parameters["arrays"][1].update(
        enabled=True, nozzles_enabled=[True, False, False] * 2, pressure_mbar=2000
    )
    plan = ExperimentPlan(
        context,
        parameters,
        2,
        SweepRange(2000, 2100, 100),
        SweepRange(100, 110, 10),
        ClassificationConfig(90, observable_confirmed=True),
        repetitions=2,
        settling_seconds=0.5,
        snapshot={"mesh_sha256": digest},
        storage_profile="legacy_npy",
    )
    return plan, {"mesh": mesh}


def samples(final=90, *, direction=1):
    angles = [0.0] * 6 + [final * i / 16 for i in range(17)] + [final] * 6
    return [
        {
            "timestamp": index * 0.02,
            "frame_id": index,
            "angle_deg": (direction * angle + 90) % 180 - 90,
            "present": True,
            "valid": True,
            "touching_border": False,
        }
        for index, angle in enumerate(angles)
    ]


def test_raster_visits_all_pairs_changing_only_one_axis(plan_assets):
    plan, _ = plan_assets
    plan.validate()
    points = plan.points()
    assert [(p.pressure_mbar, p.pulse_duration_ms) for p in points] == [
        (2000, 100),
        (2100, 100),
        (2100, 110),
        (2000, 110),
    ]
    for left, right in zip(points, points[1:], strict=False):
        assert (
            (left.pressure_mbar != right.pressure_mbar)
            + (left.pulse_duration_ms != right.pulse_duration_ms)
        ) == 1
    assert ExperimentPlan.from_dict(plan.to_dict()) == plan


@pytest.mark.parametrize(
    "values",
    [
        (0, 100, 30),
        (-1, 100, 1),
        (0, 6001, 1),
        (0, 1, 0),
        (100, 0, 1),
        (True, 100, 1),
        (0, 100, 1.0),
    ],
)
def test_invalid_raster_rejected(values):
    with pytest.raises(ValueError):
        SweepRange(*values).values(0, 6000)


@pytest.mark.parametrize(
    "final,label",
    [
        (0, "not_flipped"),
        (30, "not_flipped"),
        (80, "flipped"),
        (90, "flipped"),
        (100, "flipped"),
        (101, "overflipped"),
        (180, "overflipped"),
        (270, "overflipped"),
        (360, "overflipped"),
    ],
)
@pytest.mark.parametrize("direction", [-1, 1])
def test_endpoint_classification_and_wraps(final, label, direction):
    config = ClassificationConfig(90, camera_direction=direction, observable_confirmed=True)
    result, rows = evaluate(samples(final, direction=direction), config)
    assert result["label"] == label
    assert result["rotation_deg"] == pytest.approx(final)
    assert result["quality"] == "valid"
    assert rows[0]["angle_deg"] == 0


def test_return_to_source_is_not_flipped_despite_peak():
    rows = samples(130)
    rows = rows[:-6] + [
        {**rows[-1], "timestamp": 0.5 + i * 0.02, "frame_id": 23 + i, "angle_deg": angle}
        for i, angle in enumerate([100, 70, 40, 10] + [0] * 6)
    ]
    result, _ = evaluate(rows, ClassificationConfig(90, observable_confirmed=True))
    assert result["label"] == "not_flipped"
    assert result["max_rotation_deg"] >= 130


@pytest.mark.parametrize(
    "edit",
    [
        {"angle_deg": None},
        {"valid": False},
        {"touching_border": True},
        {"timestamp": 8.0},
        {"frame_id": 999},
        {"angle_deg": float("nan")},
        {"timestamp": None},
    ],
)
def test_interior_gaps_are_not_bridged(edit):
    rows = samples()
    rows[12].update(edit)
    result, _ = evaluate(rows, ClassificationConfig(90, observable_confirmed=True))
    assert result["label"] is None
    assert result["quality"] == "review_required"


def test_empty_frames_outside_part_do_not_invalidate_complete_rotation():
    rows = samples()
    empty = {"timestamp": -0.02, "frame_id": -1, "angle_deg": None, "present": False}
    tail = {**empty, "timestamp": 0.6, "frame_id": 30}
    result, _ = evaluate([empty, *rows, tail], ClassificationConfig(90, observable_confirmed=True))
    assert result["label"] == "flipped"


@pytest.mark.parametrize("options", [{"completed": False}, {"dropped_frames": 1}])
def test_incomplete_captures_require_review(options):
    result, _ = evaluate(samples(), ClassificationConfig(90, observable_confirmed=True), **options)
    assert result["label"] is None


def test_observability_is_required_and_missing_plateaus_are_explicit():
    assert evaluate(samples(), ClassificationConfig(90))[0]["label"] is None
    result, _ = evaluate(samples()[8:20], ClassificationConfig(90, observable_confirmed=True))
    assert "no_stable_initial_pose" in result["reasons"]
    assert "no_stable_final_pose" in result["reasons"]


@pytest.fixture
def experiment(tmp_path, plan_assets):
    current = [100.0]

    def clock():
        return current[0]

    pressure = DemoPressure()
    vision = DemoVision(pressure, clock)
    events = []
    runner = ExperimentRunner(pressure, vision, lambda *args: events.append(args), clock)
    runner.connect()
    runner.tick()
    plan, assets = plan_assets
    runner.create(tmp_path / "output", plan, assets, {"simulated": True})
    runner.tick()
    assert runner.state == "gate"

    def until(state, limit=10000):
        for _ in range(limit):
            current[0] += 0.05
            runner.tick()
            if runner.state == state:
                return
            time.sleep(0.001)
        raise AssertionError(
            f"Wanted {state}, got {runner.state}; archive={runner.archive}; "
            f"operation={runner.operation}; motor={runner.motor_operation}"
        )

    yield runner, pressure, vision, current, until, events
    runner.close()
    vision.close()


def test_confirm_each_combination_automatic_repetitions_and_separate_belt(experiment):
    runner, pressure, vision, clock, until, events = experiment
    assert not pressure.moving and not vision.trials
    for index in range(4):
        assert runner.state == "gate"
        assert not any(a["enabled"] for a in pressure.parameters["arrays"])
        previous = len(vision.trials)
        for _ in range(5):
            runner.tick()
        assert len(vision.trials) == previous
        runner.confirm()
        runner.tick()
        assert runner.state == "settling"
        assert len(vision.trials) == previous
        until("waiting")
        if not pressure.moving:
            # Confirming a series never starts the belt.
            assert index == 0
            runner.conveyor(True)
        until("gate" if index < 3 else "complete")
        assert len(vision.trials) == previous + 2
    records = runner.store.trials()
    assert len(records) == 8
    assert len({r["trial_id"] for r in records}) == 8
    assert all(r["resolution"] == "accepted" for r in records)
    assert all(r["readback_parameters"] == r["requested_parameters"] for r in records)
    assert all(
        (runner.store.trial_directory(r["trial_id"]) / "clip/manifest.json").is_file()
        for r in records
    )
    assert pressure.moving  # Completion does not change the operator's normal belt control.
    assert not any(a["enabled"] for a in pressure.parameters["arrays"])


def test_uncertain_trials_do_not_pause_series_and_batch_retry_is_finite(experiment):
    runner, pressure, vision, clock, until, events = experiment
    original = runner.store.plan
    runner.store.plan = replace(
        original, classification=replace(original.classification, observable_confirmed=False)
    )
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    until("review_pending")
    originals = runner.store.trials()
    assert len(originals) == 2 and runner.slot == 2
    assert all(r["resolution"] == "uncertain" for r in originals)
    assert "paused" not in [value for name, value in events if name == "state"]
    assert not any(a["enabled"] for a in pressure.parameters["arrays"])
    runner.repeat_uncertain()
    until("review_pending")
    records = runner.store.trials()
    assert len(records) == 4
    assert {r["replaces_trial_id"] for r in records[2:]} == {r["trial_id"] for r in originals}
    assert len(runner.store.pending_repeats()) == 2
    runner.defer_uncertain()
    assert runner.slot == 2 and runner.state == "gate"
    assert runner.store.next_slot() == 2


def test_pause_before_camera_arm_never_creates_camera_trial(experiment):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    runner.pause()
    until("paused")
    assert not vision.trials
    assert runner.store.trials()[0]["status"] == "interrupted"


def test_completed_slot_stays_resolved_after_restart_and_campaign_move(experiment, tmp_path):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    until("gate")
    assert runner.slot == 2
    source = runner.store.directory
    runner.release()
    relocated = tmp_path / "relocated"
    shutil.copytree(source, relocated)
    loaded = CampaignStore(relocated)
    loaded.verify_assets()
    assert loaded.next_slot() == 2
    runner.connect()
    runner.tick()
    runner.resume(relocated)
    runner.tick()
    assert runner.state == "gate" and runner.slot == 2
    assert len(vision.trials) == 2  # Resume also requires confirmation.


def test_manual_corrections_are_auditable_and_export_keeps_quality(experiment, tmp_path):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    until("gate")
    store = runner.store
    record = store.trials()[0]
    directory = store.trial_directory(record["trial_id"])
    original = (directory / "trial.json").read_bytes()
    with pytest.raises(ValueError):
        store.annotate(record["trial_id"], "flipped", True, "")
    store.annotate(record["trial_id"], "overflipped", True, "Falsche automatische Klasse")
    assert len(DatasetReader([store]).classifier_rows()) == 1
    store.annotate(record["trial_id"], "flipped", False, "Nach Bildprüfung wieder eingeschlossen")
    assert len(DatasetReader([store]).classifier_rows()) == 2
    assert len(read_json(directory / "annotations.json")["history"]) == 2
    assert original == (directory / "trial.json").read_bytes()
    assert DatasetReader([store]).export_csv(tmp_path / "dataset.csv") == 2
    assert "input_pressure_mbar" in (tmp_path / "dataset.csv").read_text("utf-8-sig")


def test_camera_configuration_change_prevents_any_new_trial(experiment):
    runner, pressure, vision, clock, until, events = experiment
    original = vision.state
    vision.state = lambda: {**original(), "processing": {"threshold": 99}}
    with pytest.raises(RuntimeError, match="konfiguration"):
        runner.confirm()
    assert not vision.trials


def test_import_rejects_wrong_trial_and_path_traversal(experiment):
    runner, pressure, vision, clock, until, events = experiment
    runner.confirm()
    until("waiting")
    trial_id = runner.record["trial_id"]
    pressure.moving = True
    clock[0] += 2
    result = vision.trial(trial_id)
    source = result["clip_path"]
    manifest = read_json(source + "/manifest.json")
    manifest["metadata"]["trial_id"] = "wrong"
    from bibazu_autotune.engine import atomic_json

    atomic_json(source + "/manifest.json", manifest)
    with pytest.raises(ValueError, match="gehört nicht"):
        runner.store.finish(runner.record, result)
    manifest["metadata"]["trial_id"] = trial_id
    manifest["frames"][0]["file"] = "../../outside.npy"
    atomic_json(source + "/manifest.json", manifest)
    with pytest.raises(ValueError, match="verlässt"):
        runner.store.finish(runner.record, result)


def test_interrupted_attempts_remain_and_do_not_resolve_slot(plan_assets, tmp_path):
    plan, assets = plan_assets
    store = CampaignStore.create(tmp_path / "runs", plan, assets=assets)
    record = {
        "trial_id": "a" * 32,
        "status": "archiving",
        "slot": 0,
        "created_utc": "2026-01-01",
        "point_index": 0,
    }
    store.save_trial(record)
    store.interrupt_unfinished()
    assert store.trials()[0]["status"] == "interrupted"
    assert store.next_slot() == 0


def test_eight_unrotated_passages_are_eight_not_flipped_trials(experiment, plan_assets, tmp_path):
    runner, pressure, vision, clock, until, events = experiment
    runner.end_campaign(False, runner.store.directory)
    runner.connect()
    until("ready")
    plan, assets = plan_assets
    plan = replace(
        plan,
        pressure=SweepRange(0, 0, 100),
        pulse=SweepRange(50, 50, 10),
        repetitions=8,
        settling_seconds=0,
    )
    runner.create(tmp_path / "zero-rotation", plan, assets, {})
    until("gate")
    runner.confirm()
    until("waiting")
    runner.conveyor(True)
    until("complete", limit=30000)
    records = runner.store.trials()
    assert len(records) == 8 and runner.slot == 8
    assert len({r["trial_id"] for r in records}) == 8
    assert all(r["evaluation"]["label"] == "not_flipped" for r in records)
    assert all(r["resolution"] == "accepted" for r in records)
