import copy
import json
import math
import os
import threading
from pathlib import Path

import pytest
import trimesh
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QFileDialog
from test_analytical_model import pose
from test_geometry import write_box
from test_gui import offline_window as offline_window


def prepare(window, wait, tmp_path, axis="y", angle=90):
    dialog = window.autotune_settings
    dialog.set_mesh(write_box(tmp_path / "box.stl"))
    dialog.density.setText("2700")
    wait(lambda: dialog.analysis is not None)
    dialog.target.setText(", ".join(str(v) for v in pose(axis, angle)["target_quaternion_xyzw"]))
    dialog.confirm_manual()
    panel = dialog.model_panel
    panel.fields["spread_angle_deg"][0].setValue(0)
    panel.fields["uphill_deg"][0].setValue(0)
    panel.fields["cross_slope_deg"][0].setValue(0)
    for mount, _, _, center in panel.mount_rows:
        center.setValue(15 if mount.normal_axis == "z" else 20)
    for row in window.editor.rows:
        row["pulse_duration_ms"].setValue(500)
    return dialog, panel


def test_calculation_configuration_and_no_writes(offline_window, tmp_path, monkeypatch):
    window, services, errors, wait = offline_window
    dialog, panel = prepare(window, wait, tmp_path)
    before = copy.deepcopy(window.editor.parameters())
    window.calculate_autotune()
    wait(lambda: dialog.model_result is not None)
    result = dialog.model_result
    assert [(c.mount.array_index, c.mount.nozzle_index) for c in result.candidates] == [
        (2, 1),
        (4, 1),
    ]
    assert all(c.pressure is not None for c in result.candidates)
    assert all(c.status == "ready" for c in result.candidates)
    assert window.autotune_results.table.rowCount() == 2
    assert window.autotune_results.table.item(0, 5).text() != "—"
    assert "Kippkante" in window.autotune_results.details.toPlainText()
    assert "Schwerpunkt" in window.autotune_results.details.toPlainText()
    assert "np.float" not in window.autotune_results.details.toPlainText()
    assert "1.0, 1.0" in window.autotune_results.details.toPlainText()
    assert window.editor.parameters() == before
    assert not window.variants
    assert not services.calls
    if os.environ.get("BIBAZU_GUI_ARTIFACTS"):
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
        window.show()
        QApplication.processEvents()
        destination = Path(os.environ["BIBAZU_GUI_ARTIFACTS"])
        destination.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(destination / "analytical.png"))
    old_generation = dialog.model_generation
    window.editor.rows[1]["pulse_duration_ms"].setValue(250)
    assert dialog.model_result is None
    dialog.accept_model_result(old_generation, result, "")
    assert dialog.model_result is None
    assert window.autotune_results.table.item(0, 5).text() == "—"
    window.add_variant()
    path = tmp_path / "recipe.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    window.save_recipe()
    saved = json.loads(path.read_text("utf-8"))
    assert saved["trial_context"] == window.context
    assert not {"configuration", "candidates", "mounts"} & saved.keys()
    assert "drag_coefficient" not in path.read_text("utf-8")
    assert not errors


def test_linked_width_and_pitch_and_missing_mount(offline_window, tmp_path):
    window, _, _, wait = offline_window
    dialog, panel = prepare(window, wait, tmp_path)
    panel.outer_width.setValue(28.7)
    assert abs(panel.configuration().pitch_m - 0.004) < 1e-9
    panel.fields["pitch_m"][0].setValue(3)
    assert abs(panel.outer_width.value() - 21.7) < 1e-6
    for _, _, _, center in panel.mount_rows:
        center.setValue(-0.0001)
    window.calculate_autotune()
    assert all(c.status == "blocked" for c in window.autotune_results.results)
    assert "Montageposition" in window.autotune_results.table.item(0, 7).text()
    assert dialog.model_result is None


def test_seven_hits_are_counted_and_explained_in_gui(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    mesh = trimesh.creation.box(extents=[80, 20, 20])
    mesh.apply_transform(trimesh.transformations.rotation_matrix(math.radians(0.004), [0, 0, 1]))
    path = tmp_path / "near_edge.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    dialog = window.autotune_settings
    dialog.set_mesh(path)
    dialog.density.setText("1000")
    wait(lambda: dialog.analysis is not None)
    dialog.target.setText(", ".join(str(v) for v in pose("y", -90)["target_quaternion_xyzw"]))
    dialog.confirm_manual()
    panel = dialog.model_panel
    panel.fields["distance_m"][0].setValue(17.4)
    panel.fields["contact_tolerance_m"][0].setValue(0.05)
    for mount, _, _, center in panel.mount_rows:
        if mount.array_index in (2, 4) and mount.nozzle_index == 1:
            center.setValue(12)
    before = window.editor.parameters()
    window.calculate_autotune()
    wait(lambda: dialog.model_result is not None)
    assert window.autotune_results.table.item(0, 3).text() == "6"
    assert window.autotune_results.results[0].impact.weights == (0.5,) + (1.0,) * 5 + (0.5, 0.0)
    assert "0 / 0,5 / 1" in window.autotune_results.details.toPlainText()
    assert "Flächenüberdeckung:" in window.autotune_results.details.toPlainText()
    assert "Öffnungen mit positivem Gewicht: 7" in window.autotune_results.details.toPlainText()
    assert "Vollständig abgedeckt" in window.autotune_results.details.toPlainText()
    assert window.editor.parameters() == before
    assert not services.calls
    assert not errors
    if os.environ.get("BIBAZU_GUI_ARTIFACTS"):
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
        window.show()
        QApplication.processEvents()
        destination = Path(os.environ["BIBAZU_GUI_ARTIFACTS"])
        destination.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(destination / "seven-hits.png"))


def test_manual_ambiguity_requires_explicit_axis(offline_window, tmp_path):
    window, _, _, wait = offline_window
    dialog, panel = prepare(window, wait, tmp_path, angle=180)
    window.calculate_autotune()
    assert "mehrdeutig" in window.autotune_results.details.toPlainText()
    panel.manual_axis.setCurrentText("Y")
    panel.manual_angle.setValue(180)
    window.calculate_autotune()
    wait(lambda: dialog.model_result is not None)
    assert len(window.autotune_results.results) == 2


def test_half_hit_sum_is_not_truncated_and_threshold_edits_invalidate(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    dialog, panel = prepare(window, wait, tmp_path)
    for mount, _, _, center in panel.mount_rows:
        if mount.normal_axis == "z":
            center.setValue(11.65)
    window.calculate_button.click()
    wait(lambda: dialog.model_result is not None)
    impact = dialog.model_result.candidates[0].impact
    assert impact.weights == (0.5,) + (1.0,) * 7
    assert window.autotune_results.table.item(0, 3).text() == "7.5"
    assert "Flächenüberdeckung: 50.00%" in window.autotune_results.details.toPlainText()
    assert impact.area_eff_m2 == pytest.approx(7.5 * math.pi * impact.jet_radius_m**2)
    panel.fields["half_hit_lower"][0].setValue(40)
    assert dialog.model_result is None
    assert not window.import_button.isEnabled()
    assert panel.configuration().half_hit_lower == 0.4
    assert panel.configuration().half_hit_upper == 0.75
    assert not services.calls and not errors


def test_model_worker_discards_stale_result_and_leaves_network_available(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    dialog, panel = prepare(window, wait, tmp_path)
    started, release = threading.Event(), threading.Event()
    delivered = []
    actual = dialog.pipeline.prepare

    def delayed(*args, **kwargs):
        if kwargs.get("calculate", True):
            started.set()
            assert release.wait(5)
        return actual(*args, **kwargs)

    monkeypatch.setattr(dialog.pipeline, "prepare", delayed)
    dialog.model_worker.completed.connect(lambda *args: delivered.append(args))
    try:
        window.calculate_autotune()
        wait(started.is_set)
        assert not window.calculate_button.isEnabled()
        assert not window.import_button.isEnabled()
        window.open_autotune_settings()
        assert dialog.calculation_running
        panel.fields["drag_coefficient"][0].setValue(1.2)
        assert window.calculate_button.isEnabled()
        window.worker.submit("connect")
        wait(lambda: window.state == "ready")
        assert dialog.model_result is None
        window.calculate_autotune()
        assert not window.calculate_button.isEnabled()
        assert not any(method == "PUT" for method, _, _ in services.calls)
    finally:
        release.set()
    wait(lambda: dialog.model_result is not None)
    assert len(delivered) == 2
    assert window.calculate_button.isEnabled()
    assert not errors
