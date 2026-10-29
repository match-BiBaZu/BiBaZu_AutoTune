import copy
from dataclasses import replace

import pytest
from test_engine import parameters

from bibazu_autotune.analytical_model import (
    CandidateResult,
    ImpactSolution,
    NozzleMount,
    PressurePrediction,
    Turn,
)
from bibazu_autotune.candidate_transfer import candidate_parameters


def candidate(pressure=1234.5, offset=12.25, array=2, nozzle=1, status="ready"):
    return CandidateResult(
        mount=NozzleMount(array, nozzle, "y", "z", 0.015),
        turn=Turn("y", 90),
        pulse_ms=50,
        status=status,
        message="Referenzbereich/Strömungswarnung" if status == "outside_model" else "Bereit",
        impact=ImpactSolution(
            offset / 1000,
            (1.0,) * 8,
            ((0.01, 0.01, 0),) * 8,
            0.01,
            1e-5,
            0.001,
            ((0, 0, 0),) * 8,
            ((0, 0, 0),) * 8,
            (((0, 0), (0.02, 0), (0.02, 0.02), (0, 0.02), (0, 0)),),
        ),
        pressure=PressurePrediction(1, 101300 + pressure * 100, pressure * 100, 90000, ()),
    )


@pytest.mark.parametrize("array,nozzle", [(1, 4), (2, 1), (3, 4), (4, 1)])
def test_selected_array_only_and_commercial_rounding(array, nozzle):
    original = parameters()
    original["arrays"][array - 1]["delay_ms"] = 37
    original["arrays"][array - 1]["conveyor_velocity_delay"] = False
    before = copy.deepcopy(original)
    proposal = candidate_parameters(original, candidate(array=array, nozzle=nozzle))
    assert original == before
    assert proposal["conveyor_speed_mm_per_sec"] == before["conveyor_speed_mm_per_sec"]
    for old, new in zip(before["arrays"], proposal["arrays"], strict=True):
        if old["index"] != array:
            assert new == old
        else:
            assert new == {
                **old,
                "pressure_mbar": 1235,
                "offset_mm": 12.3,
                "enabled": True,
                "nozzles_enabled": [i == nozzle for i in range(1, 7)],
                "conveyor_velocity_delay": True,
            }


@pytest.mark.parametrize(
    "pressure,offset",
    [
        (6000.001, 1),
        (-0.001, 1),
        (float("nan"), 1),
        (float("inf"), 1),
        (100, 5000.001),
        (100, -0.001),
        (100, float("nan")),
        (100, float("inf")),
    ],
)
def test_unrounded_bounds_reject_without_mutating_input(pressure, offset):
    original = parameters()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="SPS-Grenzen"):
        candidate_parameters(original, candidate(pressure, offset))
    assert original == before


@pytest.mark.parametrize("pressure,offset", [(0, 0), (6000, 5000)])
def test_sps_bounds_inclusive_and_model_warnings_allowed(pressure, offset):
    proposal = candidate_parameters(
        parameters(), candidate(pressure, offset, status="outside_model")
    )
    assert proposal["arrays"][1]["pressure_mbar"] == pressure
    assert proposal["arrays"][1]["offset_mm"] == offset


@pytest.mark.parametrize(
    "invalid",
    [
        candidate(status="blocked"),
        candidate(status="pending"),
        replace(candidate(), pressure=None),
        replace(candidate(), impact=None),
        candidate(array=0),
        candidate(nozzle=2),
    ],
)
def test_incomplete_or_invalid_candidates_rejected(invalid):
    with pytest.raises(ValueError):
        candidate_parameters(parameters(), invalid)


def test_zero_speed_and_stale_pulse_rejected():
    original = parameters()
    original["conveyor_speed_mm_per_sec"] = 0
    for array in original["arrays"]:
        array["conveyor_velocity_delay"] = False
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="Bandgeschwindigkeit größer als 0"):
        candidate_parameters(original, candidate())
    assert original == before
    with pytest.raises(ValueError, match="Impulsdauer"):
        candidate_parameters(parameters(), replace(candidate(), pulse_ms=100))


@pytest.mark.parametrize("offset_m,expected_mm", [(0.00245, 2.5), (0.00895, 9.0), (0.00005, 0.1)])
def test_si_conversion_preserves_commercial_half_rounding(offset_m, expected_mm):
    value = candidate()
    value = replace(value, impact=replace(value.impact, offset_m=offset_m))
    proposal = candidate_parameters(parameters(), value)
    assert proposal["arrays"][1]["offset_mm"] == expected_mm
