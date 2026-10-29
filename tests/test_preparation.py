import json
import os
import threading
from pathlib import Path

from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QFileDialog
from test_engine import parameters
from test_geometry import write_box
from test_gui import offline_window as offline_window

from bibazu_autotune import preparation
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.roadmap_transition_dialog import (
    SelectedRoadmapTransition,
    load_roadmap_document,
    roadmap_transition_metadata,
)
from bibazu_autotune.trial_context import manual_context


def test_offline_manual_preparation_and_recipe_roundtrip(offline_window, tmp_path, monkeypatch):
    window, services, errors, wait = offline_window
    dialog = window.autotune_settings
    window.open_autotune_settings()
    assert dialog.isVisible()
    assert not services.calls
    dialog.confirm_manual()
    assert window.context is None
    assert "STL" in dialog.pose_status.text()
    path = write_box(tmp_path / "training.stl")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    dialog.choose_stl()
    wait(lambda: dialog.analysis is not None)
    assert not dialog.source_preview.pixmap().isNull()
    assert not dialog.target_preview.pixmap().isNull()
    assert window.context is None
    dialog.source.setText("0, 0, 0, 2")
    dialog.target.setText("0, 1, 0, 1")
    dialog.density.setText("2700")
    wait(lambda: dialog.analysis is not None)
    window.editor.set_parameters(parameters())
    dialog.confirm_manual()
    assert window.context["kind"] == "manual"
    assert window.context["source_quaternion_xyzw"] == [0, 0, 0, 1]
    assert not window.source_preview.pixmap().isNull()
    dialog.refresh_status()
    status = dialog.details.toPlainText()
    assert "automatische Schätzung noch nicht implementiert" in status
    assert "{1: 50, 2: 50, 3: 50, 4: 50}" in status
    assert "Analytisches Druckmodell:" in status
    assert "Masse [g]: 64.8" in status
    if os.environ.get("BIBAZU_GUI_ARTIFACTS"):
        # Offscreen Windows sessions can lack a usable default font.
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
            dialog.update_previews()
        QApplication.processEvents()
        destination = Path(os.environ["BIBAZU_GUI_ARTIFACTS"])
        destination.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(destination / "preparation.png"))
    assert not window.variants
    assert not any(method == "PUT" for method, _, _ in services.calls)
    window.add_variant()
    recipe_path = tmp_path / "recipe.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(recipe_path), ""))
    window.save_recipe()
    record = json.loads(recipe_path.read_text("utf-8"))
    assert record["trial_context"] == window.context
    assert "roadmap_transition" not in record
    assert "density_kg_m3" not in recipe_path.read_text("utf-8")
    saved = window.context
    window.set_context(None)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(recipe_path), ""))
    window.load_recipe()
    assert window.context == saved
    dialog.target.setText("0, 0, 0, 0")
    dialog.pose_edited()
    assert window.context is None
    dialog.confirm_manual()
    assert "Nullvektor" in dialog.pose_status.text()
    assert not errors


def test_loading_manual_context_invalidates_pending_import(offline_window, tmp_path, monkeypatch):
    window, services, errors, wait = offline_window
    dialog = window.autotune_settings
    first = write_box(tmp_path / "old.stl")
    second = analyze_stl(write_box(tmp_path / "new.stl", translation=(100, 0, 0)))
    started, release = threading.Event(), threading.Event()
    actual = preparation.analyze_stl
    delivered = []
    dialog.geometry_worker.completed.connect(lambda *args: delivered.append(args))

    def delayed(path, unit, density):
        started.set()
        assert release.wait(5)
        return actual(path, unit, density)

    monkeypatch.setattr(preparation, "analyze_stl", delayed)
    try:
        dialog.set_mesh(first)
        wait(started.is_set)
        context = manual_context("New", second.sha256, [0, 0, 0, 1], [0, 1, 0, 1])
        window.set_context(context)
        assert dialog.path is None
        assert dialog.analysis is None
    finally:
        release.set()
    wait(lambda: bool(delivered))
    # Deliver the old worker result explicitly; the generation must reject it.
    dialog.accept_analysis(dialog.generation - 1, actual(first), "")
    assert window.context == context
    assert dialog.analysis is None
    assert not services.calls
    assert not errors


def select_roadmap(window, path, part_name, mesh):
    payload = json.loads((Path(__file__).parents[1] / "examples/roadmap.json").read_text("utf-8"))
    payload.update(part_name=part_name, source=str(mesh))
    for node in payload["nodes"]:
        node["representative_quaternion_xyzw"] = [0, 0, 0, 1]
    path.write_text(json.dumps(payload), "utf-8")
    document = load_roadmap_document(path)
    selection = SelectedRoadmapTransition(
        document.path,
        document.part_name,
        document.transitions[0],
        document.pose(5),
        document.pose(0),
    )
    window.set_context(roadmap_transition_metadata(selection), selection)


def test_roadmap_mesh_missing_replacement_and_part_switch(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    dialog = window.autotune_settings
    mesh = write_box(tmp_path / "box.stl")
    select_roadmap(window, tmp_path / "a.json", "Teil A", mesh)
    wait(lambda: dialog.analysis is not None)
    assert not dialog.manual.isChecked()
    assert not dialog.source.isEnabled()
    assert dialog.analysis.path == mesh
    dialog.density.setText("7800")
    wait(lambda: dialog.analysis is not None)
    previous_generation = dialog.generation
    select_roadmap(window, tmp_path / "b.json", "Teil B", tmp_path / "missing.stl")
    assert dialog.analysis is None
    assert dialog.density.text() == "7800"
    assert dialog.generation > previous_generation
    wait(lambda: "Hintergrund" not in dialog.geometry_error)
    assert "missing.stl" in dialog.details.toPlainText()
    assert window.context["part_name"] == "Teil B"
    dialog.set_mesh(mesh)
    wait(lambda: dialog.analysis is not None)
    assert window.context["part_name"] == "Teil B"
    assert not dialog.manual.isChecked()
    dialog.manual.setChecked(True)
    assert window.context is None
    dialog.confirm_manual()
    assert window.context["kind"] == "manual"
    assert not any(method == "PUT" for method, _, _ in services.calls)
    assert not errors


def test_stale_import_results_are_discarded_and_do_not_block_network(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    dialog = window.autotune_settings
    first = write_box(tmp_path / "first.stl")
    second = write_box(tmp_path / "second.stl", translation=(10, 20, 30))
    stale = analyze_stl(first)
    started, release = threading.Event(), threading.Event()
    actual = preparation.analyze_stl

    def delayed(path, unit, density):
        if path == first:
            started.set()
            assert release.wait(5)
        return actual(path, unit, density)

    monkeypatch.setattr(preparation, "analyze_stl", delayed)
    try:
        dialog.set_mesh(first)
        wait(started.is_set)
        generation = dialog.generation
        dialog.set_mesh(second)
        dialog.accept_analysis(generation, stale, "")
        assert dialog.analysis is None
        window.worker.submit("connect")
        wait(lambda: window.state == "ready")
        assert dialog.analysis is None
        assert not any(method == "PUT" for method, _, _ in services.calls)
    finally:
        release.set()
    wait(lambda: dialog.analysis is not None)
    assert dialog.analysis.path == second
    assert dialog.analysis.sha256 != stale.sha256
    assert not errors


def test_invalid_density_unit_changes_and_manual_gui_trial(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    dialog = window.autotune_settings
    dialog.set_mesh(write_box(tmp_path / "box.stl"))
    wait(lambda: dialog.analysis is not None)
    dialog.unit.setCurrentText("cm")
    wait(lambda: dialog.analysis is not None)
    assert dialog.analysis.geometry.dimensions_m[0] == 0.2
    dialog.density.setText("nan")
    wait(lambda: "Dichte muss" in dialog.geometry_error)
    assert dialog.analysis is None
    dialog.confirm_manual()
    assert window.context is None
    dialog.density.setText("1000")
    wait(lambda: dialog.analysis is not None)
    dialog.confirm_manual()
    window.worker.submit("connect")
    wait(lambda: window.state == "ready")
    assert window.start_button.isEnabled()
    window.start_trial()
    wait(lambda: window.state == "waiting")
    assert not dialog.isEnabled()
    assert not window.prepare_button.isEnabled()
    trial_id = next(iter(services.trials))
    assert services.trials[trial_id]["context"]["trial_context"]["kind"] == "manual"
    services.trials[trial_id].update(status="completed", measurements=[])
    wait(lambda: window.state == "ready" and bool(window.results))
    assert dialog.isEnabled()
    assert not errors
