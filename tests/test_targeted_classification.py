"""Phase-specific tolerance must not invent angles or hide interrupted rotations."""

import copy
import math
from dataclasses import replace
from uuid import uuid4

import pytest
from test_experiments import plan_assets as plan_assets

from bibazu_autotune.engine import atomic_json
from bibazu_autotune.experiments.classification import evaluate
from bibazu_autotune.experiments.models import ClassificationConfig, ExperimentPlan
from bibazu_autotune.experiments.storage import CampaignStore


def measurements(final=90, tail=31):
    angles = [0.0] * 11 + [final * i / 16 for i in range(1, 17)] + [final] * tail
    return [
        {
            "timestamp": i * 0.01,
            "frame_id": i,
            "angle_deg": (angle + 90) % 180 - 90,
            "present": True,
            "valid": True,
            "touching_border": False,
        }
        for i, angle in enumerate(angles)
    ]


def config(**kwargs):
    return ClassificationConfig(90, observable_confirmed=True, **kwargs)


@pytest.mark.parametrize(
    "final,label", [(0, "not_flipped"), (90, "flipped"), (180, "overflipped"), (360, "overflipped")]
)
def test_v2_preserves_wraps_and_endpoint_classification(final, label):
    result, _ = evaluate(measurements(final), config())
    assert result["algorithm_version"] == 2
    assert result["label"] == label
    assert result["rotation_deg"] == pytest.approx(final)


@pytest.mark.parametrize("invalid", [False, True])
def test_short_rest_gap_is_tolerated_with_original_missing_data_preserved(invalid):
    rows = measurements()
    if invalid:
        rows[40]["angle_deg"] = None
    else:
        del rows[40]
    original = copy.deepcopy(rows)
    result, samples = evaluate(rows, config())
    assert result["label"] == "flipped"
    assert result["warnings"] == ["rest_gap_bridged"]
    assert len(result["bridged_gaps"]) == 1
    assert rows == original
    assert len(samples) == len(rows)
    assert [s["angle_deg"] for s in samples] == [r["angle_deg"] for r in rows]
    if invalid:
        assert samples[40]["continuous_angle_deg"] is None
    legacy, _ = evaluate(rows, config(algorithm_version=1))
    assert legacy["label"] is None


@pytest.mark.parametrize("case", ["moving", "long", "many_ids", "clock_reset", "ambiguous"])
def test_critical_gaps_remain_uncertain(case):
    rows = measurements()
    if case == "moving":
        del rows[20]
    elif case == "long":
        del rows[40:44]
    elif case == "many_ids":
        for row in rows[40:]:
            row["frame_id"] += 10
    elif case == "clock_reset":
        rows[40]["timestamp"] = 0.1
    else:
        rows[20]["angle_deg"] = rows[19]["angle_deg"] + 90
    result, _ = evaluate(rows, config())
    assert result["label"] is None
    assert "interrupted_rotation" in result["reasons"]


def test_invalid_prefix_suffix_are_retained_outside_analysis():
    rows = measurements()
    rows.insert(0, {"timestamp": -0.01, "angle_deg": None, "present": False})
    rows.append({"timestamp": 0.9, "angle_deg": None, "present": False})
    result, samples = evaluate(rows, config())
    assert result["label"] == "flipped"
    assert "unusable_prefix_ignored" in result["warnings"]
    assert "unusable_suffix_ignored" in result["warnings"]
    assert len(samples) == len(rows)


def test_unsettled_valid_motion_cannot_be_trimmed_away_as_unusable_suffix():
    result, _ = evaluate(measurements()[:23], config())
    assert result["label"] is None
    assert "no_stable_final_pose" in result["reasons"]


@pytest.mark.parametrize("final", [90, 180, 360])
def test_memory_abort_after_observed_rotation_and_long_final_pose_can_be_valid(final):
    result, _ = evaluate(
        measurements(final), config(), completed=False, dropped_frames=1, stop_reason="memory_limit"
    )
    assert result["quality"] == "valid"
    assert result["rotation_deg"] == pytest.approx(final)
    assert result["reasons"] == []
    assert "memory_limit_after_stable_final_pose" in result["warnings"]


def test_small_excursion_returning_to_source_can_be_accepted_after_memory_abort():
    rows = measurements(0)
    for index in range(11, 27):
        rows[index]["angle_deg"] = 33 * math.sin(math.pi * (index - 10) / 17)
    result, _ = evaluate(
        rows, config(), completed=False, dropped_frames=1, stop_reason="memory_limit"
    )
    assert result["label"] == "not_flipped"
    assert result["max_rotation_deg"] > 30


@pytest.mark.parametrize(
    "case",
    ["flat", "short_final", "during_motion", "other_abort", "unlocated_drops", "unobservable"],
)
def test_incomplete_or_unobservable_events_are_not_silently_accepted(case):
    rows, cfg, reason, drops = measurements(), config(), "memory_limit", 1
    if case == "flat":
        rows = measurements(0)
    elif case == "short_final":
        rows = measurements(tail=6)
    elif case == "during_motion":
        rows = rows[:23]
    elif case == "other_abort":
        reason = "session_expired"
    elif case == "unlocated_drops":
        drops = 2
    else:
        cfg = replace(cfg, observable_confirmed=False)
    result, _ = evaluate(rows, cfg, completed=False, dropped_frames=drops, stop_reason=reason)
    assert result["label"] is None


def test_old_plan_documents_keep_v1_and_unfiltered_presence(plan_assets):
    plan, _ = plan_assets
    document = plan.to_dict()
    document.pop("presence_confirm_seconds")
    document["classification"].pop("algorithm_version")
    old = ExperimentPlan.from_dict(document)
    assert old.classification.algorithm_version == 1
    assert old.presence_confirm_seconds is None
    assert ExperimentPlan.from_dict(plan.to_dict()).classification.algorithm_version == 2


def test_disabled_new_camera_guard_does_not_invalidate_legacy_camera_signature():
    from bibazu_autotune.experiments.runner import camera_signature

    legacy = {"capture_config": {"start_mode": "presence", "pre_roll_seconds": 0.15}}
    current = copy.deepcopy(legacy)
    current["capture_config"]["presence_confirm_seconds"] = 0.0
    assert camera_signature(legacy) == camera_signature(current)
    current["capture_config"]["presence_confirm_seconds"] = 0.1
    assert camera_signature(legacy) != camera_signature(current)


@pytest.mark.parametrize(
    "status,error,accepted",
    [
        ("failed", None, True),
        ("aborted", None, False),
        ("failed", "IO error", False),
    ],
)
def test_archiving_keeps_camera_status_and_only_accepts_clean_terminal_memory_cutoff(
    plan_assets, tmp_path, status, error, accepted
):
    plan, assets = plan_assets
    plan = replace(plan, storage_profile="review_video_v1")
    store = CampaignStore.create(tmp_path / "campaigns", plan, assets=assets)
    trial_id = uuid4().hex
    record = {
        "trial_id": trial_id,
        "campaign_id": store.campaign_id,
        "slot": 0,
        "point_index": 0,
        "repetition": 1,
        "created_utc": "2026-11-18",
        "status": "recording",
        "requested_parameters": plan.parameters_at(plan.points()[0]),
    }
    store.save_trial(record)
    directory = store.reserve_capture(record)
    clip = directory / "clip"
    clip.mkdir()
    rows = measurements()
    atomic_json(
        clip / "manifest.json",
        {
            "schema_version": 2,
            "metadata": {"run_id": store.campaign_id, "trial_id": trial_id},
            "stop_reason": "memory_limit",
            "aborted": True,
            "dropped_frames": 1,
            "frame_count": len(rows),
            "frames": [
                {"timestamp": row["timestamp"], "frame_id": row["frame_id"], "measurement": row}
                for row in rows
            ],
            "media": {"file": "review.mp4", "state": "failed"},
        },
    )
    original = (clip / "manifest.json").read_bytes()
    result = store.finish(
        record,
        {
            "status": status,
            "error": error,
            "clip_path": str(clip),
            "stop_reason": "memory_limit",
        },
    )
    assert result["status"] == status
    assert result["stop_reason"] == "memory_limit"
    assert (result["evaluation"]["quality"] == "valid") == accepted
    assert (clip / "manifest.json").read_bytes() == original


def test_old_camera_is_rejected_before_creating_new_guarded_campaign(plan_assets, tmp_path):
    from bibazu_autotune.clients import ApiFailure
    from bibazu_autotune.experiments.demo import DemoPressure, DemoVision
    from bibazu_autotune.experiments.runner import ExperimentRunner

    pressure = DemoPressure()
    vision = DemoVision(pressure)
    runner = ExperimentRunner(pressure, vision)
    try:
        runner.connect()
        runner.tick()
        capabilities = vision.capabilities()
        capabilities.pop("qualified_presence_start")
        vision.capabilities = lambda: capabilities
        plan, assets = plan_assets
        with pytest.raises(ApiFailure, match="bestätigten Bauteilstart"):
            runner.create(tmp_path / "new-campaigns", plan, assets, {})
        assert not (tmp_path / "new-campaigns").exists()
    finally:
        runner.close()
        vision.close()
