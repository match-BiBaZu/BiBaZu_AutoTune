import copy
import json
from dataclasses import replace

import pytest
import trimesh
from shapely import box
from test_analytical_model import configured, pose
from test_engine import parameters
from test_geometry import write_box
from test_gui import offline_window as offline_window
from test_model_gui import prepare

from bibazu_autotune.analytical_model import (
    AnalyticalModel,
    ModelConfiguration,
    _safe_x_intervals,
)
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.model_defaults import ModelDefaultsStore
from bibazu_autotune.model_panel import ModelPanel


@pytest.mark.parametrize("axis,angle", [("y", 90), ("y", -90), ("z", 90), ("z", -90)])
def test_offset_reserve_moves_force_inward_for_either_side(tmp_path, axis, angle):
    path = tmp_path / "part.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(trimesh.creation.box(extents=[80, 30, 40])))
    analysis = analyze_stl(path, density=1200)
    config = configured(uphill_deg=0, cross_slope_deg=45, spread_angle_deg=0)
    baseline = AnalyticalModel().evaluate(analysis, pose(axis, angle), parameters(), config)[0]
    reserved = AnalyticalModel().evaluate(
        analysis, pose(axis, angle), parameters(), replace(config, offset_safety_m=0.005)
    )[0]
    assert reserved.pressure is not None, reserved.message
    old, new = baseline.impact, reserved.impact
    assert old is not None
    assert new.active_nozzles == old.active_nozzles == (1,)
    assert new.area_eff_m2 == pytest.approx(old.area_eff_m2)
    assert new.lever_arm_m == pytest.approx(old.lever_arm_m - 0.005, abs=1e-7)
    assert abs(new.offset_m - old.offset_m) == pytest.approx(0.005, abs=1e-7)
    for old_point, point in zip(old.points_m[:8], new.points_m[:8], strict=True):
        assert abs(point[0]) == pytest.approx(abs(old_point[0]) - 0.005, abs=1e-7)
        assert point[1:] == old_point[1:]  # Transverse coverage is unchanged.
    if axis == "z" and angle < 0:
        assert old.offset_m * 1000 == pytest.approx(79, abs=1e-4)
        assert new.offset_m * 1000 == pytest.approx(74, abs=1e-4)


def test_offset_reserve_respects_holes_and_rejects_short_surface_intervals():
    surface = box(0, 0, 30, 20).difference(box(10, 5, 20, 15))
    intervals = _safe_x_intervals(surface, [10, 2], surface.bounds, 2)
    assert intervals == [((2, 8), (22, 28)), ((2, 28),)]
    assert _safe_x_intervals(surface, [10], surface.bounds, 6) == [()]


def test_short_part_is_blocked_if_reserve_does_not_fit(tmp_path):
    analysis = analyze_stl(write_box(tmp_path / "short.stl"), density=1200)
    config = ModelConfiguration(uphill_deg=0, cross_slope_deg=0)
    config = replace(config, mounts=tuple(replace(m, row_center_m=0.015) for m in config.mounts))
    result = AnalyticalModel().evaluate(analysis, pose(), parameters(), config)
    assert len(result) == 2
    assert all(c.impact is None and c.pressure is None for c in result)
    assert all("Offsetreserve" in c.message for c in result)
    without_reserve = AnalyticalModel().evaluate(
        analysis, pose(), parameters(), replace(config, offset_safety_m=0)
    )
    assert all(c.pressure is not None for c in without_reserve)


@pytest.mark.parametrize("value", [-0.001, float("inf"), float("nan")])
def test_invalid_offset_reserve_is_rejected(value):
    with pytest.raises(ValueError, match="Offsetreserve"):
        ModelConfiguration(offset_safety_m=value).validate()


def test_old_defaults_get_new_reserve_without_changing_saved_values(tmp_path):
    path = tmp_path / "defaults.json"
    raw = {"schema_version": 2, "model_parameters": {"margin_m": 0.001}}
    path.write_text(json.dumps(raw), "utf-8")
    before = path.read_bytes()
    store = ModelDefaultsStore(path)
    loaded = store.load()
    assert loaded.configuration.margin_m == 0.001
    assert loaded.configuration.offset_safety_m == 0.005
    assert path.read_bytes() == before
    store.save_parameters(replace(loaded.configuration, offset_safety_m=0.007), 1200)
    assert store.load().configuration.offset_safety_m == 0.007


def test_gui_offset_reserve_invalidates_results_and_saves_default(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    assert window.autotune_settings.model_panel.fields["offset_safety_m"][0].value() == 5
    settings, panel = prepare(window, wait, tmp_path)
    reserve = panel.fields["offset_safety_m"][0]
    reserve.setValue(5)
    before = copy.deepcopy(window.editor.parameters())
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    selected = window.autotune_results.selected_candidate()
    assert selected.impact.offset_safety_m == 0.005
    assert "Offsetreserve entlang X [mm]: 5" in window.autotune_results.details.toPlainText()
    reserve.setValue(7)
    assert settings.model_result is None
    assert not window.import_button.isEnabled()
    panel.save_parameters_button.click()
    reopened = ModelPanel(defaults_path=panel.defaults_store.path)
    try:
        assert reopened.fields["offset_safety_m"][0].value() == 7
    finally:
        reopened.deleteLater()
    assert window.editor.parameters() == before
    assert not services.calls and not errors
