import json
import threading
from pathlib import Path

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog, QFileDialog
from test_engine import parameters
from test_geometry import write_box
from test_gui import offline_window as offline_window
from test_model_gui import prepare

from bibazu_autotune import app, preparation
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.pose_dialog import PartPoseDialog
from bibazu_autotune.profiles import profile_parameters
from bibazu_autotune.roadmap_transition_dialog import (
    SelectedRoadmapTransition,
    load_roadmap_document,
    roadmap_transition_metadata,
)
from bibazu_autotune.trial_context import manual_context


def run_pose_dialog(dialog, *, cancel=False):
    timer = QTimer(dialog)

    def finish():
        if dialog.analysis is not None:
            timer.stop()
            if not dialog.roadmap:
                dialog.direction.setCurrentIndex(2)
            dialog.reject() if cancel else dialog.accept_button.click()

    timer.timeout.connect(finish)
    timer.start(10)
    timeout = QTimer(dialog)
    timeout.setSingleShot(True)
    timeout.timeout.connect(dialog.reject)
    timeout.start(5000)
    return QDialog.exec(dialog)


def test_main_stl_load_confirm_and_single_parameter_roundtrip(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    path = write_box(tmp_path / "training.stl")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(PartPoseDialog, "exec", run_pose_dialog)
    window.stl_button.click()
    settings = window.autotune_settings
    assert window.context["kind"] == "manual"
    assert window.context["rotation_axis"] == "y" and window.context["signed_angle_deg"] == -90
    assert settings.path == path and settings.analysis is not None
    assert not window.source_preview.pixmap().isNull()
    assert not window.target_preview.pixmap().isNull()
    window.editor.set_parameters(parameters())
    saved_context = window.context
    profile_path = tmp_path / "parameters.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(profile_path), ""))
    window.save_parameters()
    record = json.loads(profile_path.read_text("utf-8"))
    assert profile_parameters(record) == parameters()
    assert record["trial_context"] == saved_context
    assert not {"variants", "density", "geometry", "configuration"} & record.keys()
    window.set_context(None)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(profile_path), ""))
    window.load_parameters()
    assert window.context == saved_context and settings.path is None
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    window.replace_stl()
    assert settings.analysis is not None and window.context == saved_context
    assert not services.calls and not errors


def test_file_and_pose_cancellation_keep_previous_part_and_results(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    settings, _ = prepare(window, wait, tmp_path)
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    previous = settings.selection()
    result, generation = settings.model_result, settings.model_generation
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    window.load_stl()
    assert settings.selection() == previous
    other = write_box(tmp_path / "other.stl", translation=(10, 20, 30))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(other), ""))
    monkeypatch.setattr(PartPoseDialog, "exec", lambda d: run_pose_dialog(d, cancel=True))
    window.load_stl()
    assert settings.selection() == previous
    assert settings.model_result is result and settings.model_generation == generation
    assert not services.calls and not errors


def test_roadmap_missing_stl_and_replacement_keep_roadmap_poses(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    settings = window.autotune_settings
    mesh = write_box(tmp_path / "box.stl")
    select_roadmap(window, tmp_path / "a.json", "Teil A", mesh)
    wait(lambda: settings.analysis is not None)
    select_roadmap(window, tmp_path / "b.json", "Teil B", tmp_path / "missing.stl")
    assert settings.analysis is None
    wait(lambda: "Hintergrund" not in settings.geometry_error)
    assert "missing.stl" in settings.geometry_error
    context = window.context
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(mesh), ""))
    monkeypatch.setattr(PartPoseDialog, "exec", run_pose_dialog)
    window.replace_stl_button.click()
    assert settings.analysis.path == mesh
    assert window.context == context and context.get("kind") != "manual"
    assert not window.edit_poses_button.isVisible()
    assert not services.calls and not errors


def test_density_unit_and_changed_stl_invalidate_but_preserve_pending_pose(
    offline_window, tmp_path
):
    window, services, errors, wait = offline_window
    settings, _ = prepare(window, wait, tmp_path)
    settings.set_unit("cm")
    wait(lambda: settings.analysis is not None)
    assert settings.analysis.geometry.dimensions_m[0] == 0.2
    settings.density.setText("nan")
    assert settings.analysis is None and "Dichte" in settings.geometry_error
    settings.density.setText("1200")
    wait(lambda: settings.analysis is not None)
    old = window.context
    write_box(settings.path, translation=(12, 0, 0))
    settings.request_analysis()
    wait(lambda: settings.analysis is not None)
    assert window.context is None and settings.unconfirmed_context == old
    window.pages.setCurrentWidget(window.main_page)
    assert window.edit_poses_button.isVisible()
    assert not services.calls and not errors


def test_closing_pose_dialog_during_import_keeps_active_selection(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    path = write_box(tmp_path / "slow.stl")
    started, release = threading.Event(), threading.Event()
    actual = preparation.analyze_stl

    def delayed(*args):
        started.set()
        assert release.wait(5)
        return actual(*args)

    monkeypatch.setattr(preparation, "analyze_stl", delayed)
    dialog = PartPoseDialog(path, 1200, window)
    dialog.show()
    try:
        wait(started.is_set)
        dialog.reject()
        assert window.context is None and dialog.selection is None
    finally:
        release.set()
    wait(lambda: not dialog.worker.isRunning() and not dialog.isVisible())
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert window.context is None and not services.calls and not errors
    dialog.deleteLater()


@pytest.mark.parametrize("accepted", [False, True])
def test_roadmap_load_button_commits_only_confirmed_selection(
    offline_window, tmp_path, monkeypatch, accepted
):
    window, services, errors, wait = offline_window
    settings, _ = prepare(window, wait, tmp_path)
    before = settings.selection()
    parameters_before = window.editor.parameters()
    path = tmp_path / "roadmap.json"
    payload = json.loads((Path(__file__).parents[1] / "examples/roadmap.json").read_text("utf-8"))
    payload.update(part_name="RoadmapPart", source=str(settings.path))
    path.write_text(json.dumps(payload), "utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))

    class Choice:
        def __init__(self, document, *_):
            self.selected_transition = SelectedRoadmapTransition(
                document.path,
                document.part_name,
                document.transitions[0],
                document.pose(5),
                document.pose(0),
            )

        def exec(self):
            return accepted

    monkeypatch.setattr(app, "RoadmapTransitionDialog", Choice)
    window.roadmap_button.click()
    if accepted:
        wait(lambda: settings.analysis is not None)
        assert window.context["part_name"] == "RoadmapPart"
        assert window.context.get("kind") != "manual"
    else:
        assert settings.selection() == before
    assert window.editor.parameters() == parameters_before
    assert not services.calls and not errors


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
