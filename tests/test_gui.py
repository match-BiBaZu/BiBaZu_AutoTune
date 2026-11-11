import copy
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton
from test_engine import Services, context, parameters

from bibazu_autotune import app
from bibazu_autotune.profiles import empty_parameters, make_profile, profile_parameters


@pytest.fixture
def offline_window(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    services = Services()

    def pressure_client(_):
        pressure, unused_vision = services.clients()
        unused_vision.close()
        return pressure

    monkeypatch.setattr(app, "PressureClient", pressure_client)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window = app.MainWindow("http://127.0.0.1:8765", tmp_path / "runs")

    def wait(predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            qt.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out: {window.state}, {errors}")

    window.show()
    yield window, services, errors, wait
    window.close()
    wait(lambda: not window.worker.isRunning() and not window.autotune_settings.workers_running())
    window.close()
    qt.processEvents()


def test_operator_surface_contains_sources_and_no_experiment_controls(offline_window):
    window, services, errors, _ = offline_window
    texts = {b.text() for b in window.findChildren(QPushButton)}
    assert {"Roadmap laden", "Aus STL laden", "Berechnung durchführen", "SPS verbinden"} <= texts
    assert not texts & {
        "Testreihe laden",
        "Testreihe speichern",
        "Variante kopieren",
        "Variante entfernen",
        "Kamera verbinden",
        "Versuch freigeben",
        "Aufnahme manuell auslösen",
    }
    assert not hasattr(window, "variants") and not hasattr(window, "variant_list")
    assert [window.autotune_settings.pages.tabText(i) for i in range(2)] == [
        "Geometrie prüfen",
        "Modell und Anlage",
    ]
    assert "STL auswählen / ersetzen" not in texts
    assert not services.calls and not errors


def test_offline_profile_load_and_connect_preserves_local_selection(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    candidate = parameters()
    candidate["arrays"][1]["pressure_mbar"] = 1234
    local_context = {**context(), "part_name": "local-part"}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(make_profile(candidate, local_context)), "utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    assert window.load_parameters_action.isEnabled()
    window.load_parameters()
    assert window.editor.parameters() == candidate and window.context == local_context
    assert not services.calls
    window.worker.submit("connect")
    wait(
        lambda: (
            window.state == "ready"
            and services.parameters == candidate
            and window.sent_parameters is None
        )
    )
    assert window.editor.parameters() == candidate
    assert not list(tmp_path.rglob("run.json"))
    assert not services.trials and not errors


def test_parameter_save_roundtrip_and_cancel_never_write_sps(offline_window, tmp_path, monkeypatch):
    window, services, errors, wait = offline_window
    window.set_context(context())
    window.editor.set_parameters(parameters())
    window.worker.submit("connect")
    wait(lambda: window.state == "ready")
    path = tmp_path / "parameters.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    window.save_parameters()
    saved = json.loads(path.read_text("utf-8"))
    assert saved["version"] == 13 and "variants" not in saved
    assert profile_parameters(saved) == parameters()
    assert saved["roadmap_transition"] == context()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    window.save_parameters()
    assert not any(method == "PUT" for method, _, _ in services.calls)
    assert not errors


def test_bad_parameter_file_does_not_change_selection_or_editor(
    offline_window, tmp_path, monkeypatch
):
    window, _, errors, _ = offline_window
    window.set_context(context())
    before = copy.deepcopy(window.editor.parameters())
    path = tmp_path / "bad.json"
    bad = make_profile(parameters(), {**context(), "part_name": "wrong"})
    bad["arrays"][0]["pressure_mbar"] = 6001
    path.write_text(json.dumps(bad), "utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    window.load_parameters()
    assert window.context == context() and window.editor.parameters() == before
    assert errors


@pytest.mark.parametrize("part_selected", [False, True])
def test_plain_pressure_profile_load_save_preserves_part_selection(
    offline_window, tmp_path, monkeypatch, part_selected
):
    window, services, errors, _ = offline_window
    if part_selected:
        window.set_context(context())
    original_context = copy.deepcopy(window.context)
    profile = make_profile(parameters())
    # Older Pressure-GUI profiles can use individual nozzle flags only.
    for array in profile["arrays"]:
        del array["nozzles_enabled"]
    path = tmp_path / "pressure-profile.json"
    path.write_text(json.dumps(profile), "utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    window.load_parameters()
    assert window.context == original_context
    assert window.editor.parameters() == parameters()
    assert window.save_parameters_action.isEnabled()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    window.save_parameters()
    saved = json.loads(path.read_text("utf-8"))
    assert profile_parameters(saved) == parameters()
    assert saved.get("roadmap_transition") == original_context
    assert not services.calls and not errors


def test_loading_empty_local_profile_is_not_overwritten_on_connect(
    offline_window, tmp_path, monkeypatch
):
    window, services, errors, wait = offline_window
    desired = empty_parameters()
    assert window.editor.parameters() == desired
    path = tmp_path / "empty-profile.json"
    path.write_text(json.dumps(make_profile(desired)), "utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    window.load_parameters()
    assert window.has_local_parameters and not services.calls
    window.worker.submit("connect")
    wait(lambda: services.parameters == desired and window.state == "ready")
    assert window.editor.parameters() == desired and not errors


def test_manual_controls_without_roadmap_or_camera_create_no_runs(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    services.fail_host = "vision.local"
    assert "Kein Bauteil" in window.context_label.text()
    window.worker.submit("connect")
    wait(lambda: window.state == "ready")
    assert window.editor.parameters() == parameters()
    assert not any(method == "PUT" for method, _, _ in services.calls)
    window.editor.rows[1]["pressure_mbar"].setValue(1900)
    window.apply_parameters()
    wait(
        lambda: (
            window.state == "ready" and services.parameters["arrays"][1]["pressure_mbar"] == 1900
        )
    )
    window.request_conveyor(True)
    wait(lambda: services.moving and not window.motor_pending)
    assert window.editor.isEnabled() and window.apply_parameters_button.isEnabled()
    window.request_conveyor(False)
    wait(lambda: not services.moving and not window.motor_pending)
    assert not list(tmp_path.rglob("run.json"))
    assert not any("trials" in path or "measurements" in path for _, path, _ in services.calls)
    assert not services.trials and not errors
