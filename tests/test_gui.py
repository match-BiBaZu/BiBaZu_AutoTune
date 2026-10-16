import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
from test_engine import Services, context, parameters

from bibazu_autotune import app
from bibazu_autotune.profiles import make_profile


def test_window_recipe_trial_result_and_selection(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    if not QFontDatabase.families():
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            qt.setFont(QFont(families[0]))
    services = Services()
    monkeypatch.setattr(app, "PressureClient", lambda _: services.clients()[0])
    monkeypatch.setattr(app, "VisionClient", lambda _: services.clients()[1])
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window = app.MainWindow("http://127.0.0.1:8765", "http://127.0.0.1:8766", tmp_path)
    window.set_context(context())
    window.worker.submit("connect")
    window.show()

    def wait(predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            qt.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(f"condition timed out: {window.state}, {errors}")

    try:
        wait(lambda: window.state == "ready")
        assert window.editor.parameters() == parameters()
        window.add_variant()
        window.editor.rows[1]["pressure_mbar"].setValue(2100)
        path = tmp_path / "recipe.json"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(path), ""))
        window.save_recipe()
        assert (
            json.loads(path.read_text("utf-8"))["variants"][1]["parameters"]["arrays"][1][
                "pressure_mbar"
            ]
            == 2100
        )
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(path), ""))
        window.load_recipe()
        assert window.variant_list.count() == 2
        window.variant_list.setCurrentRow(1)
        window.start_trial()
        wait(lambda: window.state == "waiting")
        assert not window.editor.isEnabled()
        assert window.trigger_button.isEnabled()
        assert window.conveyor_start_button.isEnabled()
        window.request_conveyor(True)
        wait(lambda: services.moving)
        window.request_conveyor(False)
        wait(lambda: not services.moving and not window.motor_pending)
        trial_id = next(iter(services.trials))
        services.trials[trial_id].update(
            status="completed",
            measurements=[
                {"timestamp": 1.0, "angle_deg": 12.0},
                {"timestamp": 1.1, "angle_deg": 20.0},
            ],
        )
        wait(lambda: window.state == "ready" and len(window.results) == 1)
        assert window.result_table.rowCount() == 1
        assert "2100 mbar" in window.result_details.text()
        window.save_result()
        wait(lambda: window.state == "ready")
        assert json.loads(path.read_text("utf-8"))["version"] == 13
        assert services.parameters["arrays"][1]["pressure_mbar"] == 2100
        window.handle_event(
            "result",
            {
                "trial_id": "aborted-without-clip",
                "status": "aborted",
                "parameters": parameters(),
                "result": {"measurements": None},
            },
        )
        assert window.plot.samples == []
        assert errors == []
        if os.environ.get("BIBAZU_GUI_ARTIFACTS"):
            destination = app.Path(os.environ["BIBAZU_GUI_ARTIFACTS"])
            destination.mkdir(parents=True, exist_ok=True)
            assert window.grab().save(str(destination / "autotune.png"))
    finally:
        window.worker.stopping.set()
        wait(lambda: not window.worker.isRunning())
        window.close()
        window.deleteLater()
        qt.processEvents()


@pytest.fixture
def offline_window(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    services = Services()
    monkeypatch.setattr(app, "PressureClient", lambda _: services.clients()[0])
    monkeypatch.setattr(app, "VisionClient", lambda _: services.clients()[1])
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window = app.MainWindow("http://127.0.0.1:8765", "http://127.0.0.1:8766", tmp_path)

    def wait(predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            qt.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out: {window.state}, {errors}")

    yield window, services, errors, wait
    window.worker.stopping.set()
    wait(lambda: not window.worker.isRunning())
    window.close()
    qt.processEvents()


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
    assert window.state == "disconnected"
    assert window.profile_button.isEnabled()
    assert window.roadmap_button.isEnabled()
    window.load_profile()
    assert window.editor.parameters() == candidate
    assert window.context == local_context
    assert not any(method == "PUT" for method, _, _ in services.calls)
    window.worker.submit("connect")
    wait(lambda: window.state == "ready")
    assert window.editor.parameters() == candidate
    assert window.context == local_context
    assert not errors


def test_offline_recipe_load_adopts_transition(offline_window, tmp_path, monkeypatch):
    window, services, errors, _ = offline_window
    path = tmp_path / "recipe.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "roadmap_transition": context(),
                "variants": [{"name": "local variant", "parameters": parameters()}],
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    window.load_recipe()
    assert window.context == context()
    assert window.editor.parameters() == parameters()
    assert not services.calls
    assert not errors


def test_save_dialog_cancellation_does_not_apply(offline_window, monkeypatch):
    window, services, errors, wait = offline_window
    window.set_context(context())
    window.worker.submit("connect")
    wait(lambda: window.state == "ready")
    window.handle_event(
        "result",
        {
            "trial_id": "result1",
            "status": "completed",
            "parameters": parameters(),
        },
    )
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    window.save_result()
    assert window.state == "ready"
    assert not any(method == "PUT" for method, _, _ in services.calls)
    assert not errors


def test_plc_remains_available_when_camera_api_times_out(offline_window):
    window, services, errors, wait = offline_window
    services.fail_host = "vision.local"
    window.set_context(context())
    window.worker.submit("connect")
    wait(lambda: window.state == "ready" and "timed out" in window.camera_connection_label.text())
    assert window.editor.parameters() == parameters()
    assert not window.start_button.isEnabled()
    assert window.camera_connect_button.isEnabled()
    assert window.conveyor_stop_button.isEnabled()
    assert not errors
    services.fail_host = None
    window.worker.submit("connect_camera")
    wait(lambda: window.camera_connected)
    assert window.start_button.isEnabled()


def test_manual_controls_without_roadmap_or_camera(offline_window):
    window, services, errors, wait = offline_window
    services.fail_host = "vision.local"
    assert "Keine Roadmap geladen" in window.context_label.text()
    assert not window.apply_parameters_button.isEnabled()
    assert not window.conveyor_start_button.isEnabled()
    window.worker.submit("connect")
    wait(lambda: window.state == "ready" and "timed out" in window.camera_connection_label.text())
    assert window.context is None
    assert window.apply_parameters_button.isEnabled()
    assert window.conveyor_start_button.isEnabled()
    assert not window.start_button.isEnabled()
    window.editor.rows[0]["enabled"].setChecked(True)
    window.editor.rows[1]["pressure_mbar"].setValue(1900)
    window.editor.speed.setValue(90)
    window.apply_parameters()
    assert not window.conveyor_start_button.isEnabled()
    wait(lambda: window.state == "ready")
    assert services.parameters["arrays"][0]["enabled"] is True
    assert services.parameters["arrays"][1]["pressure_mbar"] == 1900
    assert services.parameters["conveyor_speed_mm_per_sec"] == 90
    assert not services.trials
    window.request_conveyor(True)
    wait(lambda: services.moving and not window.motor_pending)
    wait(lambda: not window.apply_parameters_button.isEnabled())
    assert window.conveyor_stop_button.isEnabled()
    window.request_conveyor(False)
    wait(lambda: not services.moving and window.apply_parameters_button.isEnabled())
    assert "Keine Roadmap geladen" in window.context_label.text()
    assert not errors
