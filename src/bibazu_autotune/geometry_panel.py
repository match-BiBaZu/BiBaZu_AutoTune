"""Read-only STL properties in practical and SI units for inspection."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .geometry import MeshAnalysis


class GeometryPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        layout = QVBoxLayout(self)
        self.source = QLabel()
        self.source.setWordWrap(True)
        self.source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.source)
        self.reference = QLabel(
            "Bezug: ursprüngliches STL-Koordinatensystem vor der Pose- und Auflagetransformation. "
            "Die Trägheitstensoren beziehen sich auf den Volumenschwerpunkt und die STL-Achsen. "
            "Masse und physikalische Trägheit setzen eine homogene Materialdichte voraus."
        )
        self.reference.setWordWrap(True)
        layout.addWidget(self.reference)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.properties = QTableWidget(0, 5)
        self.properties.setHorizontalHeaderLabels(
            ["Parameter", "Wert", "Einheit", "SI-Wert", "SI-Einheit"]
        )
        self.properties.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.configure_table(self.properties)
        layout.addWidget(self.properties, 2)
        tensors = QHBoxLayout()
        self.geometric_inertia = self.tensor_table(tensors, "Geometrischer Trägheitstensor [m⁵]")
        self.physical_inertia = self.tensor_table(tensors, "Physikalischer Trägheitstensor [kg·m²]")
        layout.addLayout(tensors, 1)
        self.copy_button = QPushButton("Geometriewerte kopieren")
        self.copy_button.setToolTip("Werte mit Einheiten als Text in die Zwischenablage kopieren.")
        self.copy_button.clicked.connect(self.copy_values)
        layout.addWidget(self.copy_button)
        self.set_analysis(None, "STL auswählen, um die geometrischen Parameter zu prüfen.")

    @staticmethod
    def configure_table(table):
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)

    def tensor_table(self, layout, title):
        group = QGroupBox(title)
        group_layout = QVBoxLayout(group)
        table = QTableWidget(3, 3)
        table.setHorizontalHeaderLabels(["X", "Y", "Z"])
        table.setVerticalHeaderLabels(["X", "Y", "Z"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.configure_table(table)
        group_layout.addWidget(table)
        layout.addWidget(group)
        return table

    @staticmethod
    def number(value):
        return "nicht verfügbar" if value is None else f"{value:.10g}"

    def set_analysis(self, analysis: MeshAnalysis | None, message=""):
        self.analysis = analysis
        self.copy_button.setEnabled(analysis is not None)
        self.properties.setRowCount(0)
        for table in (self.geometric_inertia, self.physical_inertia):
            for row in range(3):
                for column in range(3):
                    table.setItem(row, column, QTableWidgetItem("—"))
        if analysis is None:
            self.source.setText("Keine aktuelle STL-Auswertung verfügbar.")
            self.status.setText(message)
            return
        geometry, material = analysis.geometry, analysis.material
        self.source.setText(
            f"STL: {analysis.path}\nImporteinheit: {analysis.unit} · "
            f"Dreiecke: {geometry.triangle_count}\nSHA-256: {analysis.sha256}"
        )
        self.status.setText(
            analysis.warning or "Gültiges geschlossenes Volumen; alle Geometriewerte verfügbar."
        )
        rows = []
        for axis, value in zip("XYZ", geometry.dimensions_m, strict=True):
            rows.append((f"Abmessung {axis}", value, 1000, "mm", "m"))
        rows.extend(
            [
                ("Oberfläche", geometry.area_m2, 1e6, "mm²", "m²"),
                ("Volumen", geometry.volume_m3, 1e9, "mm³", "m³"),
            ]
        )
        for index, axis in enumerate("XYZ"):
            value = geometry.center_m[index] if geometry.center_m is not None else None
            rows.append((f"Volumenschwerpunkt {axis}", value, 1000, "mm", "m"))
        rows.extend(
            [
                ("Materialdichte", material.density_kg_m3, 1, "kg/m³", "kg/m³"),
                ("Masse", material.mass_kg, 1000, "g", "kg"),
            ]
        )
        self.properties.setRowCount(len(rows))
        for row, (name, value, scale, unit, si_unit) in enumerate(rows):
            values = [
                name,
                self.number(value * scale if value is not None else None),
                unit,
                self.number(value),
                si_unit,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.properties.setItem(row, column, item)
        for table, tensor in (
            (self.geometric_inertia, geometry.inertia_m5),
            (self.physical_inertia, material.inertia_kg_m2),
        ):
            if tensor is not None:
                for row in range(3):
                    for column in range(3):
                        item = QTableWidgetItem(self.number(tensor[row][column]))
                        item.setToolTip(repr(tensor[row][column]))
                        table.setItem(row, column, item)

    def copy_values(self):
        if self.analysis is None:
            return
        lines = [self.source.text(), self.reference.text(), self.status.text()]
        for title, table in (
            ("Geometrie und Material", self.properties),
            ("Geometrischer Trägheitstensor [m⁵]", self.geometric_inertia),
            ("Physikalischer Trägheitstensor [kg·m²]", self.physical_inertia),
        ):
            lines.extend(["", title])
            headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
            if table.verticalHeaderItem(0):
                headers.insert(0, "Achse")
            lines.append("\t".join(headers))
            for row in range(table.rowCount()):
                prefix = (
                    [table.verticalHeaderItem(row).text()] if table.verticalHeaderItem(row) else []
                )
                lines.append(
                    "\t".join(
                        prefix
                        + [table.item(row, column).text() for column in range(table.columnCount())]
                    )
                )
        QApplication.clipboard().setText("\n".join(lines))
