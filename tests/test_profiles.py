import copy

import pytest
from test_engine import context, parameters

from bibazu_autotune.profiles import make_profile, profile_parameters


def test_version_13_roundtrip_preserves_metadata_and_is_portable():
    source = make_profile(
        parameters(),
        context(),
        {
            "light_barrier_debounce_ms": 20,
            "conveyor_calibration": {"valid": True, "mm_per_full_step": 0.02},
        },
        {"ur_ry_angle_deg": 15.5, "operator_note": "known setup"},
    )
    assert source["version"] == 13
    assert source["ur_ry_angle_deg"] == 15.5
    assert source["operator_note"] == "known setup"
    assert source["roadmap_transition"] == context()
    assert source["arrays"][0]["nozzle_1_enabled"] is True
    assert profile_parameters(source) == parameters()
    # Informational fields do not reach the parameter API.
    source["conveyor_calibration"]["mm_per_full_step"] = -999
    source["conveyor_enabled"] = True
    assert profile_parameters(source) == parameters()


def test_profile_uses_separate_nozzle_flags_when_list_is_absent():
    source = make_profile(parameters(), context())
    for array in source["arrays"]:
        del array["nozzles_enabled"]
    assert profile_parameters(source) == parameters()


def test_new_profile_defaults_angle_and_does_not_mutate_parameters():
    candidate = parameters()
    before = copy.deepcopy(candidate)
    source = make_profile(candidate, context())
    assert source["ur_ry_angle_deg"] == 18
    assert candidate == before


def test_invalid_profile_has_clear_error():
    with pytest.raises(ValueError, match="Version 13"):
        profile_parameters({"version": 99})
    with pytest.raises(ValueError, match="keine Arrays"):
        profile_parameters({"version": 13})
