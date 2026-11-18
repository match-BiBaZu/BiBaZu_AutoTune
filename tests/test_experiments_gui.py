"""Desktop acceptance tests using the isolated simulator and real Qt widgets."""

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox, QPushButton
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
    wait(
        lambda: (
            not window.worker.isRunning()
            and not window.settings.workers_running()
            and not window.pose_worker.isRunning()
        )
    )
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


def test_row_review_buttons_preserve_raw_data_and_require_class_for_good_data(
    training_window, tmp_path, monkeypatch
):
    window, wait, errors = training_window
    prepare_campaign(window, wait, tmp_path)
    window.confirm_button.click()
    wait(lambda: window.state == "waiting")
    window.band_start.click()
    wait(lambda: window.state == "complete")
    store = CampaignStore(window.campaign_path)
    first, second = store.trials()
    first["evaluation"].update(
        label=None, quality="review_required", reasons=["capture_incomplete", "dropped_frames"]
    )
    first["stop_reason"] = "memory_limit"
    store.save_trial(first)
    directory = store.trial_directory(first["trial_id"])
    original = {
        name: (directory / name).read_bytes() for name in ("trial.json", "measurements.csv")
    }
    window.open_review()
    review = window.review

    def click(trial_id, text):
        index = next(i for i, row in enumerate(review.records) if row["trial_id"] == trial_id)
        widget = review.table.cellWidget(index, 8)
        next(b for b in widget.findChildren(QPushButton) if b.text() == text).click()

    assert "Aufnahmepuffer voll" in review.table.item(0, 7).text()
    click(second["trial_id"], "Schlecht")
    assert store.annotation(second["trial_id"])["excluded"]
    assert review.selected()["trial_id"] == second["trial_id"]
    assert not DatasetReader([store]).classifier_rows()
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: ("", False))
    click(first["trial_id"], "Gut")
    assert store.annotation(first["trial_id"]) is None
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: ("not_flipped", True))
    click(first["trial_id"], "Gut")
    assert not store.annotation(first["trial_id"])["excluded"]
    assert store.annotation(first["trial_id"])["label"] == "not_flipped"
    assert len(DatasetReader([store]).classifier_rows()) == 1
    assert review.table.item(0, 5).text() == "Gut"
    assert all((directory / name).read_bytes() == content for name, content in original.items())
    click(second["trial_id"], "Gut")
    assert len(DatasetReader([store]).classifier_rows()) == 2
    assert len(store.annotation(second["trial_id"])["reason"]) > 0
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


def test_review_decisions_update_live_repeat_count_and_finish_campaign(
    training_window, tmp_path, monkeypatch
):
    from bibazu_autotune.experiments import storage

    evaluate = storage.evaluate

    def uncertain(*args, **kwargs):
        result, samples = evaluate(*args, **kwargs)
        result.update(label=None, quality="review_required", reasons=["interrupted_rotation"])
        return result, samples

    monkeypatch.setattr(storage, "evaluate", uncertain)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: ("not_flipped", True))
    window, wait, errors = training_window
    prepare_campaign(window, wait, tmp_path)
    window.confirm_button.click()
    wait(lambda: window.state == "waiting")
    window.band_start.click()
    wait(lambda: window.state == "review_pending")
    assert window.pending_repeats == 2
    window.open_review()
    first, second = window.review.records
    window.review.quick_review(first["trial_id"], True)
    wait(lambda: window.pending_repeats == 1)
    window.review.quick_review(second["trial_id"], False)
    wait(lambda: window.pending_repeats == 0 and window.state == "complete")
    assert not window.repeat_uncertain_button.isEnabled()
    assert "Offene unsichere Versuche: 0" in window.repeat_info.text()
    store = CampaignStore(window.campaign_path)
    assert len(store.trials()) == 2
    assert not store.pending_repeats()
    assert len(DatasetReader([store]).classifier_rows()) == 1
    assert not errors


def test_review_video_delete_buttons_preserve_curves_labels_and_exports(
    training_window, tmp_path, monkeypatch
):
    window, wait, errors = training_window
    prepare_campaign(window, wait, tmp_path)
    window.confirm_button.click()
    wait(lambda: window.state == "waiting")
    window.band_start.click()
    wait(lambda: window.state == "complete")
    window.open_review()
    review = window.review
    store = review.store
    assert len(list(store.directory.rglob("*.mp4"))) == 2
    assert not review.delete_video_button.isEnabled()
    review.label.setCurrentText("flipped")
    review.reason.setText("Bildprüfung abgeschlossen")
    review.annotate()
    assert review.delete_video_button.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    review.delete_video_button.click()
    assert len(list(store.directory.rglob("*.mp4"))) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    review.delete_video_button.click()
    assert len(list(store.directory.rglob("*.mp4"))) == 1
    assert "gelöscht" in review.image_label.text()
    assert review.curve.rows
    assert not review.slider.isEnabled()
    review.table.selectRow(1)
    review.label.setCurrentText("flipped")
    review.reason.setText("Zweites Video geprüft")
    review.annotate()
    assert review.delete_all_videos_button.isEnabled()
    review.delete_all_videos_button.click()
    assert not list(store.directory.rglob("*.mp4"))
    assert DatasetReader([store]).export_csv(tmp_path / "after-review.csv") == 2
    assert len(DatasetReader([store]).classifier_rows()) == 2
    assert not errors


@pytest.mark.parametrize("delete", [False, True])
def test_campaign_dialog_stops_capture_and_optionally_deletes(
    training_window, tmp_path, monkeypatch, delete
):
    window, wait, errors = training_window
    prepare_campaign(window, wait, tmp_path)
    wait(lambda: all(p.pixmap() and not p.pixmap().isNull() for p in window.run_previews))
    directory = window.campaign_path
    # Resumed campaigns render their archived poses even without a current editor selection.
    window.settings.set_context(None)
    window.load_run_poses(directory)
    wait(lambda: all(p.pixmap() and not p.pixmap().isNull() for p in window.run_previews))
    window.confirm_button.click()
    wait(lambda: window.state == "waiting")

    def choose(dialog):
        title = "Abbrechen und Daten löschen" if delete else "Abbrechen · Daten behalten"
        next(b for b in dialog.buttons() if b.text() == title).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", choose)
    window.end_campaign_button.click()
    wait(lambda: window.campaign_path is None)
    assert window.state == "disconnected"
    from pathlib import Path

    assert Path(directory).exists() != delete
    if not delete:
        assert CampaignStore(directory).trials()[0]["status"] == "aborted"
    assert not window.end_campaign_button.isEnabled()
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


def test_gui_finishes_uncertain_series_and_offers_deferred_repeat(training_window, tmp_path):
    window, wait, errors = training_window
    prepare_candidate(window, wait, tmp_path)
    window.repetitions.setValue(2)
    window.settling.setValue(0)
    window.observable.setChecked(False)
    window.uncertain_repeat_timing.setCurrentIndex(1)
    window.confirm_setup.setChecked(True)
    window.connect_services()
    wait(lambda: window.state == "ready")
    window.create_campaign()
    wait(lambda: window.state == "gate")
    assert CampaignStore(window.campaign_path).plan.uncertain_repeat_timing == "campaign_end"
    window.confirm_button.click()
    wait(lambda: window.state == "waiting")
    window.band_start.click()
    wait(lambda: window.state == "review_pending")
    store = CampaignStore(window.campaign_path)
    assert len(store.trials()) == 2
    assert len(store.pending_repeats()) == 2
    assert window.repeat_uncertain_button.isEnabled()
    assert window.defer_uncertain_button.isEnabled()
    window.defer_uncertain_button.click()
    wait(lambda: window.state == "complete")
    assert window.repeat_uncertain_button.isEnabled()
    window.repeat_uncertain_button.click()
    wait(lambda: window.state == "review_pending")
    assert len(store.trials()) == 4
    assert all(r.get("replaces_trial_id") for r in store.trials()[2:])
    assert not errors


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
