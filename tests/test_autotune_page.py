import copy
from dataclasses import replace

import pytest
from test_candidate_transfer import candidate
from test_gui import offline_window as offline_window
from test_model_gui import prepare

from bibazu_autotune.candidate_transfer import candidate_parameters


def calculate(window, wait, tmp_path):
    settings, _ = prepare(window, wait, tmp_path)
    window.pages.setCurrentWidget(window.main_page)
    assert window.calculate_button.isEnabled()
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    return settings.model_result


def show_candidate(window, result, value):
    result = replace(result, candidates=(replace(value, pulse_ms=500),))
    window.autotune_settings.model_result = result
    window.show_autotune_results(result)


def test_main_page_calculation_selection_import_and_later_connect(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    assert [window.pages.tabText(i) for i in range(2)] == ["Hauptseite", "Autotune-Einstellungen"]
    result = calculate(window, wait, tmp_path)
    assert window.pages.currentWidget() is window.main_page
    assert window.autotune_results.preview.candidate is result.candidates[0]
    assert window.import_button.isEnabled()
    window.autotune_results.table.selectRow(1)
    selected = result.candidates[1]
    assert selected.mount.array_index == 4
    assert window.autotune_results.preview.candidate is selected
    window.open_autotune_settings()
    window.pages.setCurrentWidget(window.main_page)
    window.output_tabs.setCurrentIndex(1)
    window.output_tabs.setCurrentIndex(0)
    assert window.autotune_settings.model_result is result
    assert window.autotune_results.selected_candidate() is selected
    window.editor.speed.setValue(80)
    before = window.editor.parameters()
    expected = candidate_parameters(before, selected)
    window.import_button.click()
    assert window.editor.parameters() == expected
    assert window.variants == [{"name": "Ausgangswerte", "parameters": expected}]
    assert window.editing == 0 and window.variant_list.currentRow() == 0
    assert not services.calls
    window.worker.submit("connect")
    wait(lambda: window.state == "ready")
    assert window.editor.parameters() == expected
    assert window.variants[0]["parameters"] == expected
    assert not any(method == "PUT" for method, _, _ in services.calls)
    assert not errors


def test_current_variant_and_editor_changes_are_preserved(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    result = calculate(window, wait, tmp_path)
    window.editor.speed.setValue(80)
    window.add_variant()
    window.add_variant()
    untouched = copy.deepcopy(window.variants[0])
    window.editor.rows[1]["delay_ms"].setValue(91)
    window.editor.rows[2]["pressure_mbar"].setValue(987)
    before = window.editor.parameters()
    expected = candidate_parameters(before, result.candidates[0])
    window.import_button.click()
    assert window.variants[0] == untouched
    assert len(window.variants) == 2
    assert window.editing == 1
    assert window.variants[1]["parameters"] == expected == window.editor.parameters()
    assert not services.calls and not errors


@pytest.mark.parametrize("reason", ["zero_speed", "pressure", "offset", "blocked", "stale"])
def test_import_failure_is_atomic(offline_window, tmp_path, reason):
    window, services, errors, wait = offline_window
    result = calculate(window, wait, tmp_path)
    if reason != "zero_speed":
        window.editor.speed.setValue(80)
    if reason == "pressure":
        show_candidate(window, result, candidate(pressure=6000.001))
    elif reason == "offset":
        show_candidate(window, result, candidate(offset=5000.001))
    elif reason == "blocked":
        show_candidate(window, result, candidate(status="blocked"))
    elif reason == "stale":
        window.editor.rows[1]["pulse_duration_ms"].setValue(300)
    before = copy.deepcopy(window.editor.parameters())
    variants = copy.deepcopy(window.variants)
    window.import_autotune_result()
    assert "Übernahme nicht möglich" in window.autotune_status.text()
    if reason == "zero_speed":
        assert "Bandgeschwindigkeit größer als 0" in window.autotune_status.text()
    assert window.editor.parameters() == before and window.variants == variants
    assert not services.calls and not errors


def test_warning_candidate_and_selection_and_trial_lock(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    result = calculate(window, wait, tmp_path)
    window.editor.speed.setValue(80)
    show_candidate(window, result, candidate(pressure=4000, status="outside_model"))
    assert window.import_button.isEnabled()
    window.import_button.click()
    assert window.editor.parameters()["arrays"][1]["pressure_mbar"] == 4000
    assert "Strömungswarnung" in window.autotune_status.text()
    assert "Strömungswarnung" in window.autotune_results.details.toPlainText()
    window.autotune_results.table.clearSelection()
    assert not window.import_button.isEnabled()
    assert window.autotune_results.preview.candidate is None
    window.autotune_results.table.selectRow(0)
    before = window.editor.parameters()
    generation = window.autotune_settings.model_generation
    window.handle_event("state", "waiting")
    assert not window.calculate_button.isEnabled()
    assert not window.import_button.isEnabled()
    assert not window.autotune_settings.isEnabled()
    window.calculate_autotune()
    window.import_autotune_result()
    assert window.autotune_settings.model_generation == generation
    assert window.editor.parameters() == before
    window.handle_event("state", "disconnected")
    assert window.import_button.isEnabled()
    assert not services.calls and not errors


@pytest.mark.parametrize("change", ["density", "unit", "mesh", "pose", "model", "mount", "pulse"])
def test_settings_changes_invalidate_results(offline_window, tmp_path, change):
    window, _, _, wait = offline_window
    result = calculate(window, wait, tmp_path)
    settings = window.autotune_settings
    generation = settings.model_generation
    if change == "density":
        settings.density.setText("1200")
    elif change == "unit":
        settings.unit.setCurrentText("cm")
    elif change == "mesh":
        settings.set_mesh(tmp_path / "missing.stl")
    elif change == "pose":
        settings.target.setText("0, 0, 0, 1")
        settings.pose_edited()
    elif change == "model":
        settings.model_panel.fields["drag_coefficient"][0].setValue(1.1)
    elif change == "mount":
        settings.model_panel.mount_rows[0][3].setValue(30)
    else:
        window.editor.rows[0]["pulse_duration_ms"].setValue(200)
    assert settings.model_result is None
    assert not window.import_button.isEnabled()
    settings.accept_model_result(generation, result, "")
    assert settings.model_result is None
