import copy
import json
import math
import os
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication
from test_analytical_model import pose
from test_engine import parameters
from test_gui import offline_window as offline_window

from bibazu_autotune.analytical_model import (
    AnalyticalModel,
    AnalyticalPressureModel,
    AutomaticOffsetEstimator,
    ModelConfiguration,
    Turn,
    drag_force,
    orient_body,
    tipping_mechanics,
)
from bibazu_autotune.autotune import AutotunePipeline
from bibazu_autotune.candidate_transfer import candidate_parameters
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.model_defaults import ModelDefaultsStore


def measured_config(**values):
    config = ModelConfiguration(
        uphill_deg=0, cross_slope_deg=0, spread_angle_deg=0, **{"offset_safety_m": 0, **values}
    )
    return replace(
        config,
        mounts=tuple(
            replace(
                m,
                row_center_m=0.012 + 0.024 * (m.nozzle_index - 1) if m.nozzle_index <= 3 else 0.012,
                longitudinal_offset_m=0.024 * (m.nozzle_index - 4) if m.nozzle_index >= 4 else 0,
            )
            for m in config.mounts
        ),
    )


def box_analysis(tmp_path, width=20, length=80):
    path = tmp_path / "flat.stl"
    path.write_bytes(
        trimesh.exchange.stl.export_stl(trimesh.creation.box(extents=[length, width, 5]))
    )
    return analyze_stl(path, density=1200)


def impact_for(analysis, config, axis="y", angle=-90, array=2):
    body = orient_body(analysis, pose(axis, angle))
    reference = next(
        m
        for m in config.mounts
        if m.array_index == array and m.nozzle_index == (4 if axis == "x" else 1)
    )
    turn = Turn(axis, angle)
    mechanics = tipping_mechanics(body, turn, reference, config)
    impact = AutomaticOffsetEstimator().estimate(body, turn, reference, mechanics, config)
    return body, mechanics, impact


@pytest.mark.parametrize("width,active", [(20, (1,)), (44, (1, 2)), (68, (1, 2, 3))])
@pytest.mark.parametrize("angle", [-90, 90])
def test_flat_part_selects_only_needed_nozzles(tmp_path, width, active, angle):
    analysis = box_analysis(tmp_path, width)
    config = measured_config()
    body, mechanics, impact = impact_for(analysis, config, angle=angle)
    assert impact.active_nozzles == active
    assert len(impact.points_m) == len(impact.weights) == len(impact.opening_ids) == 24
    assert impact.opening_ids == tuple((n, i) for n in (1, 2, 3) for i in range(1, 9))
    for nozzle in (1, 2, 3):
        indices = [i for i, (n, _) in enumerate(impact.opening_ids) if n == nozzle]
        assert any(impact.full_weights[i] for i in indices) == (nozzle in active)
        if nozzle not in active:
            assert all(impact.weights[i] == 0 for i in indices)
    expected_area = sum(impact.weights) * math.pi * config.jet_radius_m**2
    assert impact.area_eff_m2 == pytest.approx(expected_area)
    assert impact.lever_arm_m == pytest.approx(
        0.08 - config.jet_radius_m - config.margin_m, abs=1e-7
    )
    pressure = AnalyticalPressureModel().predict(body, mechanics, impact, 500, config)
    assert drag_force(pressure.gauge_pa, expected_area, config) == pytest.approx(pressure.force_n)


def test_partial_only_reference_stays_off_and_second_nozzle_works(tmp_path):
    # The narrow lower strip gives a partial hit near the trough edge, but no full
    # circle including margin fits there. Only the broad second strip can enable a nozzle.
    strips = []
    first_opening_mm = (0.012 - ModelConfiguration().center_span_m / 2) * 1000
    for width, center in [(0.25, 0.125), (0.5, first_opening_mm), (20, 34)]:
        mesh = trimesh.creation.box(extents=[80, width, 5])
        mesh.apply_translation([0, center, 0])
        strips.append(mesh)
    path = tmp_path / "separated_faces.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(trimesh.util.concatenate(strips)))
    analysis = analyze_stl(path, density=1200)
    _, _, impact = impact_for(analysis, measured_config())
    assert impact.active_nozzles == (2,)
    assert any(impact.geometric_weights[:8])
    assert not any(impact.full_weights[:8])
    assert not any(impact.weights[:8])
    assert any(impact.weights[8:16])
    assert not any(impact.weights[16:])
    # A width/bounding-box rule would wrongly enable the first nozzle too.
    assert analysis.geometry.dimensions_m[1] == pytest.approx(0.044)


@pytest.mark.parametrize("shift", [-0.024, 0.024, 0.048])
def test_x_offset_keeps_reference_when_only_nozzle_five_hits(tmp_path, shift):
    config = measured_config()
    config = replace(
        config,
        mounts=tuple(
            replace(
                m,
                row_center_m=0.012 if m.nozzle_index == 5 else 0.05,
                longitudinal_offset_m=shift if m.nozzle_index == 5 else m.longitudinal_offset_m,
            )
            if m.array_index == 2 and m.nozzle_index >= 4
            else m
            for m in config.mounts
        ),
    )
    body, _, impact = impact_for(box_analysis(tmp_path), config, axis="x")
    assert impact.active_nozzles == (5,)
    first_base = 0.04 + config.jet_radius_m + shift
    clearance = config.jet_radius_m + config.margin_m
    lower = max(0, first_base + config.center_span_m - (body.vertices[:, 0].max() - clearance))
    upper = first_base - (body.vertices[:, 0].min() + clearance)
    expected_offset = (lower + upper) / 2
    assert impact.offset_m == pytest.approx(expected_offset, abs=1e-7)
    assert np.mean(np.asarray(impact.points_m)[8:16, 0]) == pytest.approx(
        first_base + config.center_span_m / 2 - expected_offset, abs=1e-7
    )
    result = AnalyticalModel().evaluate(
        box_analysis(tmp_path), pose("x", -90), parameters(), config
    )
    candidate = next(c for c in result if c.key == (2, 4))
    copied = candidate_parameters(parameters(), candidate)
    assert copied["arrays"][1]["nozzles_enabled"] == [False, False, False, False, True, False]


def test_x_combined_area_and_distinct_levers_against_independent_moment(tmp_path):
    config = measured_config()
    centers = {4: 0.004, 5: 0.010, 6: 0.016}
    config = replace(
        config,
        mounts=tuple(
            replace(m, row_center_m=centers[m.nozzle_index])
            if m.array_index == 2 and m.nozzle_index >= 4
            else m
            for m in config.mounts
        ),
    )
    body, mechanics, impact = impact_for(box_analysis(tmp_path, length=120), config, axis="x")
    assert impact.active_nozzles == (4, 5, 6)
    assert impact.weights == (1.0,) * 24
    assert impact.lever_arms_m == pytest.approx((0.016,) * 8 + (0.010,) * 8 + (0.004,) * 8)
    assert impact.area_eff_m2 == pytest.approx(24 * math.pi * config.jet_radius_m**2)
    assert impact.lever_arm_m == pytest.approx(0.010)
    pressure = AnalyticalPressureModel().predict(body, mechanics, impact, 500, config)
    expected_force = math.sqrt(
        2 * mechanics.inertia_axis_kg_m2 * body.mass_kg * 9.81 * mechanics.delta_h_m
    ) / (0.5 * 0.010)
    assert pressure.force_n == pytest.approx(expected_force)
    assert drag_force(pressure.gauge_pa, impact.area_eff_m2, config) == pytest.approx(
        expected_force
    )


@pytest.mark.parametrize(
    "change,axis,word",
    [
        ("center", "y", "Reihenmitte"),
        ("offset", "x", "Längsversatz"),
        ("axes", "y", "übereinstimmen"),
    ],
)
def test_incomplete_group_blocks_only_affected_array(tmp_path, change, axis, word):
    config = measured_config()
    index = 5 if change == "offset" else 2
    updates = {
        "center": {"row_center_m": None},
        "offset": {"longitudinal_offset_m": None},
        "axes": {"rotation_axis": "z", "normal_axis": "y"},
    }[change]
    config = replace(
        config,
        mounts=tuple(
            replace(m, **updates) if (m.array_index, m.nozzle_index) == (2, index) else m
            for m in config.mounts
        ),
    )
    result = AnalyticalModel().evaluate(
        box_analysis(tmp_path), pose(axis, -90), parameters(), config
    )
    blocked = next(c for c in result if c.mount.array_index == 2)
    assert blocked.status == "blocked" and word in blocked.message
    assert next(c for c in result if c.mount.array_index == 4).pressure is not None


def test_results_keyed_by_array_and_group_without_overwrite(tmp_path):
    config = measured_config()
    config = replace(
        config,
        mounts=tuple(
            replace(m, rotation_axis="x", normal_axis="z")
            if m.array_index == 2 and m.nozzle_index <= 3
            else m
            for m in config.mounts
        ),
    )
    analysis = box_analysis(tmp_path)
    result = AutotunePipeline().prepare(
        analysis, pose("x", -90, analysis.sha256), parameters(), config
    )
    available = [c for c in result.candidates if c.pressure is not None]
    assert {(2, 1), (2, 4)} <= result.pressure.value.keys()
    assert len(result.pressure.value) == len(available)
    assert len(result.offset.value) == len(available)


def test_legacy_defaults_migrate_only_on_explicit_save(tmp_path):
    path = tmp_path / "defaults.json"
    config = measured_config()
    legacy = [asdict(m) for m in config.mounts if m.nozzle_index in (1, 4)]
    for mount in legacy:
        mount.pop("longitudinal_offset_m")
    raw = {"schema_version": 1, "mounts": legacy, "model_parameters": {"density_kg_m3": 1250}}
    path.write_text(json.dumps(raw), "utf-8")
    before = path.read_bytes()
    store = ModelDefaultsStore(path)
    loaded = store.load()
    assert path.read_bytes() == before
    assert len(loaded.configuration.mounts) == 24
    assert loaded.density_kg_m3 == 1250
    for mount in loaded.configuration.mounts:
        if mount.nozzle_index in (1, 4):
            assert mount.row_center_m == 0.012
            assert mount.longitudinal_offset_m == 0
        else:
            measured = next(
                m
                for m in ModelConfiguration().mounts
                if (m.array_index, m.nozzle_index) == (mount.array_index, mount.nozzle_index)
            )
            assert mount == measured
    store.save_parameters(loaded.configuration, loaded.density_kg_m3)
    assert json.loads(path.read_text("utf-8"))["schema_version"] == 2
    assert store.load() == loaded
    signed = replace(
        config,
        mounts=tuple(
            replace(m, longitudinal_offset_m=-0.024) if m.nozzle_index == 5 else m
            for m in config.mounts
        ),
    )
    store.save_mounts(signed.mounts)
    assert store.load().configuration.mounts == signed.mounts


@pytest.mark.parametrize("width,active", [(20, (1,)), (44, (1, 2)), (68, (1, 2, 3))])
def test_gui_auto_selection_and_atomic_local_import(offline_window, tmp_path, width, active):
    window, services, errors, wait = offline_window
    analysis = box_analysis(tmp_path, width)
    settings, panel = window.autotune_settings, window.autotune_settings.model_panel
    settings.set_mesh(analysis.path)
    wait(lambda: settings.analysis is not None)
    settings.target.setText(", ".join(map(str, pose("y", -90)["target_quaternion_xyzw"])))
    settings.confirm_manual()
    for name in ("uphill_deg", "cross_slope_deg", "spread_angle_deg"):
        panel.fields[name][0].setValue(0)
    for mount, _, _, center in panel.mount_rows:
        center.setValue(12 + 24 * (mount.nozzle_index - 1) if mount.nozzle_index <= 3 else 12)
        if mount.nozzle_index >= 5:
            panel.longitudinal_fields[(mount.array_index, mount.nozzle_index)].setText(
                str(24 * (mount.nozzle_index - 4))
            )
    window.editor.speed.setValue(80)
    for row in window.editor.rows:
        row["pulse_duration_ms"].setValue(500)
    before = copy.deepcopy(window.editor.parameters())
    assert panel.mount_table.rowCount() == 24
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    selected = window.autotune_results.selected_candidate()
    assert selected.impact.active_nozzles == active
    label = "Düse 1" if len(active) == 1 else "Düsen " + "+".join(map(str, active))
    assert label in window.autotune_results.table.item(0, 0).text()
    assert "Volltreffer" in window.autotune_results.details.toPlainText()
    expected = candidate_parameters(before, selected)
    window.import_button.click()
    assert window.editor.parameters() == expected
    assert expected["arrays"][1]["nozzles_enabled"] == [n in active for n in range(1, 7)]
    for i in (0, 2, 3):
        assert expected["arrays"][i] == before["arrays"][i]
    assert not services.calls and not errors
    if os.environ.get("BIBAZU_GUI_ARTIFACTS"):
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
        window.pages.setCurrentWidget(window.main_page)
        destination = Path(os.environ["BIBAZU_GUI_ARTIFACTS"])
        destination.mkdir(parents=True, exist_ok=True)
        QApplication.processEvents()
        assert window.grab().save(str(destination / f"nozzles-width-{width}.png"))
    next(
        center
        for mount, _, _, center in panel.mount_rows
        if (mount.array_index, mount.nozzle_index) == (2, 2)
    ).setValue(-0.0001)
    assert settings.model_result is None and not window.import_button.isEnabled()
    assert "Düse 2" in window.autotune_results.table.item(0, 8).text()


def test_x_mount_input_accepts_signed_values_and_invalidates_results(offline_window, tmp_path):
    window, _, _, wait = offline_window
    settings = window.autotune_settings
    settings.set_mesh(box_analysis(tmp_path).path)
    wait(lambda: settings.analysis is not None)
    settings.target.setText(", ".join(map(str, pose("x", -90)["target_quaternion_xyzw"])))
    settings.confirm_manual()
    panel = settings.model_panel
    for name in ("uphill_deg", "cross_slope_deg", "spread_angle_deg"):
        panel.fields[name][0].setValue(0)
    for m, _, _, center in panel.mount_rows:
        center.setValue(12)
        if m.nozzle_index >= 5:
            panel.longitudinal_fields[(m.array_index, m.nozzle_index)].setText(
                str(24 * (m.nozzle_index - 4))
            )
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    field = panel.longitudinal_fields[(2, 5)]
    field.setText("-24,5")
    assert settings.model_result is None
    assert not window.import_button.isEnabled()
    measured = next(
        m for m in panel.configuration().mounts if (m.array_index, m.nozzle_index) == (2, 5)
    )
    assert measured.longitudinal_offset_m == -0.0245
    field.clear()
    blocked = next(c for c in settings.preview_result.candidates if c.key == (2, 4))
    assert blocked.status == "blocked" and "Längsversatz" in blocked.message
    assert panel.longitudinal_fields[(2, 4)].isReadOnly()


@pytest.mark.parametrize("active", [(), (1, 1), (4,), (0,), (True,)])
def test_bad_automatic_nozzle_selection_cannot_be_imported(active):
    from test_candidate_transfer import candidate

    value = candidate()
    value = replace(value, impact=replace(value.impact, active_nozzles=active))
    original = parameters()
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="Düsenwahl"):
        candidate_parameters(original, value)
    assert original == before


@pytest.mark.parametrize("width,active", [(20, (1,)), (44, (1, 2)), (80, (1, 2, 3))])
@pytest.mark.parametrize("mesh_angle_deg", [-0.028, -0.004, 0.004, 0.028])
def test_nearly_aligned_flat_part_uses_all_supported_nozzles(
    tmp_path, width, active, mesh_angle_deg
):
    # Roadmap/STL orientations are not always exactly axis-aligned. A microscopic
    # increase in lever arm must not leave the other usable rows switched off.
    mesh = trimesh.creation.box(extents=[80, width, 10])
    mesh.apply_transform(
        trimesh.transformations.rotation_matrix(math.radians(mesh_angle_deg), [0, 0, 1])
    )
    path = tmp_path / "near_aligned_flat.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    analysis = analyze_stl(path, density=1200)
    config = replace(ModelConfiguration(), distance_m=0.0174, contact_tolerance_m=0.00005)
    result = AnalyticalModel().evaluate(analysis, pose("y", -179.972), parameters(), config)
    assert len(result) == 2
    for candidate in result:
        assert candidate.pressure is not None, candidate.message
        assert candidate.impact.active_nozzles == active
        for nozzle in active:
            assert any(
                full
                for (n, _), full in zip(
                    candidate.impact.opening_ids, candidate.impact.full_weights, strict=True
                )
                if n == nozzle
            )


@pytest.mark.parametrize("width,active", [(20, (1,)), (80, (1, 2, 3))])
def test_gui_near_aligned_selection_and_hidden_nozzles(offline_window, tmp_path, width, active):
    window, services, errors, wait = offline_window
    mesh = trimesh.creation.box(extents=[80, width, 10])
    mesh.apply_transform(trimesh.transformations.rotation_matrix(math.radians(0.028), [0, 0, 1]))
    path = tmp_path / "flat_roadmap_orientation.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    settings = window.autotune_settings
    settings.set_mesh(path)
    wait(lambda: settings.analysis is not None)
    settings.target.setText(", ".join(map(str, pose("y", -179.972)["target_quaternion_xyzw"])))
    settings.confirm_manual()
    settings.model_panel.fields["distance_m"][0].setValue(17.4)
    settings.model_panel.fields["contact_tolerance_m"][0].setValue(0.05)
    window.editor.speed.setValue(100)
    for row in window.editor.rows:
        row["pulse_duration_ms"].setValue(50)
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    selected = window.autotune_results.selected_candidate()
    impact = selected.impact
    preview = window.autotune_results.preview
    assert impact.active_nozzles == active
    visible = preview.visible_opening_indices()
    assert len(visible) == 8 * len(active)
    assert {impact.opening_ids[i][0] for i in visible} == set(active)
    assert len(impact.points_m) == 24  # Diagnostic results retain the unpowered rows.
    QApplication.processEvents()
    original_image = preview.grab().toImage()
    if len(active) == 1:
        # Even extreme coordinates in an inactive row must not affect rendering or scale.
        points = tuple(
            p if i in visible else (p[0] + 1000, p[1] + 1000, p[2])
            for i, p in enumerate(impact.points_m)
        )
        preview.candidate = replace(selected, impact=replace(impact, points_m=points))
        preview.update()
        QApplication.processEvents()
        assert preview.grab().toImage() == original_image
        preview.candidate = selected
    assert window.import_button.isEnabled()
    window.import_button.click()
    assert window.editor.parameters()["arrays"][1]["nozzles_enabled"] == [
        n in active for n in range(1, 7)
    ]
    assert not services.calls and not errors
    if os.environ.get("BIBAZU_GUI_ARTIFACTS"):
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
        destination = Path(os.environ["BIBAZU_GUI_ARTIFACTS"])
        destination.mkdir(parents=True, exist_ok=True)
        window.pages.setCurrentWidget(window.main_page)
        QApplication.processEvents()
        assert window.grab().save(str(destination / f"corrected-flat-{width}.png"))
