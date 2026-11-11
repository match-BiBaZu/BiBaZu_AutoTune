import copy
import json

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QFileDialog
from test_engine import context, parameters
from test_gui import offline_window as offline_window
from test_model_gui import prepare

from bibazu_autotune.profiles import make_profile


def writes(services):
    return [
        json.loads(body)
        for method, path, body in services.calls
        if method == "PUT" and path.endswith("/parameters")
    ]


def connect(window, wait):
    window.worker.submit("connect")
    wait(lambda: window.state == "ready")


def wait_applied(window, services, wait, expected):
    wait(
        lambda: (
            window.state == "ready"
            and services.parameters == expected
            and window.confirmed_parameters == expected
            and window.pending_parameters is None
            and window.sent_parameters is None
        )
    )


def test_connect_only_reads_and_every_editor_control_writes_live(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    QTest.qWait(350)
    assert not writes(services)
    row = window.editor.rows[0]
    changes = [
        lambda: row["pressure_mbar"].setValue(1800),
        lambda: row["pulse_duration_ms"].setValue(75),
        lambda: row["delay_ms"].setValue(12),
        lambda: row["offset_mm"].setValue(25.5),
        lambda: row["enabled"].setChecked(True),
        lambda: row["nozzles_enabled"][2].setChecked(False),
        lambda: row["conveyor_velocity_delay"].setChecked(False),
        lambda: window.editor.speed.setValue(95),
    ]
    for count, change in enumerate(changes, 1):
        change()
        expected = window.editor.parameters()
        wait_applied(window, services, wait, expected)
        assert len(writes(services)) == count
        assert window.editor.parameters() == expected
    QTest.qWait(350)
    assert len(writes(services)) == len(changes)  # Readback produces no echo writes.
    assert not services.trials and not services.moving and not errors
    assert "Werte bestätigt" in window.live_status.text()


def test_rapid_changes_and_bulk_updates_send_only_final_complete_set(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    row = window.editor.rows[1]
    for pressure in (1234, 1300, 1456):
        row["pressure_mbar"].setValue(pressure)
    expected = window.editor.parameters()
    wait_applied(window, services, wait, expected)
    assert writes(services) == [expected]
    bulk = copy.deepcopy(expected)
    bulk["arrays"][0].update(enabled=True, pressure_mbar=900, offset_mm=30.5)
    bulk["arrays"][2].update(pressure_mbar=1111, pulse_duration_ms=100)
    window.editor.set_parameters(bulk)
    wait_applied(window, services, wait, bulk)
    assert writes(services) == [expected, bulk]
    assert not errors


def test_connected_profile_load_is_transferred_once(offline_window, tmp_path, monkeypatch):
    window, services, errors, wait = offline_window
    connect(window, wait)
    expected = parameters()
    expected["arrays"][1]["pressure_mbar"] = 1400
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(make_profile(expected, context())), "utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    window.load_parameters()
    wait_applied(window, services, wait, expected)
    assert writes(services) == [expected]
    assert window.context == context()
    assert not errors


def test_calculation_does_not_write_but_imported_result_is_live(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    settings, _ = prepare(window, wait, tmp_path)
    window.editor.speed.setValue(90)
    original = window.editor.parameters()
    connect(window, wait)
    wait_applied(window, services, wait, original)
    count = len(writes(services))
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    assert len(writes(services)) == count
    assert window.import_button.isEnabled()
    window.import_button.click()
    expected = window.editor.parameters()
    selected = window.autotune_results.selected_candidate()
    assert selected.sps_pressure is not None
    assert expected["arrays"][selected.mount.array_index - 1]["pressure_mbar"] == round(
        selected.sps_pressure.gauge_pa / 100
    )
    assert selected.sps_pressure.gauge_pa != selected.pressure.gauge_pa
    wait_applied(window, services, wait, expected)
    assert writes(services)[-1] == expected
    assert len(writes(services)) == count + 1
    assert not services.trials and not services.moving and not errors


def test_offline_edits_are_kept_and_applied_once_after_connect(offline_window):
    window, services, errors, wait = offline_window
    window.editor.set_parameters(parameters())
    window.editor.rows[1]["pressure_mbar"].setValue(1234)
    expected = window.editor.parameters()
    QTest.qWait(350)
    assert not services.calls
    connect(window, wait)
    wait_applied(window, services, wait, expected)
    assert window.editor.parameters() == expected
    assert writes(services) == [expected]
    assert not errors


def test_cancelled_parameter_load_keeps_values_and_does_not_write(offline_window, monkeypatch):
    window, services, errors, wait = offline_window
    connect(window, wait)
    original = window.editor.parameters()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    window.load_parameters()
    QTest.qWait(350)
    assert window.editor.parameters() == original
    assert not writes(services) and not errors


def test_edit_during_write_survives_older_readback(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    services.pending = True
    window.editor.rows[0]["pressure_mbar"].setValue(1000)
    first = window.editor.parameters()
    wait(lambda: window.state == "updating" and writes(services))
    assert window.editor.isEnabled()
    window.editor.rows[0]["pressure_mbar"].setValue(1700)
    window.editor.rows[0]["offset_mm"].setValue(42.5)
    latest = window.editor.parameters()
    QTest.qWait(350)
    assert writes(services) == [first]
    services.pending = False
    for operation in services.operations.values():
        if operation["status"] == "pending":
            operation["status"] = "succeeded"
    wait_applied(window, services, wait, latest)
    assert window.editor.parameters() == latest
    assert writes(services) == [first, latest]
    assert not errors


def test_running_conveyor_writes_changes_without_stopping(offline_window):
    window, services, errors, wait = offline_window
    services.moving = True
    connect(window, wait)
    window.editor.rows[0]["pressure_mbar"].setValue(1234)
    expected = window.editor.parameters()
    assert window.editor.isEnabled()
    assert window.apply_parameters_button.isEnabled()
    wait_applied(window, services, wait, expected)
    assert writes(services) == [expected]
    assert services.moving and not services.trials and not errors
    assert not any("/conveyor/" in path for _, path, _ in services.calls)


def test_parameter_lock_pauses_pending_write_and_keeps_conveyor_controls(offline_window):
    window, services, errors, wait = offline_window
    services.moving = True
    connect(window, wait)
    window.editor.rows[0]["pressure_mbar"].setValue(1234)
    expected = window.editor.parameters()
    window.parameter_lock_button.click()
    assert window.parameters_locked
    assert window.parameter_lock_button.text() == "Parameter entsperren"
    assert not window.editor.isEnabled()
    assert not window.load_parameters_action.isEnabled()
    assert not window.apply_parameters_button.isEnabled()
    assert window.conveyor_stop_button.isEnabled()
    QTest.qWait(350)
    assert not writes(services)
    assert "Entsperren" in window.live_status.text()
    window.parameter_lock_button.click()
    wait_applied(window, services, wait, expected)
    assert window.editor.isEnabled()
    assert writes(services) == [expected]
    assert services.moving
    window.parameter_lock_button.click()
    window.conveyor_stop_button.click()
    wait(lambda: not services.moving and window.conveyor_start_button.isEnabled())
    window.conveyor_start_button.click()
    wait(lambda: services.moving and not window.motor_pending)
    assert window.parameters_locked and not window.editor.isEnabled()
    assert writes(services) == [expected]
    assert not errors


def test_lock_during_write_finishes_only_the_inflight_request(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    services.pending = True
    window.editor.rows[0]["pressure_mbar"].setValue(1000)
    first = window.editor.parameters()
    wait(lambda: window.state == "updating" and writes(services))
    window.editor.rows[0]["pressure_mbar"].setValue(1700)
    latest = window.editor.parameters()
    window.parameter_lock_button.click()
    services.pending = False
    for operation in services.operations.values():
        if operation["status"] == "pending":
            operation["status"] = "succeeded"
    wait(lambda: window.state == "ready")
    QTest.qWait(350)
    assert writes(services) == [first]
    assert window.editor.parameters() == latest
    assert window.parameters_locked and not window.editor.isEnabled()
    window.parameter_lock_button.click()
    wait_applied(window, services, wait, latest)
    assert writes(services) == [first, latest]
    assert not errors


def test_offline_parameter_lock_survives_connection_and_defers_local_values(offline_window):
    window, services, errors, wait = offline_window
    expected = parameters()
    expected["arrays"][0]["pressure_mbar"] = 1200
    window.editor.set_parameters(expected)
    window.parameter_lock_button.click()
    connect(window, wait)
    QTest.qWait(350)
    assert not writes(services)
    assert window.editor.parameters() == expected
    assert window.parameters_locked and not window.editor.isEnabled()
    window.parameter_lock_button.click()
    wait_applied(window, services, wait, expected)
    assert writes(services) == [expected]
    assert not errors


def test_autotune_buttons_are_in_results_and_lock_blocks_only_import(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    settings, _ = prepare(window, wait, tmp_path)
    window.editor.speed.setValue(90)
    assert window.autotune_results.isAncestorOf(window.calculate_button)
    assert window.autotune_results.isAncestorOf(window.import_button)
    assert window.autotune_results.isAncestorOf(window.autotune_status)
    window.parameter_lock_button.click()
    original = window.editor.parameters()
    assert window.calculate_button.isEnabled()
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    assert not window.import_button.isEnabled()
    window.import_autotune_result()
    assert "Parameter zuerst entsperren" in window.autotune_status.text()
    assert window.editor.parameters() == original
    window.parameter_lock_button.click()
    assert window.import_button.isEnabled()
    assert not services.calls and not errors


def test_invalid_edit_during_write_is_not_replaced_by_readback(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    services.pending = True
    window.editor.rows[0]["pressure_mbar"].setValue(1000)
    wait(lambda: window.state == "updating" and writes(services))
    window.editor.speed.setValue(0)
    services.pending = False
    for operation in services.operations.values():
        if operation["status"] == "pending":
            operation["status"] = "succeeded"
    wait(lambda: window.state == "ready")
    assert window.editor.speed.value() == 0
    assert window.parameter_error
    QTest.qWait(350)
    assert len(writes(services)) == 1
    window.editor.speed.setValue(90)
    expected = window.editor.parameters()
    wait_applied(window, services, wait, expected)
    assert len(writes(services)) == 2
    assert not errors


def test_file_dialog_defers_live_sync_until_closed(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    with window.file_operation():
        window.editor.rows[0]["pressure_mbar"].setValue(1234)
        QTest.qWait(350)
        assert not writes(services)
    expected = window.editor.parameters()
    wait_applied(window, services, wait, expected)
    assert writes(services) == [expected] and not errors


def test_invalid_input_cancels_pending_write_and_recovers_when_valid(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    window.editor.rows[0]["pressure_mbar"].setValue(1234)
    window.editor.speed.setValue(0)  # Array 2 still uses an offset with band speed.
    QTest.qWait(400)
    assert not writes(services)
    assert "Eingabe ungültig" in window.live_status.text()
    assert not window.conveyor_start_button.isEnabled()
    window.editor.speed.setValue(90)
    expected = window.editor.parameters()
    wait_applied(window, services, wait, expected)
    assert writes(services) == [expected]
    assert not errors


def test_disconnect_cancels_debounce_and_failure_does_not_retry(offline_window):
    window, services, errors, wait = offline_window
    connect(window, wait)
    window.editor.rows[0]["pressure_mbar"].setValue(1234)
    window.release_button.click()
    wait(lambda: window.state == "disconnected")
    QTest.qWait(350)
    assert not writes(services)
    services.operation_failure = True
    connect(window, wait)
    wait(lambda: window.state == "disconnected" and errors)
    QTest.qWait(500)
    assert len(writes(services)) == 1
    assert "readback" in errors[0]
