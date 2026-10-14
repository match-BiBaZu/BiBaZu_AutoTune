import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
from test_engine import Services, parameters

from bibazu_autotune import app


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
