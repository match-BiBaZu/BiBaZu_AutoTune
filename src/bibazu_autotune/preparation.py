"""Shared part/analysis state with read-only geometry and model settings."""

from __future__ import annotations

import copy
import queue
import threading
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QTabWidget, QTextBrowser, QVBoxLayout, QWidget

from .autotune import AutotunePipeline
from .geometry import analyze_stl
from .geometry_panel import GeometryPanel
from .model_panel import ModelPanel
from .trial_context import validate_context
from .workpiece import WorkpieceSelection


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
    selection_changed = pyqtSignal()
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
        self.analysis = self.context = self.path = self.unconfirmed_context = None
        self.unit = "mm"
        self.generation = self.model_generation = 0
        self.model_result = self.preview_result = None
        self.calculation_running = False
        self.geometry_error = "Auf der Hauptseite eine Roadmap oder STL laden."
        root = QVBoxLayout(self)
        self.pages = QTabWidget()
        root.addWidget(self.pages)
        self.geometry_panel = GeometryPanel()
        self.details = QTextBrowser()
        self.details.setMaximumHeight(150)
        self.geometry_panel.layout().addWidget(self.details)
        self.pages.addTab(self.geometry_panel, "Geometrie prüfen")
        self.pages.addTab(self.model_panel, "Modell und Anlage")
        self.geometry_worker = GeometryWorker(self)
        self.geometry_worker.completed.connect(self.accept_analysis)
        self.model_worker = ModelWorker(self.pipeline, self)
        self.model_worker.completed.connect(self.accept_model_result)
        self.model_panel.changed.connect(self.refresh_status)
        self.density.textChanged.connect(self.request_analysis)
        self.refresh_status()

    def selection(self):
        if self.context is None:
            return None
        return WorkpieceSelection(copy.deepcopy(self.context), self.path, self.unit, self.analysis)

    def accept_selection(self, selection):
        context = validate_context(selection.context)
        if selection.unit not in ("mm", "cm", "m"):
            raise ValueError("Importeinheit muss mm, cm oder m sein.")
        path = Path(selection.path).resolve() if selection.path else None
        analysis = selection.analysis
        if analysis is not None and (
            path != analysis.path.resolve()
            or selection.unit != analysis.unit
            or context.get("kind") == "manual"
            and analysis.sha256 != context["mesh_sha256"]
        ):
            raise ValueError("STL-Auswertung passt nicht zum bestätigten Bauteil.")
        self.generation += 1  # Reject every pending import from the previous selection.
        self.context, self.path, self.unit = context, path, selection.unit
        self.unconfirmed_context = None
        self.analysis = analysis
        self.geometry_error = "STL zuordnen, um das Bauteil auszuwerten."
        try:
            density = self.model_panel.density_value()
        except ValueError:
            density = None
        if path is not None and analysis is None:
            self.request_analysis()
        elif analysis is not None and analysis.material.density_kg_m3 != density:
            self.request_analysis()
        else:
            self.refresh_status()

    def set_context(self, context, mesh_path=None):
        if context is None:
            self.generation += 1
            self.context = self.path = self.analysis = self.unconfirmed_context = None
            self.unit = "mm"
            self.geometry_error = "Auf der Hauptseite eine Roadmap oder STL laden."
            self.refresh_status()
            return
        context = validate_context(context)
        analysis, unit = None, "mm"
        if (
            context.get("kind") == "manual"
            and self.context is not None
            and self.context.get("part_name") == context["part_name"]
            and self.analysis is not None
            and self.analysis.sha256 == context["mesh_sha256"]
        ):
            mesh_path, analysis, unit = self.path, self.analysis, self.unit
        self.accept_selection(WorkpieceSelection(context, mesh_path, unit, analysis))

    def set_mesh(self, path):
        self.path = Path(path).resolve() if path else None
        self.request_analysis()

    def set_unit(self, unit):
        if unit not in ("mm", "cm", "m"):
            raise ValueError("Importeinheit muss mm, cm oder m sein.")
        self.unit = unit
        self.request_analysis()

    def request_analysis(self, *_):
        self.generation += 1
        self.analysis = None
        if self.path is None:
            self.refresh_status()
            return
        try:
            density = self.model_panel.density_value()
        except ValueError as exc:
            self.geometry_error = str(exc)
            self.refresh_status()
            return
        self.geometry_error = "STL wird im Hintergrund ausgewertet …"
        self.geometry_worker.commands.put((self.generation, self.path, self.unit, density))
        if not self.geometry_worker.isRunning() and not self.geometry_worker.stopping.is_set():
            self.geometry_worker.start()
        self.refresh_status()

    def accept_analysis(self, generation, analysis, error):
        if generation != self.generation or self.geometry_worker.stopping.is_set():
            return
        self.analysis, self.geometry_error = analysis, error
        if (
            self.context
            and self.context.get("kind") == "manual"
            and analysis
            and self.context["mesh_sha256"] != analysis.sha256
        ):
            self.unconfirmed_context, self.context = self.context, None
            self.geometry_error = (
                "STL-Inhalt geändert: Bauteil und Posen auf der Hauptseite bestätigen."
            )
        self.refresh_status()

    def refresh_status(self):
        self.geometry_panel.set_analysis(self.analysis, self.geometry_error)
        self.model_generation += 1
        self.model_result = self.preview_result = None
        self.calculation_running = False
        self.inputs_changed.emit()
        self.calculation_state_changed.emit(False)
        self.selection_changed.emit()
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
            self.details.setPlainText(str(exc))
            self.status_changed.emit(str(exc))

    def show_model_result(self, result):
        lines = [
            "Modellschritte",
            result.pulse.message,
            f"Impulsdauer je Array [ms]: {result.pulse.value}",
            result.offset.message,
            result.pressure.message,
            result.sps_pressure.message,
            result.ml.message,
        ]
        if result.missing:
            lines.append("Fehlende Voraussetzungen: " + "; ".join(result.missing))
        self.details.setPlainText("\n".join(lines))
        self.results_changed.emit(result)
        if result.missing:
            self.status_changed.emit("Fehlende Voraussetzungen: " + "; ".join(result.missing))
        elif self.model_result is not None:
            count = sum(c.pressure is not None for c in result.candidates)
            scaled = sum(c.sps_pressure is not None for c in result.candidates)
            self.status_changed.emit(
                f"Berechnung abgeschlossen · {count} analytische Ergebnisse, "
                f"{scaled} SPS-Druckvorschläge. Kandidat zur Übernahme auswählen."
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
            return
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

    def stop(self):
        self.geometry_worker.stopping.set()
        self.model_worker.stopping.set()

    def workers_running(self):
        return self.geometry_worker.isRunning() or self.model_worker.isRunning()
