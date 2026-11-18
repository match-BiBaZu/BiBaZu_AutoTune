"""Desktop acceptance tests using the isolated simulator and real Qt widgets."""

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QMessageBox
from test_model_gui import prepare

from bibazu_autotune.experiments.app import TrainingWindow
from bibazu_autotune.experiments.dataset import DatasetReader
from bibazu_autotune.experiments.storage import CampaignStore


@pytest.fixture
def training_window(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window = TrainingWindow(None, "simulation", tmp_path / "campaigns", demo=True)
    window.show()

    def wait(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"State {window.state}; errors {errors}; {window.log.toPlainText()}")

    yield window, wait, errors
    window.close()
    wait(lambda: not window.worker.isRunning() and not window.settings.workers_running())
    window.close()
    application.processEvents()


def prepare_candidate(window, wait, tmp_path):
    # Exercise the existing analytical preparation and new candidate capture together.
    adapter = SimpleNamespace(
        autotune_settings=window.settings,
        editor=window.editor,
        accept_selection=window.settings.accept_selection,
    )
    prepare(adapter, wait, tmp_path)
    window.settings.calculate_model()
    wait(lambda: window.settings.model_result is not None)
    window.select_candidate()
    assert window.candidate is not None


def prepare_campaign(window, wait, tmp_path):
    prepare_candidate(window, wait, tmp_path)
    for pressure in window.pressures[:2]:
        pressure.setValue(2000)
    window.repetitions.setValue(2)
    window.settling.setValue(0)
    window.observable.setChecked(True)
    window.confirm_setup.setChecked(True)
    window.connect_services()
    wait(lambda: window.state == "ready")
    window.create_campaign()
    wait(lambda: window.state == "gate")


def test_window_starts_offline_and_has_three_workspaces(training_window, tmp_path):
    window, wait, errors = training_window
    assert window.pages.count() == 3
    assert window.state == "disconnected"
    assert not window.confirm_button.isEnabled()
    assert not (tmp_path / "campaigns").exists()
    assert not errors


def test_full_gui_acquisition_review_and_training_data(training_window, tmp_path):
    window, wait, errors = training_window
    prepare_campaign(window, wait, tmp_path)
    window.confirm_button.click()
    wait(lambda: window.state == "waiting")
    assert "steht" in window.band_label.text()
    window.band_start.click()
    wait(lambda: window.state == "complete")
    store = CampaignStore(window.campaign_path)
    records = store.trials()
    assert len(records) == 2
    assert all(record["resolution"] == "accepted" for record in records)
    assert all(record["analytical"]["pressure"]["gauge_pa"] > 0 for record in records)
    assert store.manifest["assets"][0]["sha256"] == window.settings.analysis.sha256
    assert "läuft" in window.band_label.text()
    window.open_review()
    assert window.review.table.rowCount() == 2
    assert window.review.frames
    window.review.slider.setValue(10)
    assert not window.review.image_label.pixmap().isNull()
    window.review.label.setCurrentText("overflipped")
    window.review.excluded.setChecked(True)
    window.review.reason.setText("Bildkontrolle: fehlerhafter Versuch")
    window.review.annotate()
    assert len(DatasetReader([store]).classifier_rows()) == 1
    window.training_dataset()
    assert "1 klassifizierbare" in window.training_count.text()
    assert not errors


def test_pause_archives_aborted_attempt_and_retry_waits_for_new_part(training_window, tmp_path):
    window, wait, errors = training_window
    prepare_campaign(window, wait, tmp_path)
    window.confirm_button.click()
    wait(lambda: window.state == "waiting")
    window.pause()
    wait(lambda: window.state == "paused")
    records = CampaignStore(window.campaign_path).trials()
    assert len(records) == 1
    assert records[0]["status"] == "aborted"
    assert records[0]["evaluation"]["label"] is None
    assert window.skip_button.isEnabled()
    window.retry_button.click()
    wait(lambda: window.state == "waiting")
    records = CampaignStore(window.campaign_path).trials()
    assert len(records) == 2
    assert records[-1]["replaces_trial_id"] == records[0]["trial_id"]
    assert not errors


def test_space_in_text_field_is_not_a_series_confirmation(training_window, tmp_path):
    window, wait, errors = training_window
    prepare_campaign(window, wait, tmp_path)
    window.pages.setCurrentWidget(window.review)
    window.review.reason.setFocus()
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ")
    assert not window.eventFilter(window.review.reason, event)
    assert window.state == "gate"
    assert not CampaignStore(window.campaign_path).trials()
    assert not errors


def test_space_confirms_once_even_with_button_focus(training_window, monkeypatch):
    window, wait, errors = training_window
    window.state = "gate"
    window.setup.setCurrentWidget(window.run_page)
    monkeypatch.setattr(window, "isActiveWindow", lambda: True)
    confirmations = []
    monkeypatch.setattr(window, "confirm_series", lambda: confirmations.append(True))
    window.confirm_button.setFocus()
    press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ")
    repeat = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ", True
    )
    assert window.eventFilter(window.confirm_button, press)
    assert window.eventFilter(window.confirm_button, repeat)
    assert confirmations == [True]


def test_impulse_grid_has_pressure_references_and_survives_candidate_selection(
    training_window, tmp_path
):
    window, wait, errors = training_window
    for control, value in zip(window.pulses, (50, 150, 50), strict=True):
        control.setValue(value)
    for control, value in zip(window.pressures, (1000, 2000, 100), strict=True):
        control.setValue(value)
    prepare_candidate(window, wait, tmp_path)
    first_page = window.setup.widget(0)
    assert first_page.isAncestorOf(window.pulses[0])
    assert first_page.isAncestorOf(window.pressures[0])
    assert first_page.isAncestorOf(window.reference_table)
    assert [control.value() for control in window.pulses] == [50, 150, 50]
    assert [control.value() for control in window.pressures] == [1000, 2000, 100]
    assert window.reference_table.rowCount() == 3
    window.confirm_setup.setChecked(True)
    plan = window.build_plan()
    pressures = []
    for row, duration in enumerate((50, 100, 150)):
        analytical = plan.snapshot["analytical_by_duration"][str(duration)]
        assert window.reference_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == duration
        pressure = window.reference_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        pressures.append(pressure)
        assert pressure == pytest.approx(analytical["pressure"]["gauge_pa"] / 100)
        assert window.reference_table.item(row, 2).data(Qt.ItemDataRole.UserRole) == pytest.approx(
            analytical["sps_pressure"]["gauge_pa"] / 100
        )
    assert pressures[0] > pressures[1] > pressures[2]
    # Pressure bounds do not affect the analytical reference for an impulse duration.
    window.pressures[1].setValue(0)
    assert window.reference_table.rowCount() == 3
    window.pulses[2].setValue(40)
    assert window.reference_table.rowCount() == 0
    assert "Endwert" in window.reference_status.text()
    window.pulses[2].setValue(50)
    assert window.reference_table.rowCount() == 3
    window.settings.model_panel.fields["spread_angle_deg"][0].setValue(1)
    assert window.reference_table.rowCount() == 0
    assert window.candidate is None
    assert not errors
