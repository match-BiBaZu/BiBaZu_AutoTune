import os
from pathlib import Path

from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication
from test_gui import offline_window as offline_window
from test_model_gui import prepare


def test_parameter_sections_show_their_controls_without_changing_values(offline_window):
    window, services, errors, _ = offline_window
    settings = window.autotune_settings
    panel = settings.model_panel
    window.resize(1440, 950)
    window.open_autotune_settings()
    settings.pages.setCurrentWidget(panel)
    assert settings.pages.tabText(1) == "Modell und Anlage"
    assert [panel.navigation.item(i).text() for i in range(panel.navigation.count())] == [
        "Material und Kippen",
        "Düsen und Luftstrahl",
        "Treffer und Offset",
        "Analytischer Druck",
        "SPS-Druckskalierung",
        "Montage je Array/Düse",
    ]
    controls = {
        "material": [panel.density]
        + [
            panel.fields[name][0]
            for name in ("gravity_m_s2", "uphill_deg", "cross_slope_deg", "contact_tolerance_m")
        ],
        "nozzles": [panel.count, panel.outer_width, panel.center_span]
        + [
            panel.fields[name][0]
            for name in ("diameter_m", "pitch_m", "distance_m", "spread_angle_deg")
        ],
        "hits": [
            panel.fields[name][0]
            for name in ("margin_m", "offset_safety_m", "half_hit_lower", "half_hit_upper")
        ],
        "pressure": [
            panel.fields[name][0]
            for name in (
                "drag_coefficient",
                "mesh_coefficient",
                "concentricity_coefficient",
                "kappa",
                "ambient_pa",
            )
        ],
        "scaling": [panel.fields[name][0] for name in ("pressure_scale_a", "pressure_scale_b_pa")],
        "mounts": [panel.mount_table, panel.mount_filter, panel.save_mounts_button],
    }
    assert {control for group in controls.values() for control in group} >= {
        control for control, _ in panel.fields.values()
    }
    original = panel.configuration()
    generation = settings.model_generation
    changes = QSignalSpy(panel.changed)
    destination = os.environ.get("BIBAZU_GUI_ARTIFACTS")
    if destination:
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
        Path(destination).mkdir(parents=True, exist_ok=True)
    for index, key in enumerate(panel.section_keys):
        panel.navigation.setCurrentRow(index)
        QApplication.processEvents()
        page = panel.section_pages[key]
        assert page.isVisible()
        for control in controls[key]:
            assert page.isAncestorOf(control) and control.isVisible()
        assert panel.parameters_footer.isVisible() == (key != "mounts")
        assert panel.configuration() == original
        if destination:
            assert panel.grab().save(str(Path(destination) / f"settings-{key}.png"))
    assert len(changes) == 0 and settings.model_generation == generation
    assert not panel.defaults_store.path.exists()
    assert not services.calls and not errors


def test_navigation_and_array_filter_keep_results_and_edits(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    settings, panel = prepare(window, wait, tmp_path)
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    result = settings.model_result
    generation = settings.model_generation
    window.open_autotune_settings()
    settings.pages.setCurrentWidget(panel)
    original = panel.configuration()
    for index in range(panel.navigation.count()):
        panel.navigation.setCurrentRow(index)
    panel.mount_filter.setCurrentIndex(2)  # Array 2, six of 24 existing rows.
    assert [
        panel.mount_table.item(row, 0).text()
        for row in range(24)
        if not panel.mount_table.isRowHidden(row)
    ] == ["2"] * 6
    assert settings.model_result is result and settings.model_generation == generation
    assert panel.configuration() == original
    panel.mount_filter.setCurrentIndex(0)
    assert not any(panel.mount_table.isRowHidden(row) for row in range(24))
    panel.navigation.setCurrentRow(1)
    panel.outer_width.setValue(28.7)
    assert settings.model_result is None  # Actual edits still invalidate results.
    assert abs(panel.configuration().pitch_m - 0.004) < 1e-9
    panel.navigation.setCurrentRow(4)
    window.pages.setCurrentWidget(window.main_page)
    window.open_autotune_settings()
    assert panel.navigation.currentRow() == 4
    panel.navigation.setCurrentRow(1)
    assert panel.outer_width.value() == 28.7
    assert not services.calls and not errors
