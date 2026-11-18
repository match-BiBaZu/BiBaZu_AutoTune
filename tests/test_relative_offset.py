import copy
import json
import math
from dataclasses import replace

import pytest
import trimesh
from shapely import box
from test_analytical_model import configured, pose
from test_engine import parameters
from test_gui import offline_window as offline_window
from test_model_gui import select_manual
from test_nozzle_groups import box_analysis, impact_for, measured_config

from bibazu_autotune.analytical_model import (
    AnalyticalModel,
    ModelConfiguration,
    _safe_x_intervals,
    drag_force,
)
from bibazu_autotune.candidate_transfer import candidate_parameters
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.model_defaults import ModelDefaultsStore
from bibazu_autotune.model_panel import ModelPanel


def rectangular_candidate(tmp_path, length, axis="y", angle=90, fraction=0.20):
    path = tmp_path / "relative.stl"
    trimesh.creation.box(extents=[length, 30, 40]).export(path)
    analysis = analyze_stl(path, density=1200)
    config = configured(
        diameter_m=0.0008,
        distance_m=0.0174,
        uphill_deg=0,
        cross_slope_deg=45,
        offset_strategy="relative",
        offset_inset_fraction=fraction,
        offset_safety_m=0.1,  # Must not be added to the relative inset.
    )
    candidate = AnalyticalModel().evaluate(analysis, pose(axis, angle), parameters(), config)[0]
    assert candidate.pressure is not None, candidate.message
    return analysis, config, candidate


@pytest.mark.parametrize("length", [20, 80])
@pytest.mark.parametrize("axis,angle", [("y", 90), ("y", -90), ("z", 90), ("z", -90)])
@pytest.mark.parametrize("fraction", [0, 0.20, 0.50])
def test_relative_inset_matches_independent_rectangle_reference(
    tmp_path, length, axis, angle, fraction
):
    analysis, config, candidate = rectangular_candidate(tmp_path, length, axis, angle, fraction)
    impact, mechanics = candidate.impact, candidate.mechanics
    # Independent formulas: distance is measured from the front to the nozzle CENTER.
    radius = (17.4 * math.tan(math.radians(10)) + 0.4) / 1000
    clearance = radius + 0.001
    early = clearance + fraction * (length / 1000 - 2 * clearance)
    late = clearance + (1 - fraction) * (length / 1000 - 2 * clearance)
    expected = late if (axis == "y") == (angle > 0) else early
    assert impact.offset_m == pytest.approx(expected, abs=1e-7)
    assert impact.lever_arm_m == pytest.approx(late, abs=1e-7)
    assert impact.offset_strategy == "relative" and impact.offset_safety_m == 0
    assert impact.offset_inset_fraction == fraction
    assert impact.active_nozzles == (1,)
    for point in impact.points_m:
        assert point[0] == pytest.approx(length / 2000 - impact.offset_m)
    force = math.sqrt(
        2 * mechanics.inertia_axis_kg_m2 * analysis.material.mass_kg * 9.81 * mechanics.delta_h_m
    ) / (candidate.pulse_ms / 1000 * late)
    # The conservative polygon buffer shifts the nominal boundary by <0.1 µm.
    assert candidate.pressure.force_n == pytest.approx(force, rel=1e-5)
    assert drag_force(candidate.pressure.gauge_pa, impact.area_eff_m2, config) == pytest.approx(
        force, rel=1e-5
    )


def test_relative_inset_keeps_holes_and_separate_faces_separate():
    surface = box(0, 0, 30, 20).difference(box(10, 5, 20, 15))
    assert _safe_x_intervals(surface, [10, 2], surface.bounds, 0, fraction=0.20) == [
        ((2, 8), (22, 28)),
        ((6, 24),),
    ]
    assert _safe_x_intervals(surface, [10], surface.bounds, 0, fraction=0.50) == [
        ((5, 5), (25, 25)),
    ]


def test_relative_inset_does_not_move_a_jet_across_a_hole(tmp_path):
    strips = []
    for center in (-22.5, 22.5):
        strip = trimesh.creation.box(extents=[35, 68, 5])
        strip.apply_translation([center, 0, 0])
        strips.append(strip)
    path = tmp_path / "slotted.stl"
    trimesh.util.concatenate(strips).export(path)
    analysis = analyze_stl(path, density=1200)
    config = measured_config(offset_strategy="relative", offset_inset_fraction=0.20)
    # A through-slot across the complete projected width splits every jet's X interval.
    _, _, impact = impact_for(analysis, config)
    assert impact.active_nozzles == (1, 2, 3)
    for x, _, _ in impact.points_m:
        assert abs(x) > 0.005 + config.jet_radius_m + config.margin_m
    assert all(len(intervals) == 2 for intervals in impact.allowed_x_intervals_m if intervals)


def test_relative_inset_still_rejects_a_surface_smaller_than_the_full_jet(tmp_path):
    path = tmp_path / "too_short.stl"
    trimesh.creation.box(extents=[8, 30, 40]).export(path)
    config = configured(offset_strategy="relative", distance_m=0.0174, diameter_m=0.0008)
    results = AnalyticalModel().evaluate(
        analyze_stl(path, density=1200), pose(), parameters(), config
    )
    assert all(c.impact is None and "Randabstand" in c.message for c in results)


def test_x_centering_uses_reference_nozzle_center_and_ignores_yz_fraction(tmp_path):
    analysis = box_analysis(tmp_path)
    fixed = measured_config(offset_safety_m=0.005)
    _, _, original = impact_for(analysis, fixed, axis="x")
    for fraction in (0, 0.20, 0.50):
        relative = replace(fixed, offset_strategy="relative", offset_inset_fraction=fraction)
        _, _, impact = impact_for(analysis, relative, axis="x")
        assert impact == original
        assert impact.offset_strategy == "fixed" and impact.offset_safety_m == 0.005
        for point, (nozzle, opening) in zip(impact.points_m, impact.opening_ids, strict=True):
            mount = next(
                m for m in relative.mounts if m.array_index == 2 and m.nozzle_index == nozzle
            )
            row_position = (opening - 1 - (relative.opening_count - 1) / 2) * relative.pitch_m
            assert point[0] == pytest.approx(
                0.04 + mount.longitudinal_offset_m + row_position - impact.offset_m
            )


@pytest.mark.parametrize("fraction", [-0.01, 0.500001, float("nan"), float("inf"), True, "0.2"])
def test_invalid_relative_inset_is_rejected(fraction):
    with pytest.raises(ValueError, match="Offseteinrückung"):
        ModelConfiguration(offset_inset_fraction=fraction).validate()


@pytest.mark.parametrize("strategy", [None, True, "unknown"])
def test_invalid_strategy_is_rejected(strategy):
    with pytest.raises(ValueError, match="Offsetstrategie"):
        ModelConfiguration(offset_strategy=strategy).validate()


def test_old_defaults_select_relative_twenty_percent_and_keep_saved_reserve(tmp_path):
    path = tmp_path / "defaults.json"
    raw = {
        "schema_version": 2,
        "model_parameters": {"offset_safety_m": 0.007, "distance_m": 0.0174},
    }
    path.write_text(json.dumps(raw), "utf-8")
    before = path.read_bytes()
    store = ModelDefaultsStore(path)
    config = store.load().configuration
    assert config.offset_strategy == "relative" and config.offset_inset_fraction == 0.20
    assert config.offset_safety_m == 0.007 and config.distance_m == 0.0174
    assert path.read_bytes() == before
    store.save_parameters(
        replace(config, offset_strategy="fixed", offset_inset_fraction=0.30), 1200
    )
    reopened = store.load().configuration
    assert reopened.offset_strategy == "fixed" and reopened.offset_inset_fraction == 0.30


def test_relative_gui_results_transfer_and_invalidation(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    panel = window.autotune_settings.model_panel
    assert panel.offset_strategy.currentData() == "relative"
    inset = panel.fields["offset_inset_fraction"][0]
    assert inset.value() == 20 and inset.isEnabled()
    assert all(row["conveyor_velocity_delay"].isChecked() for row in window.editor.rows)
    path = tmp_path / "operator.stl"
    trimesh.creation.box(extents=[80, 30, 5]).export(path)
    panel.fields["distance_m"][0].setValue(17.4)
    analysis = analyze_stl(path, density=1200)
    select_manual(window, analysis)
    window.editor.speed.setValue(100)
    for row in window.editor.rows:
        row["pulse_duration_ms"].setValue(500)
    before = copy.deepcopy(window.editor.parameters())
    window.calculate_button.click()
    wait(lambda: window.autotune_settings.model_result is not None)
    candidate = window.autotune_results.selected_candidate()
    assert candidate.impact.offset_strategy == "relative"
    details = window.autotune_results.details.toPlainText()
    assert "Einrückung je Seite: 20 %" in details
    assert "Zulässige Auslegungsbereiche X [mm]" in details
    assert "Mitte der Referenzdüse an der Bauteilvorderkante" in details
    assert window.editor.parameters() == before and not services.calls
    # No conversion belongs in transfer: the model already returns the SPS convention.
    proposal = candidate_parameters(before, candidate)
    index = candidate.mount.array_index - 1
    assert proposal["arrays"][index]["offset_mm"] == pytest.approx(
        round(candidate.impact.offset_m * 1000, 1)
    )
    assert proposal["arrays"][index]["conveyor_velocity_delay"] is True
    assert window.import_button.isEnabled()
    window.import_button.click()
    assert window.editor.parameters() == proposal and not services.calls
    generation = window.autotune_settings.model_generation
    inset.setValue(25)
    assert window.autotune_settings.model_result is None and not window.import_button.isEnabled()
    window.autotune_settings.accept_model_result(generation, object(), "")
    assert window.autotune_settings.model_result is None
    panel.save_parameters_button.click()
    reopened = ModelPanel(defaults_path=panel.defaults_store.path)
    assert reopened.configuration().offset_inset_fraction == 0.25
    reopened.deleteLater()
    panel.offset_strategy.setCurrentIndex(panel.offset_strategy.findData("fixed"))
    assert not inset.isEnabled()
    assert window.editor.parameters() == proposal and not services.calls and not errors
