"""Display model probabilities for a fixed setup. This dialog only reads inputs."""

from PyQt6.QtCore import QAbstractTableModel, Qt, QThread
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
)

from ..recipes import LIMITS
from .artifacts import FLIP_CLASSES, load_model, model_directory, read_metadata
from .search import GridRange, grid_axes, score_grid

PROBABILITY_HEADERS = tuple(f"P({label})" for label in FLIP_CLASSES)


class GridTable(QAbstractTableModel):
    headers = (
        "Druck [mbar SPS]",
        "Impuls [ms]",
        "Analytisch [mbar]",
        *PROBABILITY_HEADERS,
        "Bereich",
    )

    def __init__(self, parent):
        super().__init__(parent)
        self.rows = ()

    def show_points(self, points):
        self.beginResetModel()
        self.rows = points
        self.endResetModel()

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        point = self.rows[index.row()]
        if point.outside_features:
            if role == Qt.ItemDataRole.ToolTipRole:
                return "Außerhalb der beobachteten Spannen: " + ", ".join(point.outside_features)
            if role == Qt.ItemDataRole.BackgroundRole:
                return QBrush(QColor("#f8d9aa"))
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(QColor("#442600"))
        if role == Qt.ItemDataRole.DisplayRole:
            values = (
                str(point.pressure_mbar),
                str(point.pulse_duration_ms),
                f"{point.analytical_mbar:.2f}",
                *(f"{p:.1%}" for p in point.probabilities),
                "Extrapolation" if point.outside_features else "In Merkmalsspannen",
            )
            return values[index.column()]
        return None


class SearchWorker(QThread):
    def __init__(self, path, context, pressure, duration, parent):
        super().__init__(parent)
        self.path = path
        self.context = context
        self.pressure = pressure
        self.duration = duration
        self.result = None
        self.error = ""

    def run(self):
        try:
            model = load_model(self.path)
            self.result = (
                model.metadata,
                score_grid(
                    model,
                    self.context,
                    self.pressure,
                    self.duration,
                ),
            )
        except ImportError:
            self.error = "ML-Abhängigkeiten fehlen. AutoTune mit Setup.ps1 einrichten."
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__


class RecommendationDialog(QDialog):
    def __init__(self, context, part_hash, setup, pressure, duration, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ML-Empfehlung · Druck und Impulsdauer")
        self.resize(1040, 540)
        self.context = context
        self.part_hash = part_hash
        self.worker = None
        self.setup_current = True
        layout = QVBoxLayout(self)
        setup_label = QLabel(setup)
        setup_label.setWordWrap(True)
        layout.addWidget(setup_label)
        form = QFormLayout()
        catalog = QHBoxLayout()
        self.models = QComboBox()
        self.reload = QPushButton("Modelle aktualisieren")
        self.reload.clicked.connect(self.refresh_models)
        catalog.addWidget(self.models, 1)
        catalog.addWidget(self.reload)
        form.addRow("Modell", catalog)
        self.pressure = self.range_widgets(form, "Druck [mbar SPS]", "pressure_mbar", pressure, 100)
        self.duration = self.range_widgets(
            form, "Impulsdauer [ms]", "pulse_duration_ms", duration, 10
        )
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.search = QPushButton("Raster bewerten")
        self.search.clicked.connect(self.start_search)
        close = QPushButton("Schließen")
        close.clicked.connect(self.reject)
        actions.addWidget(self.search)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        table = QTableView()
        self.grid = GridTable(table)
        table.setModel(self.grid)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setDefaultSectionSize(135)
        layout.addWidget(table, 1)
        self.models.currentIndexChanged.connect(self.invalidate)
        self.refresh_models()

    def range_widgets(self, layout, label, field, initial, step):
        low, high, _ = LIMITS[field]
        widgets = []
        row = QHBoxLayout()
        for caption, minimum, value in (
            ("Von", low, initial),
            ("Bis", low, initial),
            ("Schritt", 1, step),
        ):
            spin = QSpinBox()
            spin.setRange(minimum, high)
            spin.setValue(value)
            spin.valueChanged.connect(self.invalidate)
            row.addWidget(QLabel(caption))
            row.addWidget(spin, 1)
            widgets.append(spin)
        layout.addRow(label, row)
        return widgets

    def refresh_models(self):
        current = self.models.currentData()
        self.models.blockSignals(True)
        self.models.clear()
        try:
            directory = model_directory()
            if directory.is_dir():
                for path in sorted(directory.iterdir()):
                    if not path.is_dir() or path.name.startswith("."):
                        continue
                    if not (path / "metadata.json").is_file():
                        continue
                    try:
                        meta = read_metadata(path)
                        label = f"{meta.get('name', path.name)} · {meta.get('created_utc', '')}"
                    except (OSError, ValueError):
                        label = f"{path.name} · Metadaten ungültig"
                    self.models.addItem(label, path)
            if (index := self.models.findData(current)) >= 0:
                self.models.setCurrentIndex(index)
        except OSError as exc:
            self.models.blockSignals(False)
            self.invalidate()
            self.status.setText(f"Modellordner nicht lesbar: {exc}")
            return
        self.models.blockSignals(False)
        self.invalidate()

    def invalidate_setup(self):
        self.setup_current = False
        self.invalidate()

    def invalidate(self, *_):
        self.grid.show_points(())
        self.update_controls()
        if not self.setup_current:
            self.status.setText("Aufbau geändert. ML-Empfehlung erneut öffnen.")
        elif self.models.count() == 0:
            self.status.setText("Im Modellordner ist noch kein trainiertes Modell verfügbar.")
        else:
            self.status.setText("")

    def update_controls(self):
        idle = self.worker is None
        for widget in (self.models, self.reload, *self.pressure, *self.duration):
            widget.setEnabled(idle and self.setup_current)
        self.search.setEnabled(idle and self.setup_current and self.models.count() > 0)

    def start_search(self):
        if not self.search.isEnabled():
            return
        self.invalidate()
        pressure = GridRange(*(widget.value() for widget in self.pressure))
        duration = GridRange(*(widget.value() for widget in self.duration))
        try:
            pressures, durations = grid_axes(pressure, duration)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText(f"Bewerte {len(pressures) * len(durations)} Parameterpaare …")
        self.worker = SearchWorker(
            self.models.currentData(), self.context, pressure, duration, self
        )
        self.worker.finished.connect(self.show_result)
        self.update_controls()
        self.worker.start()

    def show_result(self):
        worker, self.worker = self.worker, None
        self.update_controls()
        worker.deleteLater()
        if not self.setup_current:
            return
        if worker.error:
            self.status.setText("Modell oder Suche nicht verfügbar: " + worker.error)
            return
        metadata, rows = worker.result
        self.grid.show_points(rows)
        best = rows[0]
        details = [
            f"{label}: {p:.1%}" for label, p in zip(FLIP_CLASSES, best.probabilities, strict=True)
        ]
        if best.outside_features:
            details.append("Extrapolation außerhalb beobachteter Merkmalsspannen")
        if self.part_hash not in metadata["part_groups"]:
            details.append("Bauteil war nicht in den Trainingsdaten")
        if metadata["validation"].get("status") != "validated":
            details.append("An neuen Bauteilen ungeprüft")
        self.status.setText(
            f"Vorschlag {best.pressure_mbar} mbar SPS / {best.pulse_duration_ms} ms "
            f"· analytisch {best.analytical_mbar:.2f} mbar Überdruck\n" + " · ".join(details)
        )

    def done(self, result):
        if self.worker is None:
            super().done(result)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
