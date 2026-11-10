"""Analytical settings with explicitly saved startup defaults."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .analytical_model import ModelConfiguration
from .model_defaults import ModelDefaults, ModelDefaultsStore, validate_density
from .pressure_scaling import PRESSURE_SCALING_SOURCE


class ModelPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None, defaults_path=None):
        super().__init__(parent)
        self.fields = {}
        self.mount_rows = []
        self.longitudinal_fields = {}
        self.defaults_store = ModelDefaultsStore(defaults_path)
        warning = ""
        try:
            defaults = self.defaults_store.load()
        except (OSError, ValueError) as exc:
            defaults = ModelDefaults()
            warning = "Gespeicherte Standardwerte konnten nicht geladen werden: " + str(exc)
        config = defaults.configuration
        root = QVBoxLayout(self)
        body = QHBoxLayout()
        root.addLayout(body, 1)
        self.navigation = QListWidget()
        self.navigation.setMinimumWidth(210)
        self.navigation.setMaximumWidth(245)
        self.navigation.setSpacing(3)
        self.pages = QStackedWidget()
        body.addWidget(self.navigation)
        body.addWidget(self.pages, 1)
        self.section_pages = {}
        self.section_keys = []
        for key, title, description in (
            (
                "material",
                "Material und Kippen",
                "Materialdichte für Masse und Trägheit sowie Schwerkraft, Rinnenneigung "
                "und gewünschte Kippbewegung.",
            ),
            (
                "nozzles",
                "Düsen und Luftstrahl",
                "Geometrie einer schaltbaren Düse und Ausbreitung ihrer einzelnen Luftstrahlen.",
            ),
            (
                "hits",
                "Treffer und Offset",
                "Bewertung der bestrahlten Bauteilfläche und Sicherheitsabstände "
                "für den automatischen Kraftangriff.",
            ),
            (
                "pressure",
                "Analytischer Druck",
                "Strömungsparameter für die Umrechnung der erforderlichen Kippkraft "
                "in einen analytischen Druckvorschlag.",
            ),
            (
                "scaling",
                "SPS-Druckskalierung",
                "Empirische Umrechnung des analytischen Überdrucks in den Sollwert "
                "des SPS-Druckreglers.",
            ),
            (
                "mounts",
                "Montage je Array/Düse",
                "Einbaupositionen, Reihenachsen und Strahlrichtungen der sechs Düsen je Array.",
            ),
        ):
            self.add_section(key, title, description, scroll=key != "mounts")

        material = self.add_group("material", "Material und Masse")
        self.density = QLineEdit(f"{defaults.density_kg_m3:g}")
        self.density.setPlaceholderText("Standard 1200; homogene Massenverteilung")
        material.addRow("Materialdichte [kg/m³]", self.density)
        self.add_note(
            material,
            "Masse und physikalischer Trägheitstensor werden aus der STL-Geometrie "
            "mit homogener Materialdichte berechnet. Werte unter »Geometrie prüfen« vergleichen.",
        )
        forms = {
            "gravity": self.add_group("material", "Schwerkraft und Rinnenneigung"),
            "tipping": self.add_group("material", "Kippbewegung und Kontakt"),
            "openings": self.add_group("nozzles", "Lochreihe einer schaltbaren Düse"),
            "jet": self.add_group("nozzles", "Strahlausbreitung"),
            "coverage": self.add_group("hits", "Trefferbewertung"),
            "offset": self.add_group("hits", "Sicherheitsabstände für den Offset"),
            "flow": self.add_group("pressure", "Analytisches Strömungsmodell"),
            "scaling": self.add_group("scaling", "SPS-Sollwert aus analytischem Druck"),
        }
        self.count = QSpinBox()
        self.count.setRange(1, 256)
        self.count.setValue(config.opening_count)
        forms["openings"].addRow("Öffnungen je schaltbarer Düse", self.count)
        specs = [
            ("openings", "diameter_m", "Einzelöffnungsdurchmesser [mm]", 1000, 0.001, 100),
            ("openings", "pitch_m", "Öffnungs-Mittenabstand [mm]", 1000, 0, 1000),
            ("jet", "distance_m", "Düse–Bauteil-Abstand [mm]", 1000, 0.001, 10000),
            ("jet", "spread_angle_deg", "Strahlöffnungswinkel [°]", 1, 0, 179),
            ("offset", "margin_m", "Zusätzlicher Randabstand [mm]", 1000, 0, 1000),
            (
                "offset",
                "offset_safety_m",
                "Zusätzliche Offsetreserve entlang X [mm]",
                1000,
                0,
                1000,
            ),
            ("coverage", "half_hit_lower", "Gewicht 0,5 ab Überdeckung [%]", 100, 0.001, 50),
            ("coverage", "half_hit_upper", "Gewicht 1 ab Überdeckung [%]", 100, 50, 99.999),
            ("flow", "drag_coefficient", "Widerstandsbeiwert c_d", 1, 0.001, 100),
            ("flow", "mesh_coefficient", "Mesh-Koeffizient c_m", 1, 0.001, 100),
            ("flow", "concentricity_coefficient", "Konzentrizitätsfaktor c_c", 1, 0.001, 100),
            ("flow", "kappa", "Isentropenexponent κ", 1, 1.001, 5),
            ("flow", "ambient_pa", "Umgebungsdruck absolut [bar]", 1e-5, 0.001, 100),
            ("gravity", "gravity_m_s2", "Schwerebeschleunigung [m/s²]", 1, 0.001, 100),
            ("gravity", "uphill_deg", "X-Neigung, positiv bergauf [°]", 1, -89, 89),
            ("gravity", "cross_slope_deg", "Querneigung [°]", 1, 0, 90),
            ("tipping", "contact_tolerance_m", "Kontakt-Toleranz [mm]", 1000, 0.00001, 1),
        ]
        for group, name, label, scale, low, high in specs:
            control = QDoubleSpinBox()
            control.setDecimals(6)
            control.setRange(low, high)
            control.setValue(getattr(config, name) * scale)
            self.fields[name] = (control, scale)
            forms[group].addRow(label, control)
        self.fields["offset_safety_m"][0].setToolTip(
            "Zusätzliche Sicherheit in beide Förderrichtungen, über Strahlradius und "
            "Randabstand hinaus. Standard 5 mm. Der Kraftangriff liegt weiter innerhalb "
            "der Bauteilfläche; seitliche Reihenabstände bleiben unverändert. "
            "0 mm stellt die bisherige Offsetauswahl wieder her."
        )
        self.add_note(
            forms["coverage"],
            "Überdeckung der projizierten Bauteilfläche: unter der unteren Grenze Gewicht 0, "
            "dazwischen 0,5, ab der oberen Grenze 1. Die Düsenfreigabe berücksichtigt "
            "zusätzlich Volltreffer und Sicherheitsabstände.",
        )
        self.add_note(
            forms["flow"],
            "0,05–0,80 bar Überdruck ist der Referenzbereich des Ausgangsmodells. "
            "Die Grenze für nicht abgeschnürte Strömung wird separat geprüft. "
            "Den analytischen Druck und die erforderliche Kraft zeigen die Ergebnisse.",
        )
        self.add_note(
            forms["flow"],
            "Die Impulsdauer je Array wird auf der Hauptseite im Parametereditor eingestellt.",
        )
        self.add_note(
            forms["scaling"],
            "p_SPS = (p_analytisch − b) / a; Drücke in mbar. "
            "Der analytische Druck bleibt in den Ergebnissen sichtbar. "
            "Übernommen wird der SPS-Sollwert innerhalb 0–6000 mbar.",
        )
        for name, text, scale, decimals, low, high in (
            ("pressure_scale_a", "Druckskalierung a [–]", 1, 12, 1e-12, 1e6),
            ("pressure_scale_b_pa", "Druckskalierung b [mbar]", 0.01, 9, -1e9, 1e9),
        ):
            control = QDoubleSpinBox()
            control.setDecimals(decimals)
            control.setRange(low, high)
            control.setValue(getattr(config, name) * scale)
            self.fields[name] = (control, scale)
            forms["scaling"].addRow(text, control)
        self.add_note(forms["scaling"], PRESSURE_SCALING_SOURCE)
        self.outer_width = QDoubleSpinBox()
        self.outer_width.setDecimals(6)
        self.outer_width.setRange(0.001, 100000)
        self.outer_width.setValue(config.outer_width_m * 1000)
        forms["openings"].addRow("Außenbreite der Öffnungen [mm]", self.outer_width)
        self.center_span = QLabel(f"{config.center_span_m * 1000:g}")
        self.center_span.setToolTip(
            "Abstand zwischen erster und letzter Lochmitte: (Öffnungszahl − 1) × Teilung. "
            "Bei acht Öffnungen und 2,8 mm Teilung ergibt das 19,6 mm."
        )
        forms["openings"].addRow("Abstand erste–letzte Lochmitte [mm]", self.center_span)
        self.manual_axis = QComboBox()
        self.manual_axis.addItems(["Automatisch aus Posen/Roadmap", "X", "Y", "Z"])
        self.manual_angle = QDoubleSpinBox()
        self.manual_angle.setRange(-360, 360)
        self.manual_angle.setValue(90)
        self.add_note(forms["tipping"], "Manuelle Drehung: gilt nur für den aktuellen Übergang.")
        forms["tipping"].addRow("Manuelle Achse bei mehrdeutigen Posen", self.manual_axis)
        forms["tipping"].addRow("Manueller Drehwinkel mit Vorzeichen [°]", self.manual_angle)
        self.add_note(
            forms["tipping"],
            "Auswertung am Impulsbeginn; Bewegung während des Impulses bleibt unberücksichtigt.",
        )
        self.parameters_footer = QWidget()
        footer = QVBoxLayout(self.parameters_footer)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(
            QLabel("Speichert alle Modellbereiche; Montagewerte werden separat gespeichert.")
        )
        self.save_parameters_button = QPushButton(
            "Modellwerte aller Bereiche als Standard speichern"
        )
        self.save_parameters_button.clicked.connect(self.save_parameters_defaults)
        self.parameters_status = QLabel(warning)
        self.parameters_status.setWordWrap(True)
        footer.addWidget(self.save_parameters_button)
        footer.addWidget(self.parameters_status)
        root.addWidget(self.parameters_footer)
        self.mount_table = QTableWidget(len(config.mounts), 6)
        self.mount_table.setHorizontalHeaderLabels(
            [
                "Array",
                "Düse",
                "Kipp-/Reihenachse",
                "Strahl normal",
                "Reihenmitte ab Rinnenkante [mm]",
                "X-Versatz zur Referenzdüse [mm]",
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
            longitudinal = QLineEdit(
                f"{mount.longitudinal_offset_m * 1000:g}"
                if mount.longitudinal_offset_m is not None
                else ""
            )
            longitudinal.setPlaceholderText("Nicht angegeben")
            longitudinal.setToolTip(
                "Vorzeichenbehafteter X-Versatz der Reihenmitte zur Referenzdüse 1/4. "
                "Nur für Reihen entlang X; Referenzdüse bleibt bei 0 mm."
            )
            self.longitudinal_fields[(mount.array_index, mount.nozzle_index)] = longitudinal
            self.mount_table.setCellWidget(row, 5, longitudinal)
            longitudinal.setReadOnly(mount.nozzle_index in (1, 4))
            longitudinal.setEnabled(mount.rotation_axis == "x")
            axis.currentTextChanged.connect(
                lambda text, field=longitudinal: field.setEnabled(text == "X")
            )
            longitudinal.textChanged.connect(self.changed)
            axis.currentTextChanged.connect(self.changed)
            normal.currentTextChanged.connect(self.changed)
            center.valueChanged.connect(self.changed)
        self.mount_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        montage_layout = self.section_pages["mounts"].layout()
        instructions = QLabel(
            "Düsen 1–3 und 4–6 verlängern jeweils dieselbe Lochreihe. "
            "Alle drei Montagepositionen müssen bekannt sein. Zugeschaltet wird eine Düse "
            "erst ab einem Volltreffer einschließlich Randabstand. "
            "Für Reihen entlang X den Versatz der Reihenmitte zu Düse 1/4 eingeben; "
            "ansonsten bezeichnet die Reihenmitte den Abstand entlang Y/Z von der Rinnenkante."
        )
        instructions.setWordWrap(True)
        montage_layout.addWidget(instructions)
        self.mount_filter = QComboBox()
        self.mount_filter.addItem("Alle Arrays", 0)
        for array in range(1, 5):
            self.mount_filter.addItem(f"Array {array}", array)
        self.mount_filter.currentIndexChanged.connect(self.filter_mounts)
        montage_layout.addWidget(self.mount_filter)
        montage_layout.addWidget(self.mount_table, 1)
        self.save_mounts_button = QPushButton("Montagewerte aller Arrays als Standard speichern")
        self.save_mounts_button.clicked.connect(self.save_mounts_defaults)
        montage_layout.addWidget(self.save_mounts_button)
        self.mounts_status = QLabel(warning)
        self.mounts_status.setWordWrap(True)
        montage_layout.addWidget(self.mounts_status)
        for key, page in self.section_pages.items():
            if key != "mounts":
                page.layout().addStretch()
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
        self.navigation.currentRowChanged.connect(self.show_section)
        self.navigation.setCurrentRow(0)

    def add_section(self, key, title, description, *, scroll):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel(title)
        font = heading.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        heading.setFont(font)
        layout.addWidget(heading)
        help_text = QLabel(description)
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.section_keys.append(key)
        self.section_pages[key] = page
        self.navigation.addItem(title)
        self.navigation.item(self.navigation.count() - 1).setToolTip(description)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(page)
            self.pages.addWidget(area)
        else:
            self.pages.addWidget(page)

    def add_group(self, section, title):
        group = QGroupBox(title)
        self.section_pages[section].layout().addWidget(group)
        return QFormLayout(group)

    @staticmethod
    def add_note(form, text):
        note = QLabel(text)
        note.setWordWrap(True)
        form.addRow(note)

    def show_section(self, index):
        if index < 0:
            return
        self.pages.setCurrentIndex(index)
        self.parameters_footer.setVisible(self.section_keys[index] != "mounts")

    def filter_mounts(self):
        array = self.mount_filter.currentData()
        for row, (mount, _, _, _) in enumerate(self.mount_rows):
            self.mount_table.setRowHidden(row, bool(array and array != mount.array_index))

    def link_pitch(self):
        if self._linking:
            return
        self._linking = True
        diameter = self.fields["diameter_m"][0].value()
        pitch = self.fields["pitch_m"][0].value()
        self.outer_width.setValue(diameter + (self.count.value() - 1) * pitch)
        self.center_span.setText(f"{(self.count.value() - 1) * pitch:g}")
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
        self.center_span.setText(
            f"{(self.count.value() - 1) * self.fields['pitch_m'][0].value():g}"
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
                longitudinal_offset_m=self.longitudinal_value(mount, axis.currentText()),
            )
            for mount, axis, normal, center in self.mount_rows
        )

    def longitudinal_value(self, mount, axis):
        if axis != "X" or mount.nozzle_index in (1, 4):
            return 0.0
        text = self.longitudinal_fields[(mount.array_index, mount.nozzle_index)].text().strip()
        if not text:
            return None
        try:
            return float(text.replace(",", ".")) / 1000
        except ValueError as exc:
            raise ValueError(
                "X-Versatz muss eine Zahl in mm sein; Dezimalpunkt oder Komma verwenden."
            ) from exc

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
