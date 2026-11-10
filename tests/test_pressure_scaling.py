import copy
from dataclasses import replace

import pytest
from test_analytical_model import analyzed_body, configured
from test_candidate_transfer import candidate
from test_engine import parameters

from bibazu_autotune.analytical_model import AnalyticalModel, ModelConfiguration, PressurePrediction
from bibazu_autotune.candidate_transfer import candidate_parameters
from bibazu_autotune.pressure_scaling import (
    DEFAULT_PRESSURE_SCALE_A,
    DEFAULT_PRESSURE_SCALE_B_PA,
    SpsPressurePrediction,
    scale_pressure,
)


def test_100_ms_peak_reference_in_si_and_scaled_editor_value():
    result = scale_pressure(200000, DEFAULT_PRESSURE_SCALE_A, DEFAULT_PRESSURE_SCALE_B_PA)
    assert result.gauge_pa / 100 == pytest.approx(3491.248526650112, abs=1e-9)
    assert result.scale_b_pa / 100 == pytest.approx(-284.80891486008954)
    value = replace(candidate(pressure=2000), sps_pressure=result)
    before = parameters()
    saved = copy.deepcopy(before)
    proposal = candidate_parameters(before, value)
    assert before == saved
    assert value.pressure.gauge_pa == 200000
    assert proposal["arrays"][1]["pressure_mbar"] == 3491


@pytest.mark.parametrize("pressure_pa", [0, 100, 600000, 1e6])
def test_neutral_scaling_keeps_analytical_pressure(pressure_pa):
    assert scale_pressure(pressure_pa, 1, 0).gauge_pa == pressure_pa


@pytest.mark.parametrize("b_pa,expected", [(-1000, 402000), (0, 400000), (1000, 398000)])
def test_signed_intercept_is_subtracted_in_pa(b_pa, expected):
    assert scale_pressure(200000, 0.5, b_pa).gauge_pa == expected


@pytest.mark.parametrize("a", [0, -1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_slope_rejected_by_model_and_scaler(a):
    with pytest.raises(ValueError, match="Druckskalierung a"):
        ModelConfiguration(pressure_scale_a=a).validate()
    with pytest.raises(ValueError, match="Druckskalierung a"):
        scale_pressure(100, a, 0)


@pytest.mark.parametrize("b", [float("nan"), float("inf"), True, "-100"])
def test_invalid_intercept_rejected_by_model_and_scaler(b):
    with pytest.raises(ValueError, match="Druckskalierung b"):
        ModelConfiguration(pressure_scale_b_pa=b).validate()
    with pytest.raises(ValueError, match="Druckskalierung b"):
        scale_pressure(100, 1, b)


def test_overflow_and_invalid_analytical_pressure_have_explicit_errors():
    with pytest.raises(ValueError, match="endlichen Zahlenbereichs"):
        scale_pressure(1e308, 1e-308, 0)
    for value in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="Analytischer Überdruck"):
            scale_pressure(value, 1, 0)


@pytest.mark.parametrize("pressure_pa", [-0.01, 600000.01, float("nan"), float("inf")])
def test_only_scaled_unrounded_bounds_block_atomic_transfer(pressure_pa):
    original = parameters()
    before = copy.deepcopy(original)
    value = replace(candidate(pressure=1000), sps_pressure=SpsPressurePrediction(pressure_pa, 1, 0))
    with pytest.raises(ValueError, match="SPS-Grenzen"):
        candidate_parameters(original, value)
    assert original == before


@pytest.mark.parametrize("analytical_pa,a,b", [(0, 1, 1), (600001, 1, 0), (600000, 0.5, 0)])
def test_finite_scaled_values_are_shown_without_clipping(analytical_pa, a, b):
    result = scale_pressure(analytical_pa, a, b)
    assert result.gauge_pa == (analytical_pa - b) / a
    assert "SPS-Druckgrenzen" in result.warnings[0]


def test_sps_bounds_use_scaled_pressure_not_analytical_pressure():
    value = replace(candidate(pressure=7000), sps_pressure=scale_pressure(700000, 2, 0))
    assert candidate_parameters(parameters(), value)["arrays"][1]["pressure_mbar"] == 3500
    value = replace(value, sps_pressure=None)
    with pytest.raises(ValueError):
        candidate_parameters(parameters(), value)


def test_same_scaling_for_other_arrays_and_pulses_leaves_physics_unchanged(tmp_path):
    analysis, context, _ = analyzed_body(tmp_path)
    params = parameters()
    params["arrays"][3]["pulse_duration_ms"] = 100
    config = configured(uphill_deg=0, cross_slope_deg=0)
    model = AnalyticalModel()
    neutral = model.evaluate(
        analysis, context, params, replace(config, pressure_scale_a=1, pressure_scale_b_pa=0)
    )
    calibrated = model.evaluate(analysis, context, params, config)
    assert [(c.mount.array_index, c.pulse_ms) for c in calibrated] == [(2, 50), (4, 100)]
    for old, new in zip(neutral, calibrated, strict=True):
        assert old.pressure is not None and old.pressure == new.pressure
        assert old.mechanics == new.mechanics and old.impact == new.impact
        assert new.sps_pressure.scale_a == config.pressure_scale_a
        assert new.sps_pressure.scale_b_pa == config.pressure_scale_b_pa
        assert new.sps_pressure.gauge_pa == pytest.approx(
            (new.pressure.gauge_pa - config.pressure_scale_b_pa) / config.pressure_scale_a
        )


def test_scaling_overflow_keeps_analytical_result_visible(tmp_path):
    analysis, context, _ = analyzed_body(tmp_path)

    class Pressure:
        def predict(self, body, mechanics, impact, pulse_ms, config):
            return PressurePrediction(1, config.ambient_pa + 1e8, 1e8, 90000, ())

    candidates = AnalyticalModel(pressure=Pressure()).evaluate(
        analysis, context, parameters(), configured(pressure_scale_a=1e-310)
    )
    for value in candidates:
        assert value.status == "blocked"
        assert value.pressure.gauge_pa == 1e8
        assert value.impact is not None and value.mechanics is not None
        assert value.sps_pressure is None
        assert "Skalierter SPS-Druck" in value.message
