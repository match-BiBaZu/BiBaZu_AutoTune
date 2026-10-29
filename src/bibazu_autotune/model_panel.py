"""Analytical settings with explicitly saved startup defaults."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .analytical_model import ModelConfiguration
from .model_defaults import ModelDefaults, ModelDefaultsStore, validate_density


class ModelPanel(QTabWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None, defaults_path=None):
        super().__init__(parent)
        self.fields = {}
        self.mount_rows = []
        self.defaults_store = ModelDefaultsStore(defaults_path)
        warning = ""
        try:
            defaults = self.defaults_store.load()
        except (OSError, ValueError) as exc:
            defaults = ModelDefaults()
            warning = "Gespeicherte Standardwerte konnten nicht geladen werden: " + str(exc)
        config = defaults.configuration
        settings = QWidget()
        form = QFormLayout(settings)
        self.density = QLineEdit(f"{defaults.density_kg_m3:g}")
        self.density.setPlaceholderText("Standard 1200; homogene Massenverteilung")
        form.addRow("Materialdichte [kg/m³]", self.density)
        pressure_hint = QLabel(
            "0,05–0,80 bar Überdruck ist der Referenzbereich des Ausgangsmodells. "
            "Die SPS erlaubt 0–6 bar. Berechnete Werte außerhalb des Referenzbereichs "
            "werden weiterhin angezeigt und gekennzeichnet. Die Grenze für nicht "
            "abgeschnürte Strömung wird separat geprüft."
        )
        pressure_hint.setWordWrap(True)
        form.addRow(pressure_hint)
        self.count = QSpinBox()
        self.count.setRange(1, 256)
        self.count.setValue(config.opening_count)
        form.addRow("Öffnungen je schaltbarer Düse", self.count)
        specs = [
            ("diameter_m", "Einzelöffnungsdurchmesser [mm]", 1000, 0.001, 100),
            ("pitch_m", "Öffnungs-Mittenabstand [mm]", 1000, 0, 1000),
            ("distance_m", "Düse–Bauteil-Abstand [mm]", 1000, 0.001, 10000),
            ("spread_angle_deg", "Strahlöffnungswinkel [°]", 1, 0, 179),
            ("margin_m", "Zusätzlicher Randabstand [mm]", 1000, 0, 1000),
            ("half_hit_lower", "Gewicht 0,5 ab Überdeckung [%]", 100, 0.001, 50),
            ("half_hit_upper", "Gewicht 1 ab Überdeckung [%]", 100, 50, 99.999),
            ("drag_coefficient", "Widerstandsbeiwert c_d", 1, 0.001, 100),
            ("mesh_coefficient", "Mesh-Koeffizient c_m", 1, 0.001, 100),
            ("concentricity_coefficient", "Konzentrizitätsfaktor c_c", 1, 0.001, 100),
            ("kappa", "Isentropenexponent κ", 1, 1.001, 5),
            ("ambient_pa", "Umgebungsdruck absolut [bar]", 1e-5, 0.001, 100),
            ("gravity_m_s2", "Schwerebeschleunigung [m/s²]", 1, 0.001, 100),
            ("uphill_deg", "X-Neigung, positiv bergauf [°]", 1, -89, 89),
            ("cross_slope_deg", "Querneigung [°]", 1, 0, 90),
            ("contact_tolerance_m", "Kontakt-Toleranz [mm]", 1000, 0.00001, 1),
        ]
        for name, label, scale, low, high in specs:
            control = QDoubleSpinBox()
            control.setDecimals(6)
            control.setRange(low, high)
            control.setValue(getattr(config, name) * scale)
            self.fields[name] = (control, scale)
            form.addRow(label, control)
        self.outer_width = QDoubleSpinBox()
        self.outer_width.setDecimals(6)
        self.outer_width.setRange(0.001, 100000)
        self.outer_width.setValue(config.outer_width_m * 1000)
        form.insertRow(3, "Außenbreite Lochreihe [mm] (vorläufig 24)", self.outer_width)
        self.manual_axis = QComboBox()
        self.manual_axis.addItems(["Automatisch aus Posen/Roadmap", "X", "Y", "Z"])
        self.manual_angle = QDoubleSpinBox()
        self.manual_angle.setRange(-360, 360)
        self.manual_angle.setValue(90)
        form.addRow(QLabel("Manuelle Drehung: gilt nur für den aktuellen Übergang."))
        form.addRow("Manuelle Achse bei mehrdeutigen Posen", self.manual_axis)
        form.addRow("Manueller Drehwinkel mit Vorzeichen [°]", self.manual_angle)
        form.addRow(
            QLabel(
                "Auswertung am Impulsbeginn; Bewegung während des Impulses bleibt unberücksichtigt."
            )
        )
        scroll = QScrollArea()
        self.save_parameters_button = QPushButton("Aktuelle Modellparameter als Standard speichern")
        self.save_parameters_button.clicked.connect(self.save_parameters_defaults)
        self.parameters_status = QLabel(warning)
        self.parameters_status.setWordWrap(True)
        scroll.setWidgetResizable(True)
        scroll.setWidget(settings)
        parameters_page = QWidget()
        parameters_layout = QVBoxLayout(parameters_page)
        parameters_layout.addWidget(scroll)
        parameters_layout.addWidget(self.save_parameters_button)
        parameters_layout.addWidget(self.parameters_status)
        self.addTab(parameters_page, "Modellparameter")
        self.mount_table = QTableWidget(8, 5)
        self.mount_table.setHorizontalHeaderLabels(
            [
                "Array",
                "Düse",
                "Kipp-/Reihenachse",
                "Strahl normal",
                "Reihenmitte ab Rinnenkante [mm]",
            ]
        )
        for row, mount in enumerate(config.mounts):
            for col, text in enumerate((mount.array_index, mount.nozzle_index)):
                item = QTableWidgetItem(str(text))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.mount_table.setItem(row, col, item)
            axis, normal, center = QComboBox(), QComboBox(), QDoubleSpinBox()
            axis.addItems(["X", "Y", "Z"])
            axis.setCurrentText(mount.rotation_axis.upper())
            normal.addItems(["+Y", "+Z"])
            normal.setCurrentText("+" + mount.normal_axis.upper())
            center.setDecimals(4)
            center.setRange(-0.0001, 5000)
            center.setSpecialValueText("Nicht angegeben")
            center.setValue(
                mount.row_center_m * 1000 if mount.row_center_m is not None else -0.0001
            )
            for col, control in enumerate((axis, normal, center), 2):
                self.mount_table.setCellWidget(row, col, control)
            self.mount_rows.append((mount, axis, normal, center))
            axis.currentTextChanged.connect(self.changed)
            normal.currentTextChanged.connect(self.changed)
            center.valueChanged.connect(self.changed)
        self.mount_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        montage = QWidget()
        montage_layout = QVBoxLayout(montage)
        montage_layout.addWidget(self.mount_table)
        self.save_mounts_button = QPushButton("Aktuelle Montagewerte als Standard speichern")
        self.save_mounts_button.clicked.connect(self.save_mounts_defaults)
        montage_layout.addWidget(self.save_mounts_button)
        self.mounts_status = QLabel(warning)
        self.mounts_status.setWordWrap(True)
        montage_layout.addWidget(self.mounts_status)
        self.addTab(montage, "Montage je Array/Düse")
        self._linking = False
        self.count.valueChanged.connect(self.link_pitch)
        self.fields["diameter_m"][0].valueChanged.connect(self.link_pitch)
        self.fields["pitch_m"][0].valueChanged.connect(self.link_pitch)
        self.outer_width.valueChanged.connect(self.link_width)
        for control, _ in self.fields.values():
            control.valueChanged.connect(self.changed)
        self.count.valueChanged.connect(self.changed)
        self.manual_axis.currentTextChanged.connect(self.changed)
        self.manual_angle.valueChanged.connect(self.changed)

    def link_pitch(self):
        if self._linking:
            return
        self._linking = True
        diameter = self.fields["diameter_m"][0].value()
        pitch = self.fields["pitch_m"][0].value()
        self.outer_width.setValue(diameter + (self.count.value() - 1) * pitch)
        self.outer_width.setReadOnly(self.count.value() == 1)
        self._linking = False

    def link_width(self):
        if self._linking or self.count.value() == 1:
            return
        self._linking = True
        diameter = self.fields["diameter_m"][0].value()
        self.fields["pitch_m"][0].setValue(
            (self.outer_width.value() - diameter) / (self.count.value() - 1)
        )
        self._linking = False
        self.changed.emit()

    def parameter_configuration(self) -> ModelConfiguration:
        values = {name: control.value() / scale for name, (control, scale) in self.fields.items()}
        config = ModelConfiguration(**values, opening_count=self.count.value())
        config.validate()
        return config

    def current_mounts(self):
        return tuple(
            replace(
                mount,
                rotation_axis=axis.currentText().lower(),
                normal_axis=normal.currentText()[-1].lower(),
                row_center_m=center.value() / 1000 if center.value() >= 0 else None,
            )
            for mount, axis, normal, center in self.mount_rows
        )

    def configuration(self) -> ModelConfiguration:
        config = replace(
            self.parameter_configuration(),
            mounts=self.current_mounts(),
            manual_axis=None
            if self.manual_axis.currentIndex() == 0
            else self.manual_axis.currentText().lower(),
            manual_angle_deg=self.manual_angle.value(),
        )
        config.validate()
        return config

    def density_value(self):
        try:
            value = float(self.density.text().strip().replace(",", "."))
        except ValueError as exc:
            raise ValueError("Dichte muss eine positive, endliche Zahl in kg/m³ sein.") from exc
        return validate_density(value)

    def save_parameters_defaults(self):
        try:
            self.defaults_store.save_parameters(
                self.parameter_configuration(), self.density_value()
            )
        except (OSError, ValueError) as exc:
            self.parameters_status.setText("Standardwerte nicht gespeichert: " + str(exc))
        else:
            self.parameters_status.setText(
                "Modellparameter einschließlich Dichte als Standard gespeichert."
            )

    def save_mounts_defaults(self):
        try:
            self.defaults_store.save_mounts(self.current_mounts())
        except (OSError, ValueError) as exc:
            self.mounts_status.setText("Standardwerte nicht gespeichert: " + str(exc))
        else:
            self.mounts_status.setText(
                "Reihenmitten und Montagezuordnung als Standard gespeichert."
            )
