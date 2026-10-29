import os
from pathlib import Path

import pytest
import trimesh
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QAbstractItemView, QApplication
from test_geometry import write_box
from test_gui import offline_window as offline_window


def values(panel):
    table = panel.properties
    return {
        table.item(row, 0).text(): [table.item(row, column).text() for column in range(1, 5)]
        for row in range(table.rowCount())
    }


def test_geometry_inspection_table_tensors_and_copy(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    settings = window.autotune_settings
    panel = settings.geometry_panel
    window.open_autotune_settings()
    settings.pages.setCurrentWidget(panel)
    assert panel.isVisible()
    assert not panel.copy_button.isEnabled()
    path = write_box(tmp_path / "box.stl", translation=(100, -200, 300))
    settings.density.setText("2700")
    settings.set_mesh(path)
    wait(lambda: settings.analysis is not None)
    rows = values(panel)
    for axis, dimension in zip("XYZ", (20, 30, 40), strict=True):
        assert rows[f"Abmessung {axis}"] == [str(dimension), "mm", str(dimension / 1000), "m"]
    assert rows["Oberfläche"] == ["5200", "mm²", "0.0052", "m²"]
    assert rows["Volumen"] == ["24000", "mm³", "2.4e-05", "m³"]
    for axis, position in zip("XYZ", (100, -200, 300), strict=True):
        assert float(rows[f"Volumenschwerpunkt {axis}"][0]) == pytest.approx(position)
    assert rows["Materialdichte"][0:2] == ["2700", "kg/m³"]
    assert rows["Masse"] == ["64.8", "g", "0.0648", "kg"]
    assert float(panel.geometric_inertia.item(0, 0).text()) == pytest.approx(5e-9)
    assert float(panel.physical_inertia.item(0, 0).text()) == pytest.approx(1.35e-5)
    assert panel.properties.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert str(path.resolve()) in panel.source.text()
    assert settings.analysis.sha256 in panel.source.text()
    assert "Dreiecke: 12" in panel.source.text()
    panel.copy_button.click()
    report = QApplication.clipboard().text()
    assert "Abmessung X\t20\tmm\t0.02\tm" in report
    assert "Geometrischer Trägheitstensor [m⁵]" in report
    assert "Achse\tX\tY\tZ" in report
    assert "Physikalischer Trägheitstensor [kg·m²]" in report
    assert "vor der Pose- und Auflagetransformation" in report
    window.pages.setCurrentWidget(window.main_page)
    window.open_autotune_settings()
    assert settings.pages.currentWidget() is panel
    assert values(panel) == rows
    assert not services.calls and not errors
    if os.environ.get("BIBAZU_GUI_ARTIFACTS"):
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 9))
        destination = Path(os.environ["BIBAZU_GUI_ARTIFACTS"])
        destination.mkdir(parents=True, exist_ok=True)
        QApplication.processEvents()
        assert window.grab().save(str(destination / "geometry.png"))


def test_geometry_view_updates_density_and_unit_and_clears_pending_values(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    settings = window.autotune_settings
    panel = settings.geometry_panel
    settings.set_mesh(write_box(tmp_path / "box.stl"))
    wait(lambda: settings.analysis is not None)
    assert values(panel)["Masse"][0] == "28.8"
    settings.density.setText("2400")
    assert panel.analysis is None
    assert panel.properties.rowCount() == 0
    assert not panel.copy_button.isEnabled()
    wait(lambda: settings.analysis is not None)
    assert values(panel)["Masse"][0] == "57.6"
    settings.unit.setCurrentText("cm")
    assert panel.analysis is None
    wait(lambda: settings.analysis is not None)
    assert values(panel)["Abmessung X"][0] == "200"
    assert values(panel)["Masse"][0] == "57600"
    assert "Importeinheit: cm" in panel.source.text()
    settings.density.setText("ungültig")
    assert panel.analysis is None and not panel.copy_button.isEnabled()
    assert "Dichte" in panel.status.text()
    assert panel.physical_inertia.item(0, 0).text() == "—"
    assert not services.calls and not errors


def test_open_mesh_and_missing_file_show_unavailable_values(offline_window, tmp_path):
    window, services, errors, wait = offline_window
    settings = window.autotune_settings
    panel = settings.geometry_panel
    mesh = trimesh.creation.box(extents=[20, 30, 40])
    mesh.update_faces([False] + [True] * 11)
    path = tmp_path / "open.stl"
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    settings.set_mesh(path)
    wait(lambda: settings.analysis is not None)
    rows = values(panel)
    assert float(rows["Oberfläche"][0]) > 0
    for name in ("Volumen", "Volumenschwerpunkt X", "Masse"):
        assert rows[name][0] == rows[name][2] == "nicht verfügbar"
    assert panel.geometric_inertia.item(0, 0).text() == "—"
    assert panel.physical_inertia.item(0, 0).text() == "—"
    assert "offen oder inkonsistent" in panel.status.text()
    generation, old_analysis = settings.generation, settings.analysis
    settings.set_mesh(tmp_path / "missing.stl")
    wait(lambda: "missing.stl" in panel.status.text())
    settings.accept_analysis(generation, old_analysis, "")
    assert panel.analysis is None and panel.properties.rowCount() == 0
    assert not panel.copy_button.isEnabled()
    assert not services.calls and not errors
