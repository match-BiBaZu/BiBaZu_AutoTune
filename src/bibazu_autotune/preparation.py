"""Embedded offline settings and analysis workers, separate from network IO."""

from __future__ import annotations

import copy
import queue
import threading
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .autotune import AutotunePipeline
from .geometry import analyze_stl
from .geometry_panel import GeometryPanel
from .model_panel import ModelPanel
from .pose_preview import render_mesh_preview
from .trial_context import manual_context, normalize_quaternion


class GeometryWorker(QThread):
    completed = pyqtSignal(int, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.commands = queue.Queue()
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            try:
                generation, path, unit, density = self.commands.get(timeout=0.1)
            except queue.Empty:
                continue
            # Coalesce edits queued while the preceding mesh was being read.
            while True:
                try:
                    generation, path, unit, density = self.commands.get_nowait()
                except queue.Empty:
                    break
            try:
                analysis = analyze_stl(path, unit, density)
            except Exception as exc:
                self.completed.emit(generation, None, str(exc))
            else:
                self.completed.emit(generation, analysis, "")


class ModelWorker(QThread):
    completed = pyqtSignal(int, object, str)

    def __init__(self, pipeline, parent=None):
        super().__init__(parent)
        self.pipeline = pipeline
        self.commands = queue.Queue()
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            try:
                generation, args = self.commands.get(timeout=0.1)
            except queue.Empty:
                continue
            while True:
                try:
                    generation, args = self.commands.get_nowait()
                except queue.Empty:
                    break
            try:
                result = self.pipeline.prepare(*args)
            except Exception as exc:
                self.completed.emit(generation, None, str(exc))
            else:
                self.completed.emit(generation, result, "")


class AutotuneSettingsPage(QWidget):
    context_changed = pyqtSignal(object)
    inputs_changed = pyqtSignal()
    results_changed = pyqtSignal(object)
    calculation_state_changed = pyqtSignal(bool)
    status_changed = pyqtSignal(str)

    def __init__(self, parameters, parent=None):
        super().__init__(parent)
        self.parameters = parameters
        self.pipeline = AutotunePipeline()
        self.model_panel = ModelPanel()
        self.density = self.model_panel.density
        self.analysis = None
        self.context = None
        self.path = None
        self.generation = 0
        self.model_generation = 0
        self.model_result = None
        self.preview_result = None
        self.calculation_running = False
        self.geometry_error = "STL aus einer Roadmap übernehmen oder manuell auswählen."
        self._setting_context = False
        root = QVBoxLayout(self)
        self.pages = QTabWidget()
        root.addWidget(self.pages)
        setup = QWidget()
        self.pages.addTab(setup, "STL und Posen")
        layout = QVBoxLayout(setup)
        form = QFormLayout()
        self.file_label = QLabel("Keine STL geladen")
        self.file_label.setWordWrap(True)
        form.addRow("STL", self.file_label)
        self.choose_button = QPushButton("STL auswählen / ersetzen")
        self.choose_button.clicked.connect(self.choose_stl)
        form.addRow(self.choose_button)
        self.unit = QComboBox()
        self.unit.addItems(["mm", "cm", "m"])
        form.addRow("STL-Koordinateneinheit", self.unit)
        self.manual = QCheckBox("Manuelle Ausgangs- und Zielpose verwenden")
        self.manual.setChecked(True)
        form.addRow(self.manual)
        self.part_name = QLineEdit()
        self.source = QLineEdit("0, 0, 0, 1")
        self.target = QLineEdit("0, 0, 0, 1")
        self.source.setPlaceholderText("x, y, z, w; Dezimalpunkt verwenden")
        self.target.setPlaceholderText("x, y, z, w; Dezimalpunkt verwenden")
        form.addRow("Bauteilname", self.part_name)
        form.addRow("Ausgangsquaternion x, y, z, w", self.source)
        form.addRow("Zielquaternion x, y, z, w", self.target)
        self.confirm_button = QPushButton("Manuelle Posen bestätigen")
        self.confirm_button.clicked.connect(self.confirm_manual)
        form.addRow(self.confirm_button)
        self.pose_status = QLabel("Manuelle Posen noch nicht bestätigt.")
        self.pose_status.setWordWrap(True)
        form.addRow(self.pose_status)
        layout.addLayout(form)
        previews = QHBoxLayout()
        self.source_preview = QLabel("Ausgangspose")
        self.target_preview = QLabel("Zielpose")
        self.source_preview.setMinimumSize(320, 180)
        self.target_preview.setMinimumSize(320, 180)
        previews.addWidget(self.source_preview)
        previews.addWidget(self.target_preview)
        layout.addLayout(previews)
        self.details = QTextBrowser()
        layout.addWidget(self.details)
        self.geometry_panel = GeometryPanel()
        self.pages.addTab(self.geometry_panel, "Geometrie prüfen")
        self.pages.addTab(self.model_panel, "Modell und Montage")
        self.geometry_worker = GeometryWorker(self)
        self.geometry_worker.completed.connect(self.accept_analysis)
        self.model_worker = ModelWorker(self.pipeline, self)
        self.model_worker.completed.connect(self.accept_model_result)
        self.model_panel.changed.connect(self.refresh_status)
        self.unit.currentTextChanged.connect(self.request_analysis)
        self.density.textChanged.connect(self.request_analysis)
        self.manual.toggled.connect(self.mode_changed)
        for field in (self.part_name, self.source, self.target):
            field.textEdited.connect(self.pose_edited)
        self.refresh_status()

    def choose_stl(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "STL auswählen", str(self.path.parent) if self.path else "", "STL (*.stl)"
        )
        if path:
            self.set_mesh(path)

    def invalidate_manual(self):
        if self.context and self.context.get("kind") == "manual":
            self.context = None
            self.context_changed.emit(None)
        if self.manual.isChecked():
            self.pose_status.setText("Manuelle Posen noch nicht bestätigt.")

    def set_mesh(self, path):
        self.invalidate_manual()
        new_path = Path(path).resolve() if path else None
        if new_path != self.path:
            self.clear_mesh()
            if self.manual.isChecked() and new_path:
                self.part_name.setText(new_path.stem)
        self.path = new_path
        self.file_label.setText(str(self.path) if self.path else "Keine STL zugeordnet")
        self.request_analysis()

    def set_context(self, context, mesh_path=None):
        previous = self.context
        previous_part = self.part_name.text().strip()
        self.context = context
        is_manual = bool(context and context.get("kind") == "manual")
        self._setting_context = True
        try:
            self.manual.setChecked(is_manual or context is None)
            if context:
                self.part_name.setText(context["part_name"])
                for field, key in (
                    (self.source, "source_quaternion_xyzw"),
                    (self.target, "target_quaternion_xyzw"),
                ):
                    if key in context:
                        field.setText(", ".join(f"{v:.16g}" for v in context[key]))
            if is_manual:
                if (
                    self.analysis is None
                    or self.analysis.sha256 != context["mesh_sha256"]
                    or previous_part != context["part_name"]
                ):
                    # Invalidate pending imports too: their hash is not known yet.
                    self.clear_mesh()
            elif context is not None:
                # A roadmap change never inherits a manually selected replacement mesh.
                if previous != context or self.path != mesh_path:
                    self.clear_mesh()
                    self.path = Path(mesh_path) if mesh_path else None
                    self.file_label.setText(
                        str(self.path) if self.path else "Roadmap enthält keine STL."
                    )
                    self.request_analysis()
            self.pose_status.setText(
                "Manuelle Posen bestätigt."
                if is_manual
                else "Posen aus Roadmap übernommen."
                if context
                else "Manuelle Posen noch nicht bestätigt."
            )
        finally:
            self._setting_context = False
        self.update_pose_controls()
        self.update_previews()
        self.refresh_status()

    def clear_mesh(self):
        self.generation += 1
        self.path = None
        self.analysis = None
        self.unit.blockSignals(True)
        self.unit.setCurrentText("mm")
        self.unit.blockSignals(False)
        self.geometry_error = "STL auswählen; Geometrie wird neu ausgewertet."
        self.file_label.setText("Keine STL geladen")

    def mode_changed(self):
        if not self._setting_context:
            # Switching mode discards the old confirmed transition, never invents poses.
            self.context = None
            # Keep the selected mode when the parent clears its old trial context.
            manual = self.manual.isChecked()
            self.context_changed.emit(None)
            self._setting_context = True
            self.manual.setChecked(manual)
            self._setting_context = False
            self.pose_status.setText("Manuelle Posen bestätigen oder eine Roadmap auswählen.")
        self.update_pose_controls()
        self.update_previews()
        self.refresh_status()

    def update_pose_controls(self):
        for control in (self.part_name, self.source, self.target, self.confirm_button):
            control.setEnabled(self.manual.isChecked())

    @staticmethod
    def parse_quaternion(text):
        try:
            values = [float(v) for v in text.replace(",", " ").split()]
        except ValueError as exc:
            raise ValueError("Quaternion: vier Zahlen mit Dezimalpunkt eingeben.") from exc
        return normalize_quaternion(values)

    def pose_edited(self):
        if self._setting_context:
            return
        self.invalidate_manual()
        self.update_previews()
        self.refresh_status()

    def confirm_manual(self):
        try:
            if self.analysis is None:
                raise ValueError("Zuerst eine STL erfolgreich auswerten.")
            context = manual_context(
                self.part_name.text(),
                self.analysis.sha256,
                self.parse_quaternion(self.source.text()),
                self.parse_quaternion(self.target.text()),
            )
        except ValueError as exc:
            self.pose_status.setText(str(exc))
            return
        self.context = context
        self.pose_status.setText("Manuelle Posen bestätigt.")
        self.context_changed.emit(context)
        self.update_previews()
        self.refresh_status()

    def request_analysis(self, *_):
        self.generation += 1
        self.analysis = None
        self.update_previews()
        if self.path is None:
            self.refresh_status()
            return
        try:
            density = self.model_panel.density_value()
        except ValueError:
            self.geometry_error = "Dichte muss eine positive, endliche Zahl in kg/m³ sein."
            self.refresh_status()
            return
        self.geometry_error = "STL wird im Hintergrund ausgewertet …"
        self.geometry_worker.commands.put(
            (self.generation, self.path, self.unit.currentText(), density)
        )
        if not self.geometry_worker.isRunning() and not self.geometry_worker.stopping.is_set():
            self.geometry_worker.start()
        self.update_previews()
        self.refresh_status()

    def accept_analysis(self, generation, analysis, error):
        if generation != self.generation or self.geometry_worker.stopping.is_set():
            return
        self.analysis = analysis
        self.geometry_error = error
        if (
            self.context
            and self.context.get("kind") == "manual"
            and analysis
            and self.context["mesh_sha256"] != analysis.sha256
        ):
            self.invalidate_manual()
        self.update_previews()
        self.refresh_status()

    def update_previews(self):
        for label, field, key, caption in (
            (self.source_preview, self.source, "source_quaternion_xyzw", "Ausgangspose"),
            (self.target_preview, self.target, "target_quaternion_xyzw", "Zielpose"),
        ):
            label.clear()
            if self.analysis is None:
                label.setText(caption + ": STL auswerten")
                continue
            try:
                if self.manual.isChecked():
                    quaternion = self.parse_quaternion(field.text())
                elif self.context and key in self.context:
                    quaternion = normalize_quaternion(self.context[key])
                else:
                    label.setText(caption + ": Orientierung fehlt")
                    continue
                label.setPixmap(
                    render_mesh_preview(
                        self.analysis.path,
                        320,
                        180,
                        quaternion_xyzw=quaternion,
                        caption=caption,
                        triangles=self.analysis.preview_triangles,
                    )
                )
            except (ValueError, OSError) as exc:
                label.setText(caption + ": " + str(exc))

    def refresh_status(self):
        self.geometry_panel.set_analysis(self.analysis, self.geometry_error)
        self.model_generation += 1
        self.model_result = None
        self.preview_result = None
        self.calculation_running = False
        self.inputs_changed.emit()
        self.calculation_state_changed.emit(False)
        lines = []
        if self.analysis:
            g, m = self.analysis.geometry, self.analysis.material
            lines.extend(
                [
                    f"STL SHA-256: {self.analysis.sha256}",
                    f"Dreiecke: {g.triangle_count}",
                    "Abmessungen X/Y/Z [mm]: " + ", ".join(f"{v * 1000:g}" for v in g.dimensions_m),
                    f"Oberfläche [mm²]: {g.area_m2 * 1e6:g}",
                    f"Volumen [mm³]: {g.volume_m3 * 1e9:g}"
                    if g.valid_volume
                    else "Volumen: nicht verfügbar",
                    f"Volumenschwerpunkt [m]: {g.center_m}"
                    if g.center_m is not None
                    else "Volumenschwerpunkt: nicht verfügbar",
                    "Geometrischer Trägheitstensor am Schwerpunkt [m⁵]:\n"
                    + self.format_tensor(g.inertia_m5),
                    f"Dichte [kg/m³]: {m.density_kg_m3}"
                    if m.density_kg_m3 is not None
                    else "Dichte: nicht angegeben",
                    f"Masse [g]: {m.mass_kg * 1000:g}"
                    if m.mass_kg is not None
                    else "Masse: nicht verfügbar",
                    "Trägheitstensor am Schwerpunkt [kg·m²]:\n"
                    + self.format_tensor(m.inertia_kg_m2),
                    self.analysis.warning,
                ]
            )
        else:
            lines.append(self.geometry_error)
        self.base_details = "\n".join(lines)
        try:
            result = self.pipeline.prepare(
                self.analysis,
                self.context,
                self.parameters(),
                self.model_panel.configuration(),
                calculate=False,
            )
            self.preview_result = result
            self.show_model_result(result)
        except ValueError as exc:
            self.preview_result = None
            lines.append(str(exc))
            self.details.setPlainText("\n".join(lines))
            self.status_changed.emit(str(exc))

    def show_model_result(self, result):
        lines = [
            self.base_details,
            "",
            "Modellschritte",
            result.pulse.message,
            f"Impulsdauer je Array [ms]: {result.pulse.value}",
            result.offset.message,
            result.pressure.message,
            result.ml.message,
        ]
        if result.missing:
            lines.append("Fehlende Voraussetzungen: " + "; ".join(result.missing))
        lines.append(
            "\nGeometrie und Ergebnisse bleiben in dieser Sitzung. Modell- und Montagestartwerte "
            "werden nur über die Schaltflächen zum Speichern der Standardwerte gespeichert."
        )
        self.details.setPlainText("\n".join(lines))
        self.results_changed.emit(result)
        if result.missing:
            self.status_changed.emit("Fehlende Voraussetzungen: " + "; ".join(result.missing))
        elif self.model_result is not None:
            count = sum(c.pressure is not None for c in result.candidates)
            self.status_changed.emit(
                f"Berechnung abgeschlossen · {count} Druckvorschläge. "
                "Kandidat zur Übernahme auswählen."
            )
        elif any(c.status == "pending" for c in result.candidates):
            self.status_changed.emit("Bereit zur Berechnung.")
        elif result.candidates:
            self.status_changed.emit("Voraussetzungen prüfen: " + result.candidates[0].message)

    def calculate_model(self):
        if not self.isEnabled() or self.calculation_running:
            return
        self.refresh_status()
        try:
            args = (
                self.analysis,
                copy.deepcopy(self.context),
                self.parameters(),
                self.model_panel.configuration(),
            )
            preview = self.pipeline.prepare(*args, calculate=False)
            if preview.missing or not any(c.status == "pending" for c in preview.candidates):
                return
        except ValueError:
            return  # refresh_status has already displayed the validation error.
        self.calculation_running = True
        self.calculation_state_changed.emit(True)
        self.status_changed.emit("Analytisches Modell wird im Hintergrund berechnet …")
        self.model_worker.commands.put((self.model_generation, args))
        if not self.model_worker.isRunning() and not self.model_worker.stopping.is_set():
            self.model_worker.start()

    def accept_model_result(self, generation, result, error):
        if generation != self.model_generation or self.model_worker.stopping.is_set():
            return
        self.calculation_running = False
        self.calculation_state_changed.emit(False)
        if error:
            self.status_changed.emit(error)
            return
        self.model_result = result
        self.show_model_result(result)

    @staticmethod
    def format_tensor(tensor):
        if tensor is None:
            return "nicht verfügbar"
        return "\n".join("  ".join(f"{v:.6g}" for v in row) for row in tensor)

    def stop(self):
        self.geometry_worker.stopping.set()
        self.model_worker.stopping.set()

    def workers_running(self):
        return self.geometry_worker.isRunning() or self.model_worker.isRunning()
