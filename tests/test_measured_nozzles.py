from dataclasses import replace

import pytest
from test_analytical_model import pose
from test_engine import parameters
from test_gui import offline_window as offline_window
from test_nozzle_groups import box_analysis

from bibazu_autotune.analytical_model import AnalyticalModel, ModelConfiguration
from bibazu_autotune.model_defaults import ModelDefaultsStore


def test_factory_settings_match_measured_nozzle_and_block_dimensions():
    config = ModelConfiguration()
    assert config.opening_count == 8
    assert config.diameter_m / 2 * 1000 == pytest.approx(0.4)
    assert config.pitch_m * 1000 == pytest.approx(2.8)
    assert config.center_span_m * 1000 == pytest.approx(19.6)
    assert config.outer_width_m * 1000 == pytest.approx(20.4)
    for array in range(1, 5):
        mounts = [m for m in config.mounts if m.array_index == array]
        assert [m.row_center_m * 1000 for m in mounts] == pytest.approx(
            [12.44, 37.32, 62.20, 12.44, 12.44, 12.44]
        )
        assert [m.longitudinal_offset_m * 1000 for m in mounts] == pytest.approx(
            [0, 0, 0, 0, 24.88, 49.76]
        )
        first = mounts[0].row_center_m - config.center_span_m / 2
        last = mounts[0].row_center_m + config.center_span_m / 2
        next_first = mounts[1].row_center_m - config.center_span_m / 2
        assert first * 1000 == pytest.approx(2.64)
        assert last * 1000 == pytest.approx(22.24)
        assert (next_first - last) * 1000 == pytest.approx(5.28)


@pytest.mark.parametrize("width,active", [(20, (1,)), (44, (1, 2)), (68, (1, 2, 3))])
def test_measured_montage_automatically_selects_needed_nozzles(tmp_path, width, active):
    config = replace(ModelConfiguration(), uphill_deg=0, cross_slope_deg=0)
    analysis = box_analysis(tmp_path, width)
    result = AnalyticalModel().evaluate(analysis, pose("y", -90), parameters(), config)
    assert len(result) == 2
    assert all(c.pressure is not None for c in result)
    assert all(c.impact.active_nozzles == active for c in result)


def test_gui_shows_measured_aperture_span_separately_from_outer_width(offline_window):
    window, services, errors, _ = offline_window
    panel = window.autotune_settings.model_panel
    assert panel.fields["diameter_m"][0].value() == 0.8
    assert panel.fields["pitch_m"][0].value() == 2.8
    assert panel.outer_width.value() == pytest.approx(20.4)
    assert float(panel.center_span.text()) == pytest.approx(19.6)
    for mount, _, _, center in panel.mount_rows:
        expected = [12.44, 37.32, 62.20, 12.44, 12.44, 12.44][mount.nozzle_index - 1]
        assert center.value() == pytest.approx(expected)
        if mount.nozzle_index >= 4:
            value = panel.longitudinal_fields[(mount.array_index, mount.nozzle_index)].text()
            assert float(value) == pytest.approx([0, 24.88, 49.76][mount.nozzle_index - 4])
    panel.fields["pitch_m"][0].setValue(3)
    assert float(panel.center_span.text()) == 21
    assert panel.outer_width.value() == pytest.approx(21.8)
    panel.outer_width.setValue(20.4)
    assert panel.fields["pitch_m"][0].value() == 2.8
    assert float(panel.center_span.text()) == pytest.approx(19.6)
    panel.count.setValue(1)
    assert panel.outer_width.value() == pytest.approx(0.8)
    assert panel.center_span.text() == "0"
    assert not services.calls and not errors


def test_explicit_saved_geometry_overrides_factory_values_without_load_side_effects(tmp_path):
    store = ModelDefaultsStore(tmp_path / "defaults.json")
    saved = replace(ModelConfiguration(), diameter_m=0.001, pitch_m=0.003)
    store.save_parameters(saved, 1200)
    before = store.path.read_bytes()
    loaded = store.load().configuration
    assert loaded.diameter_m == 0.001 and loaded.pitch_m == 0.003
    assert store.path.read_bytes() == before
