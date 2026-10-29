import json
from dataclasses import replace

import pytest
from test_geometry import write_box
from test_gui import offline_window as offline_window

from bibazu_autotune import model_defaults
from bibazu_autotune.analytical_model import ModelConfiguration
from bibazu_autotune.model_defaults import ModelDefaultsStore
from bibazu_autotune.model_panel import ModelPanel
from bibazu_autotune.preparation import AutotuneSettingsPage


def test_missing_file_uses_1200_and_does_not_create_settings(tmp_path):
    path = tmp_path / "settings/defaults.json"
    defaults = ModelDefaultsStore(path).load()
    assert defaults.density_kg_m3 == 1200
    assert defaults.configuration == ModelConfiguration()
    assert not path.exists()


def test_old_defaults_load_new_hit_thresholds_and_save_custom_thresholds(tmp_path):
    path = tmp_path / "defaults.json"
    path.write_text(
        json.dumps({"schema_version": 1, "model_parameters": {"drag_coefficient": 0.8}}),
        "utf-8",
    )
    store = ModelDefaultsStore(path)
    config = store.load().configuration
    assert (config.half_hit_lower, config.half_hit_upper) == (0.25, 0.75)
    store.save_parameters(replace(config, half_hit_lower=0.4, half_hit_upper=0.6), 1200)
    loaded = store.load().configuration
    assert (loaded.half_hit_lower, loaded.half_hit_upper) == (0.4, 0.6)
    assert loaded.drag_coefficient == 0.8


def test_parameters_and_mounts_are_saved_independently(tmp_path):
    store = ModelDefaultsStore(tmp_path / "settings/defaults.json")
    config = ModelConfiguration(
        drag_coefficient=0.8, contact_tolerance_m=0.00005, manual_axis="y", manual_angle_deg=-180
    )
    store.save_parameters(config, 1250)
    assert all(m.row_center_m is None for m in store.load().configuration.mounts)
    mounts = tuple(
        replace(m, row_center_m=0.0123 if m.nozzle_index == 1 else None) for m in config.mounts
    )
    store.save_mounts(mounts)
    loaded = ModelDefaultsStore(store.path).load()
    assert loaded.configuration.drag_coefficient == 0.8
    assert loaded.configuration.contact_tolerance_m == 0.00005
    assert loaded.density_kg_m3 == 1250
    assert loaded.configuration.mounts == mounts
    assert loaded.configuration.manual_axis is None
    assert loaded.configuration.manual_angle_deg == 90
    assert "manual_axis" not in store.path.read_text("utf-8")
    store.save_parameters(replace(config, drag_coefficient=0.9), 1200)
    assert store.load().configuration.mounts == mounts
    assert store.load().density_kg_m3 == 1200


@pytest.mark.parametrize("density", [0, -1, float("nan"), float("inf"), True, "1200"])
def test_invalid_density_never_overwrites_saved_defaults(tmp_path, density):
    store = ModelDefaultsStore(tmp_path / "defaults.json")
    store.save_parameters(ModelConfiguration(), 1200)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="Dichte"):
        store.save_parameters(ModelConfiguration(), density)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        '{"schema_version":2}',
        '{"schema_version":1,"model_parameters":{"distance_m":"bad"}}',
        '{"schema_version":1,"model_parameters":{"density_kg_m3":-1}}',
        '{"schema_version":1,"mounts":[]}',
    ],
)
def test_invalid_defaults_are_not_silently_replaced(tmp_path, contents):
    path = tmp_path / "defaults.json"
    path.write_text(contents, "utf-8")
    store = ModelDefaultsStore(path)
    with pytest.raises(ValueError):
        store.load()
    with pytest.raises(ValueError):
        store.save_parameters(ModelConfiguration(), 1200)
    assert path.read_text("utf-8") == contents


def test_atomic_write_failure_preserves_previous_values(tmp_path, monkeypatch):
    store = ModelDefaultsStore(tmp_path / "defaults.json")
    store.save_parameters(ModelConfiguration(), 1200)
    before = store.path.read_bytes()

    def fail(*args):
        raise OSError("Read-only settings")

    monkeypatch.setattr(model_defaults.os, "replace", fail)
    with pytest.raises(OSError, match="Read-only"):
        store.save_parameters(ModelConfiguration(), 1300)
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_gui_density_location_initial_mass_and_restart_defaults(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    dialog, panel = window.autotune_settings, window.autotune_settings.model_panel
    assert panel.density.text() == "1200"
    assert panel.isAncestorOf(dialog.density)
    assert not dialog.pages.widget(0).isAncestorOf(dialog.density)
    source = write_box(tmp_path / "box.stl")
    dialog.set_mesh(source)
    wait(lambda: dialog.analysis is not None)
    assert dialog.analysis.material.density_kg_m3 == 1200
    assert dialog.analysis.material.mass_kg == pytest.approx(0.0288)
    panel.density.setText("1250")
    wait(lambda: dialog.analysis is not None)
    panel.fields["drag_coefficient"][0].setValue(0.8)
    panel.save_parameters_button.click()
    assert "als Standard gespeichert" in panel.parameters_status.text()
    panel.mount_rows[2][3].setValue(12)
    panel.save_mounts_button.click()
    assert "als Standard gespeichert" in panel.mounts_status.text()
    # Changing a value without saving must not replace the selected startup default.
    panel.fields["drag_coefficient"][0].setValue(0.9)
    panel.mount_rows[6][3].setValue(14)
    panel.save_mounts_button.click()
    reopened = AutotuneSettingsPage(window.editor.parameters, window)
    try:
        assert reopened.density.text() == "1250"
        assert reopened.model_panel.configuration().drag_coefficient == 0.8
        assert reopened.model_panel.mount_rows[2][3].value() == 12
        assert reopened.model_panel.mount_rows[6][3].value() == 14
        reopened.set_mesh(source)
        wait(lambda: reopened.analysis is not None)
        assert reopened.analysis.material.mass_kg == pytest.approx(0.03)
    finally:
        reopened.stop()
        wait(lambda: not reopened.workers_running())
        reopened.deleteLater()
    assert not services.calls
    assert not errors


def test_save_buttons_validate_only_their_own_section(offline_window):
    window, services, _, _ = offline_window
    panel = window.autotune_settings.model_panel
    panel.density.setText("nan")
    panel.mount_rows[0][3].setValue(12)
    panel.save_mounts_button.click()
    assert "als Standard gespeichert" in panel.mounts_status.text()
    panel.save_parameters_button.click()
    assert "nicht gespeichert" in panel.parameters_status.text()
    saved = json.loads(panel.defaults_store.path.read_text("utf-8"))
    assert "model_parameters" not in saved
    panel.density.setText("1200")
    # An invalid unsaved mount must not block saving valid physical parameters.
    panel.mount_rows[0][1].setCurrentText("Y")  # Parallel to the +Y jet: invalid.
    panel.save_parameters_button.click()
    assert "als Standard gespeichert" in panel.parameters_status.text()
    assert panel.defaults_store.load().configuration.mounts[0].rotation_axis == "z"
    assert not services.calls


def test_corrupt_defaults_fall_back_visibly_in_gui(offline_window, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("broken", "utf-8")
    panel = ModelPanel(defaults_path=path)
    assert panel.density.text() == "1200"
    assert "nicht geladen" in panel.parameters_status.text()
    assert "nicht geladen" in panel.mounts_status.text()
    panel.save_parameters_button.click()
    assert "nicht gespeichert" in panel.parameters_status.text()
    assert path.read_text("utf-8") == "broken"
    panel.deleteLater()
