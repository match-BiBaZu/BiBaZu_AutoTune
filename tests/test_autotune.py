import copy
import json
from dataclasses import replace

import pytest
from test_engine import Services, context, parameters
from test_geometry import write_box

from bibazu_autotune.analytical_model import (
    AutomaticOffsetEstimator,
    ModelConfiguration,
    PressurePrediction,
)
from bibazu_autotune.autotune import AutotunePipeline, StageResult
from bibazu_autotune.engine import Engine
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.profiles import make_profile, profile_parameters
from bibazu_autotune.recipes import transition_identity, validate_recipe
from bibazu_autotune.trial_context import (
    context_fields,
    manual_context,
    normalize_quaternion,
    read_context,
)


def manual(sha="a" * 64, target=(0, 1, 0, 1)):
    return manual_context("Trainingsteil", sha, (0, 0, 0, 2), target)


@pytest.mark.parametrize(
    "quaternion",
    [[], [0, 0, 0, 0], [0, 0, 0, float("nan")], [0, 0, float("inf"), 1], [True, 0, 0, 1]],
)
def test_bad_quaternions(quaternion):
    with pytest.raises(ValueError, match="Quaternion"):
        normalize_quaternion(quaternion)


def test_quaternion_identity_and_scaled_inputs():
    assert normalize_quaternion([0, 0, 0, 1e-300]) == (0, 0, 0, 1)
    assert normalize_quaternion([0, 0, 0, 1e300]) == (0, 0, 0, 1)
    assert transition_identity(manual()) == transition_identity(manual(target=(0, -3, 0, -3)))
    assert transition_identity(manual()) != transition_identity(manual(sha="b" * 64))
    assert transition_identity(manual()) != transition_identity(manual(target=(0, 0, 0, 1)))
    with pytest.raises(ValueError, match="Bauteilname"):
        manual_context(" ", "a" * 64, [0, 0, 0, 1], [0, 0, 0, 1])


def test_manual_recipe_profile_roundtrip_and_ambiguous_contexts():
    ctx = manual()
    recipe = {
        "schema_version": 1,
        **context_fields(ctx),
        "variants": [{"name": "Training", "parameters": parameters()}],
    }
    assert read_context(recipe) == ctx
    assert validate_recipe(recipe, ctx)[0]["parameters"] == parameters()
    with pytest.raises(ValueError, match="anderen"):
        validate_recipe(recipe, manual(sha="b" * 64))
    profile = make_profile(parameters(), ctx, template={"roadmap_transition": context()})
    assert profile["version"] == 13
    assert "roadmap_transition" not in profile
    assert read_context(profile) == ctx
    assert profile_parameters(profile) == parameters()
    roadmap_profile = make_profile(parameters(), context(), template=profile)
    assert "trial_context" not in roadmap_profile
    assert read_context(roadmap_profile) == context()
    with pytest.raises(ValueError, match="widersprüchliche"):
        read_context({**recipe, "roadmap_transition": context()})
    with pytest.raises(ValueError, match="manuellen"):
        read_context({"trial_context": context()})


def test_missing_mount_blocks_pressure_without_side_effects(tmp_path):
    analysis = analyze_stl(write_box(tmp_path / "box.stl"), density=2700)
    candidate = parameters()
    before = copy.deepcopy(candidate)
    result = AutotunePipeline().prepare(analysis, manual(analysis.sha256), candidate)
    assert not result.missing
    assert result.pulse.value == {1: 50, 2: 50, 3: 50, 4: 50}
    assert result.pulse.source == "manual"
    for stage in (result.offset, result.pressure):
        assert stage.status == "blocked"
        assert stage.value is None
    assert all("Montageposition" in c.message for c in result.candidates)
    assert result.ml.status == "not_implemented"
    assert candidate == before
    missing = AutotunePipeline().prepare(None, None, candidate)
    assert any("STL" in text for text in missing.missing)
    assert any("Posen" in text for text in missing.missing)
    mismatch = AutotunePipeline().prepare(analysis, manual(), candidate)
    assert any("passt nicht" in text for text in mismatch.missing)


def test_replacement_stages_obey_dependencies(tmp_path):
    analysis = analyze_stl(write_box(tmp_path / "box.stl"), density=2700)
    calls = []

    class Offset:
        def estimate(self, body, turn, mount, mechanics, config):
            calls.append("offset")
            return AutomaticOffsetEstimator().estimate(body, turn, mount, mechanics, config)

    class Pressure:
        def predict(self, body, mechanics, impact, pulse_ms, config):
            assert pulse_ms == 50
            assert impact.lever_arm_m > 0
            calls.append("pressure")
            return PressurePrediction(0.1, config.ambient_pa + 12000, 12000, 90000, ())

    class ML:
        def refine(self, request, pressures_mbar):
            calls.append("ml")
            assert pressures_mbar == {2: 120, 4: 120}
            return StageResult("ready", {2: 125.0, 4: 125.0})

    pipeline = AutotunePipeline(offset=Offset(), pressure=Pressure(), ml=ML())
    result = pipeline.prepare(analysis, manual(analysis.sha256), parameters())
    assert calls == []  # Missing mounts prevent downstream model calls.
    assert result.pressure.value is None
    config = ModelConfiguration(uphill_deg=0, cross_slope_deg=0)
    config = replace(config, mounts=tuple(replace(m, row_center_m=0.015) for m in config.mounts))
    result = pipeline.prepare(analysis, manual(analysis.sha256), parameters(), config)
    assert calls == ["offset", "pressure", "offset", "pressure", "ml"]
    assert result.ml.value == {2: 125, 4: 125}


def test_complete_manual_trial_logs_only_pose_context_and_starts_new_runs(tmp_path):
    services = Services()
    engine = Engine(*services.clients(), tmp_path)
    ctx = manual()
    engine.set_context(ctx)
    engine.connect()
    engine.tick()
    run_id = engine.run_id
    assert not any(method == "PUT" for method, _, _ in services.calls)
    engine.set_context(manual(target=(0, -2, 0, -2)))
    assert engine.run_id == run_id
    engine.start(parameters())
    engine.tick()
    assert engine.state == "waiting"
    trial_id = engine.current
    assert services.trials[trial_id]["context"]["trial_context"] == ctx
    engine.trigger()
    services.trials[trial_id].update(status="completed", measurements=[], clip_path="demo")
    engine.tick()
    assert engine.state == "ready"
    for path in (tmp_path / run_id).glob("*.json"):
        record = json.loads(path.read_text("utf-8"))
        assert record["trial_context"] == ctx
        assert "roadmap_transition" not in record
        assert not {"geometry", "density", "model_status", "unit"} & record.keys()
    profile = tmp_path / "profile.json"
    engine.save(parameters(), profile)
    engine.tick()
    assert read_context(json.loads(profile.read_text("utf-8"))) == ctx
    engine.set_context(manual(sha="b" * 64))
    assert engine.run_id != run_id
    run_id = engine.run_id
    engine.set_context(manual(sha="b" * 64, target=(1, 0, 0, 1)))
    assert engine.run_id != run_id
    assert not any("conveyor/start" in path for _, path, _ in services.calls)
    engine.release()
