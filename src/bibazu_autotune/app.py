"""Desktop utility; the worker alone owns network IO and trial orchestration."""

from __future__ import annotations

import argparse
import copy
import json
import queue
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .candidate_transfer import candidate_parameters, check_candidate
from .clients import PressureClient, VisionClient
from .engine import Engine, atomic_json
from .model_results import CalculationResultsWidget
from .preparation import AutotuneSettingsPage
from .profiles import empty_parameters, profile_parameters
from .recipes import LIMITS, transition_identity, validate_parameters, validate_recipe
from .roadmap_transition_dialog import (
    RoadmapTransitionDialog,
    default_roadmap_directory,
    load_roadmap_document,
    pose_pixmap,
    roadmap_transition_metadata,
    selection_from_roadmap_transition_metadata,
)
from .trial_context import context_fields, read_context, validate_context

STATES = {
    "disconnected": "Nicht verbunden",
    "connecting": "Verbinde und lese SPS-Werte …",
    "ready": "SPS verbunden · Array- und Bandsteuerung bereit",
    "updating": "Array-Parameter und Bandgeschwindigkeit schreiben und zurücklesen …",
    "applying": "Parameter schreiben und zurücklesen …",
    "waiting": "Kamera wird scharfgeschaltet / wartet auf Durchfahrt · Band manuell bedienen",
    "recording": "Durchfahrt wird aufgezeichnet",
    "saving": "Aufzeichnung wird gespeichert …",
    "selecting": "Ausgewählten Parametersatz übernehmen …",
    "aborting": "Versuch wird abgebrochen und gespeichert …",
}


class NetworkWorker(QThread):
    event = pyqtSignal(str, object)

    def __init__(self, pressure_url, vision_url, output_dir):
        super().__init__()
        self.urls = (pressure_url, vision_url)
        self.output_dir = output_dir
        self.commands = queue.Queue()
        self.stopping = threading.Event()

    def submit(self, name, value=None):
        self.commands.put((name, value))

    def run(self):
        pressure, vision = PressureClient(self.urls[0]), VisionClient(self.urls[1])
        engine = Engine(pressure, vision, self.output_dir, self.event.emit)
        try:
            while not self.stopping.is_set():
                try:
                    name, value = self.commands.get(timeout=0.2)
                except queue.Empty:
                    name, value = None, None
                try:
                    if name == "connect":
                        engine.connect()
                    elif name == "connect_camera":
                        engine.connect_camera()
                    elif name == "start":
                        engine.start(value)
                    elif name == "apply_parameters":
                        engine.apply_parameters(value)
                    elif name == "save":
                        engine.save(**value)
                    elif name == "context":
                        engine.set_context(value)
                    elif name == "conveyor":
                        engine.conveyor(value)
                    elif name == "trigger":
                        engine.trigger()
                    elif name == "abort":
                        engine.abort()
                    elif name == "release":
                        engine.release()
                    engine.tick()
                except Exception as exc:
                    engine.fail(exc)
        finally:
            engine.release()
            pressure.close()
            vision.close()


class AnglePlot(QWidget):
    def __init__(self):
        super().__init__()
        self.samples = []
        self.setMinimumHeight(180)

    def set_samples(self, samples):
        self.samples = samples[-10000:]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f3f6fa"))
        area = self.rect().adjusted(50, 24, -15, -28)
        painter.setPen(QColor("#526174"))
        painter.drawText(10, 17, "Kamera-Achswinkel [°] · Zeit [s] · modulo 180°")
        for angle in (-90, 0, 90):
            y = area.bottom() - (angle + 90) / 180 * area.height()
            painter.drawText(5, int(y + 4), str(angle))
            painter.drawLine(area.left(), int(y), area.right(), int(y))
        if not self.samples:
            return
        first = self.samples[0]["timestamp"]
        duration = max(0.1, self.samples[-1]["timestamp"] - first)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#1671c5"), 2))
        segment = []
        for sample in self.samples:
            angle = sample.get("angle_deg")
            if angle is None:
                if len(segment) > 1:
                    painter.drawPolyline(QPolygonF(segment))
                segment = []
                continue
            segment.append(
                QPointF(
                    area.left() + (sample["timestamp"] - first) / duration * area.width(),
                    area.bottom() - (angle + 90) / 180 * area.height(),
                )
            )
        if len(segment) > 1:
            painter.drawPolyline(QPolygonF(segment))
        painter.drawText(area.left(), self.height() - 6, "0")
        painter.drawText(area.right() - 45, self.height() - 6, f"{duration:.2f}")


class ParameterEditor(QWidget):
    pulse_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setting_parameters = False
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0, 200)
        self.speed.setDecimals(1)
        self.speed.setSuffix(" mm/s")
        form.addRow("Bandgeschwindigkeit (Bandstart bleibt manuell)", self.speed)
        layout.addLayout(form)
        self.table = QTableWidget(4, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Array",
                "Aktiv",
                "Düsen 1–6",
                "Druck [mbar]",
                "Puls [ms]",
                "Verzögerung [ms]",
                "Offset [mm]",
                "Offset mit Bandsollwert",
            ]
        )
        self.rows = []
        for index in range(4):
            self.table.setItem(index, 0, QTableWidgetItem(str(index + 1)))
            enabled, mode = QCheckBox(), QCheckBox()
            nozzle_widget = QWidget()
            nozzle_layout = QHBoxLayout(nozzle_widget)
            nozzle_layout.setContentsMargins(2, 0, 2, 0)
            nozzles = []
            for nozzle in range(6):
                checkbox = QCheckBox(str(nozzle + 1))
                nozzle_layout.addWidget(checkbox)
                nozzles.append(checkbox)
            row = {"enabled": enabled, "nozzles_enabled": nozzles, "conveyor_velocity_delay": mode}
            self.table.setCellWidget(index, 1, enabled)
            self.table.setCellWidget(index, 2, nozzle_widget)
            for column, field in enumerate(LIMITS, 3):
                low, high, kind = LIMITS[field]
                control = QSpinBox() if kind is int else QDoubleSpinBox()
                control.setRange(low, high)
                if kind is float:
                    control.setDecimals(1)
                row[field] = control
                self.table.setCellWidget(index, column, control)
            self.table.setCellWidget(index, 7, mode)
            self.rows.append(row)
        # LIMITS insertion order is pressure, pulse, delay, offset.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(160)
        self.table.setMaximumHeight(190)
        layout.addWidget(self.table)
        for row in self.rows:
            row["pulse_duration_ms"].valueChanged.connect(self.on_pulse_changed)

    def on_pulse_changed(self):
        if not self.setting_parameters:
            self.pulse_changed.emit()

    def set_parameters(self, parameters):
        parameters = validate_parameters(parameters)
        previous_pulses = tuple(row["pulse_duration_ms"].value() for row in self.rows)
        self.setting_parameters = True
        self.speed.setValue(parameters["conveyor_speed_mm_per_sec"])
        for controls, values in zip(self.rows, parameters["arrays"], strict=True):
            controls["enabled"].setChecked(values["enabled"])
            controls["conveyor_velocity_delay"].setChecked(values["conveyor_velocity_delay"])
            for checkbox, value in zip(
                controls["nozzles_enabled"], values["nozzles_enabled"], strict=True
            ):
                checkbox.setChecked(value)
            for key in LIMITS:
                controls[key].setValue(values[key])
        self.setting_parameters = False
        if previous_pulses != tuple(row["pulse_duration_ms"].value() for row in self.rows):
            self.pulse_changed.emit()

    def parameters(self):
        arrays = []
        for index, row in enumerate(self.rows, 1):
            arrays.append(
                {
                    "index": index,
                    "enabled": row["enabled"].isChecked(),
                    "nozzles_enabled": [c.isChecked() for c in row["nozzles_enabled"]],
                    "conveyor_velocity_delay": row["conveyor_velocity_delay"].isChecked(),
                    **{key: row[key].value() for key in LIMITS},
                }
            )
        return validate_parameters(
            {"arrays": arrays, "conveyor_speed_mm_per_sec": self.speed.value()}
        )


class MainWindow(QMainWindow):
    def __init__(self, pressure_url, vision_url, output_dir):
        super().__init__()
        self.setWindowTitle("BiBaZu AutoTune · Parameter-Testreihen")
        self.resize(1300, 850)
        self.context = None
        self.variants = []
        self.results = []
        self.samples = []
        self.editing = -1
        self.state = "disconnected"
        self.manual_capture = False
        self.camera_armed = False
        self.camera_connected = False
        self.motor_pending = False
        self.pressure_state = {}
        self.profile_template = {}
        self.profile_directory = Path(output_dir).parent / "profiles"
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        self.pages = QTabWidget()
        root_layout.addWidget(self.pages)
        self.main_page = QWidget()
        self.pages.addTab(self.main_page, "Hauptseite")
        layout = QVBoxLayout(self.main_page)
        header = QHBoxLayout()
        information = QVBoxLayout()
        header.addLayout(information, 1)
        layout.addLayout(header)
        self.context_label = QLabel(
            "Keine Roadmap geladen · Manuelle Array- und Bandsteuerung ohne Roadmap möglich"
        )
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet("color: #9a6700; font-weight: 600;")
        information.addWidget(self.context_label)
        self.connection_label = QLabel(f"SPS-API: {pressure_url}   Kamera-API: {vision_url}")
        self.connection_label.setWordWrap(True)
        information.addWidget(self.connection_label)
        self.camera_connection_label = QLabel("Kamera: nicht verbunden")
        self.camera_connection_label.setWordWrap(True)
        information.addWidget(self.camera_connection_label)
        preview = QHBoxLayout()
        self.source_preview = QLabel("Ausgangspose")
        self.target_preview = QLabel("Zielpose")
        preview.addWidget(self.source_preview)
        preview.addWidget(self.target_preview)
        header.addLayout(preview)
        self.state_label = QLabel(STATES[self.state])
        information.addWidget(self.state_label)
        information.addStretch()
        actions = QHBoxLayout()
        self.connect_button = self.button(
            actions, "SPS verbinden", lambda: self.worker.submit("connect")
        )
        self.camera_connect_button = self.button(
            actions, "Kamera verbinden", lambda: self.worker.submit("connect_camera")
        )
        self.release_button = self.button(
            actions, "Sitzung beenden", lambda: self.worker.submit("release")
        )
        layout.addLayout(actions)
        actions = QHBoxLayout()
        self.roadmap_button = self.button(actions, "Roadmap auswählen", self.load_roadmap)
        self.profile_button = self.button(actions, "Profil laden", self.load_profile)
        self.prepare_button = self.button(
            actions, "Autotune-Einstellungen", self.open_autotune_settings
        )
        self.load_button = self.button(actions, "Testreihe laden", self.load_recipe)
        self.save_recipe_button = self.button(actions, "Testreihe speichern", self.save_recipe)
        self.add_button = self.button(actions, "Variante kopieren", self.add_variant)
        self.remove_button = self.button(actions, "Variante entfernen", self.remove_variant)
        layout.addLayout(actions)
        autotune_actions = QHBoxLayout()
        self.calculate_button = self.button(
            autotune_actions, "Berechnung durchführen", self.calculate_autotune
        )
        self.import_button = self.button(
            autotune_actions, "Ergebnisse in Ausgangswerte schreiben", self.import_autotune_result
        )
        self.import_button.setToolTip(
            "Ausgewählten Kandidaten nur in den lokalen Editor übernehmen. "
            "Parameter übernehmen schreibt anschließend separat auf die SPS."
        )
        layout.addLayout(autotune_actions)
        self.autotune_status = QLabel("Autotune-Einstellungen prüfen.")
        self.autotune_status.setWordWrap(True)
        layout.addWidget(self.autotune_status)
        splitter = QSplitter()
        self.variant_list = QListWidget()
        self.variant_list.currentRowChanged.connect(self.select_variant)
        splitter.addWidget(self.variant_list)
        self.editor = ParameterEditor()
        self.editor.set_parameters(empty_parameters())
        splitter.addWidget(self.editor)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        trial_actions = QHBoxLayout()
        self.start_button = self.button(trial_actions, "Versuch freigeben", self.start_trial)
        self.trigger_button = self.button(
            trial_actions, "Aufnahme manuell auslösen", lambda: self.worker.submit("trigger")
        )
        self.abort_button = self.button(
            trial_actions, "Abbrechen", lambda: self.worker.submit("abort")
        )
        self.result_button = self.button(
            trial_actions, "Ergebnis übernehmen und Profil speichern", self.save_result
        )
        layout.addLayout(trial_actions)
        conveyor_actions = QHBoxLayout()
        self.apply_parameters_button = self.button(
            conveyor_actions, "Parameter übernehmen", self.apply_parameters
        )
        self.apply_parameters_button.setToolTip(
            "Array-Parameter und Bandgeschwindigkeit auf die SPS schreiben und zurücklesen."
        )
        self.conveyor_start_button = self.button(
            conveyor_actions, "Band starten", lambda: self.request_conveyor(True)
        )
        self.conveyor_stop_button = self.button(
            conveyor_actions, "Band stoppen", lambda: self.request_conveyor(False)
        )
        self.conveyor_label = QLabel("Band: nicht verbunden")
        conveyor_actions.addWidget(self.conveyor_label)
        layout.addLayout(conveyor_actions)
        self.output_tabs = QTabWidget()
        self.autotune_results = CalculationResultsWidget()
        self.output_tabs.addTab(self.autotune_results, "Autotune-Ergebnisse")
        camera_page = QWidget()
        camera_layout = QVBoxLayout(camera_page)
        self.output_tabs.addTab(camera_page, "Versuchsprotokoll")
        layout.addWidget(self.output_tabs, 1)
        self.plot = AnglePlot()
        camera_layout.addWidget(self.plot)
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(
            ["Versuch", "Status", "Endgrund / Fehler", "Kamera-Clip"]
        )
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.itemSelectionChanged.connect(self.select_result)
        camera_layout.addWidget(self.result_table)
        self.result_details = QLabel(
            "Ergebnis auswählen, um Parameter und Winkelverlauf zu vergleichen."
        )
        self.result_details.setWordWrap(True)
        camera_layout.addWidget(self.result_details)
        self.worker = NetworkWorker(pressure_url, vision_url, output_dir)
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.close_if_stopping)
        self.worker.start()
        self.autotune_settings = AutotuneSettingsPage(self.editor.parameters, self)
        self.pages.addTab(self.autotune_settings, "Autotune-Einstellungen")
        self.autotune_settings.context_changed.connect(self.set_context)
        self.autotune_settings.geometry_worker.finished.connect(self.close_if_stopping)
        self.autotune_settings.model_worker.finished.connect(self.close_if_stopping)
        self.autotune_settings.inputs_changed.connect(self.invalidate_autotune_results)
        self.autotune_settings.results_changed.connect(self.show_autotune_results)
        self.autotune_settings.status_changed.connect(self.autotune_status.setText)
        self.autotune_settings.calculation_state_changed.connect(self.update_autotune_controls)
        self.autotune_results.selection_changed.connect(self.update_autotune_controls)
        self.editor.pulse_changed.connect(self.autotune_settings.refresh_status)
        self.autotune_settings.refresh_status()
        self.update_controls()

    def open_autotune_settings(self):
        self.pages.setCurrentWidget(self.autotune_settings)

    def invalidate_autotune_results(self):
        self.autotune_results.set_results(())
        self.update_autotune_controls()

    def show_autotune_results(self, result):
        self.autotune_results.set_results(result.candidates)
        if result.missing:
            self.autotune_results.details.setPlainText("\n".join(result.missing))
        self.update_autotune_controls()

    def update_autotune_controls(self, *_):
        editable = self.state in {"disconnected", "ready"}
        settings = self.autotune_settings
        preview = settings.preview_result
        self.calculate_button.setEnabled(
            editable
            and not settings.calculation_running
            and preview is not None
            and not preview.missing
            and any(c.status == "pending" for c in preview.candidates)
        )
        candidate = self.autotune_results.selected_candidate()
        available = (
            editable
            and settings.model_result is not None
            and candidate is not None
            and any(c is candidate for c in settings.model_result.candidates)
        )
        if available:
            try:
                check_candidate(candidate)
            except ValueError:
                available = False
        self.import_button.setEnabled(bool(available))

    def calculate_autotune(self):
        if self.state not in {"disconnected", "ready"}:
            return
        self.pages.setCurrentWidget(self.main_page)
        self.output_tabs.setCurrentWidget(self.autotune_results)
        self.autotune_settings.calculate_model()

    def import_autotune_result(self):
        try:
            if self.state not in {"disconnected", "ready"}:
                raise ValueError("Versuch oder laufende SPS-Operation zuerst abschließen.")
            candidate = self.autotune_results.selected_candidate()
            result = self.autotune_settings.model_result
            if (
                result is None
                or candidate is None
                or not any(c is candidate for c in result.candidates)
            ):
                raise ValueError("Zuerst einen aktuellen berechneten Kandidaten auswählen.")
            parameters = candidate_parameters(self.editor.parameters(), candidate)
        except ValueError as exc:
            self.autotune_status.setText("Übernahme nicht möglich: " + str(exc))
            return
        self.editor.set_parameters(parameters)
        if 0 <= self.editing < len(self.variants):
            self.remember_edit()
        else:
            self.variants.append({"name": "Ausgangswerte", "parameters": copy.deepcopy(parameters)})
            self.refresh_variants(len(self.variants) - 1)
        self.pages.setCurrentWidget(self.main_page)
        self.autotune_status.setText(
            f"Array {candidate.mount.array_index} / Düse {candidate.mount.nozzle_index} "
            "in lokale Ausgangswerte übernommen. SPS unverändert. " + candidate.message
        )
        self.update_controls()

    def button(self, layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def update_controls(self):
        ready = self.state == "ready"
        editable = self.state in {"disconnected", "ready"}
        self.connect_button.setEnabled(self.state == "disconnected")
        self.camera_connect_button.setEnabled(ready and not self.camera_connected)
        self.release_button.setEnabled(self.state != "disconnected")
        for control in (
            self.load_button,
            self.save_recipe_button,
            self.add_button,
            self.remove_button,
            self.variant_list,
            self.editor,
            self.roadmap_button,
            self.profile_button,
            self.prepare_button,
        ):
            control.setEnabled(editable)
        self.autotune_settings.setEnabled(editable)
        self.update_autotune_controls()
        self.save_recipe_button.setEnabled(
            editable and self.context is not None and bool(self.variants)
        )
        motor = self.pressure_state.get("conveyor") or {}
        stationary = not motor.get("moving", True)
        self.apply_parameters_button.setEnabled(ready and stationary and not self.motor_pending)
        self.start_button.setEnabled(
            ready
            and self.camera_connected
            and self.context is not None
            and stationary
            and not self.motor_pending
        )
        self.trigger_button.setEnabled(
            self.state == "waiting" and self.camera_armed and self.manual_capture
        )
        self.abort_button.setEnabled(self.state not in {"disconnected", "ready"})
        self.result_button.setEnabled(
            ready and bool(self.results) and stationary and not self.motor_pending
        )
        self.conveyor_start_button.setEnabled(
            (ready or self.state == "recording" or (self.state == "waiting" and self.camera_armed))
            and not self.motor_pending
            and stationary
            and not motor.get("fault_code")
            and not motor.get("error")
        )
        self.conveyor_stop_button.setEnabled(
            self.state not in {"disconnected", "connecting"}
            and bool(self.pressure_state.get("connected"))
        )

    def handle_event(self, name, value):
        if name == "state":
            self.state = value
            self.state_label.setText(STATES.get(value, value))
            self.update_controls()
        elif name == "connected":
            if not self.variants:
                self.variants = [{"name": "Ausgangswerte", "parameters": value["parameters"]}]
                self.editing = -1
                self.refresh_variants()
        elif name == "pressure_state":
            self.pressure_state = value
            motor = value.get("conveyor") or {}
            self.conveyor_label.setText(
                f"Band: {'läuft' if motor.get('moving') else 'steht'} · "
                f"Bereit: {'ja' if motor.get('ready') else 'nein'} · "
                f"Fehler: {motor.get('fault_code', '—')}"
            )
            self.update_controls()
        elif name == "conveyor_pending":
            self.motor_pending = value
            self.update_controls()
        elif name == "camera_armed":
            self.camera_armed = value
            self.update_controls()
        elif name == "camera_connection":
            self.camera_connected = value["connected"]
            detail = value.get("error")
            self.camera_connection_label.setText(
                "Kamera: verbunden"
                if self.camera_connected
                else "Kamera: nicht verbunden" + (f" · {detail}" if detail else "")
            )
            self.update_controls()
        elif name == "parameters_applied":
            self.editor.set_parameters(value)
            self.remember_edit()
            self.statusBar().showMessage("Array-Parameter und Bandgeschwindigkeit bestätigt.")
        elif name == "measurements":
            self.samples.extend(value)
            self.samples = self.samples[-10000:]
            self.plot.set_samples(self.samples)
        elif name == "trial_mode":
            self.manual_capture = value == "manual"
            self.update_controls()
        elif name == "result":
            self.results.append(value)
            row = self.result_table.rowCount()
            self.result_table.insertRow(row)
            result = value.get("result", {})
            for column, text in enumerate(
                (
                    value["trial_id"][:10],
                    value["status"],
                    value.get("error") or result.get("error") or result.get("stop_reason", ""),
                    result.get("clip_path") or "",
                )
            ):
                self.result_table.setItem(row, column, QTableWidgetItem(str(text)))
            self.result_table.selectRow(row)
        elif name == "saved":
            self.profile_directory = Path(value["profile_path"]).parent
            self.statusBar().showMessage("Profil gespeichert: " + value["profile_path"])
        elif name == "error":
            self.state_label.setText("Fehler: " + value)
            QMessageBox.warning(self, "AutoTune", value)

    def set_context(self, context, selection=None):
        if self.state not in {"disconnected", "ready"}:
            raise ValueError("Versuch zuerst abschließen.")
        context = validate_context(context) if context is not None else None
        if transition_identity(self.context) != transition_identity(context):
            self.results = []
            self.result_table.setRowCount(0)
            self.samples = []
            self.plot.set_samples([])
            self.profile_template = {}
        self.context = context
        if context is None:
            self.context_label.setText(
                "Kein bestätigter Übergang · Roadmap auswählen oder manuelle Posen bestätigen"
            )
            self.source_preview.setText("Ausgangspose")
            self.target_preview.setText("Zielpose")
            self.autotune_settings.set_context(None)
            self.worker.submit("context", None)
            self.update_controls()
            return
        self.context_label.setStyleSheet("")
        title = context.get("title") or (
            f"{context['source_pose_id']} → {context['target_pose_id']}"
        )
        self.context_label.setText(
            f"{context['part_name']} · {title}"
            f" · {context.get('actuation', '')} · Bandstart und Bandstopp manuell"
        )
        if selection is None:
            selection = selection_from_roadmap_transition_metadata(context)
        self.autotune_settings.set_context(
            context, selection.source_pose.mesh_path if selection else None
        )
        for label, pose, title in (
            (self.source_preview, selection.source_pose if selection else None, "Ausgangspose"),
            (self.target_preview, selection.target_pose if selection else None, "Zielpose"),
        ):
            pixmap = pose_pixmap(pose, 210, 118) if pose else None
            if context.get("kind") == "manual":
                preview = (
                    self.autotune_settings.source_preview
                    if label is self.source_preview
                    else self.autotune_settings.target_preview
                )
                pixmap = preview.pixmap()
            if pixmap is not None and not pixmap.isNull():
                label.setPixmap(
                    pixmap.scaled(
                        210,
                        118,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                label.setText(
                    title
                    + ": "
                    + str(
                        context.get(
                            "source_pose_id" if label is self.source_preview else "target_pose_id",
                            "manuell",
                        )
                    )
                )
        self.worker.submit("context", context)
        self.update_controls()

    def load_roadmap(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Roadmap auswählen",
            str(default_roadmap_directory()),
            "Roadmap (*.json *.yaml *.yml)",
        )
        if path:
            try:
                document = load_roadmap_document(path)
                dialog = RoadmapTransitionDialog(document, self, self.profile_directory)
                if dialog.exec() and dialog.selected_transition:
                    self.set_context(
                        roadmap_transition_metadata(dialog.selected_transition),
                        dialog.selected_transition,
                    )
            except (OSError, ValueError, TypeError, KeyError) as exc:
                QMessageBox.warning(self, "Roadmap", str(exc))

    def load_profile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Profil laden", str(self.profile_directory), "JSON (*.json)"
        )
        if path:
            try:
                profile = json.loads(Path(path).read_text("utf-8"))
                parameters = profile_parameters(profile)
                self.set_context(read_context(profile))
                self.profile_template = profile
                self.variants = [{"name": Path(path).stem, "parameters": parameters}]
                self.editing = -1
                self.refresh_variants()
                self.profile_directory = Path(path).parent
            except (OSError, ValueError, TypeError, KeyError) as exc:
                QMessageBox.warning(self, "Profil", str(exc))

    def request_conveyor(self, start):
        self.motor_pending = True
        self.update_controls()
        self.worker.submit("conveyor", start)

    def apply_parameters(self):
        try:
            self.remember_edit()
            parameters = self.editor.parameters()
            self.state = "updating"
            self.update_controls()
            self.worker.submit("apply_parameters", parameters)
        except ValueError as exc:
            QMessageBox.warning(self, "Parameter", str(exc))

    def remember_edit(self):
        if 0 <= self.editing < len(self.variants):
            self.variants[self.editing]["parameters"] = self.editor.parameters()

    def refresh_variants(self, selected=0):
        self.variant_list.blockSignals(True)
        self.variant_list.clear()
        self.variant_list.addItems([v["name"] for v in self.variants])
        self.variant_list.blockSignals(False)
        self.editing = -1
        self.variant_list.setCurrentRow(selected)
        self.select_variant(selected)

    def select_variant(self, index):
        if index < 0 or index >= len(self.variants):
            return
        try:
            self.remember_edit()
            self.editor.set_parameters(self.variants[index]["parameters"])
            self.editing = index
        except ValueError as exc:
            QMessageBox.warning(self, "Parameter", str(exc))

    def add_variant(self):
        try:
            self.remember_edit()
            self.variants.append(
                {"name": f"Variante {len(self.variants)}", "parameters": self.editor.parameters()}
            )
            self.refresh_variants(len(self.variants) - 1)
        except ValueError as exc:
            QMessageBox.warning(self, "Parameter", str(exc))

    def remove_variant(self):
        if len(self.variants) > 1 and self.editing >= 0:
            self.variants.pop(self.editing)
            self.refresh_variants()

    def start_trial(self):
        try:
            self.remember_edit()
            parameters = self.editor.parameters()
            self.samples = []
            self.plot.set_samples([])
            self.state = "applying"
            self.update_controls()
            self.worker.submit("start", parameters)
        except ValueError as exc:
            QMessageBox.warning(self, "Parameter", str(exc))

    def select_result(self):
        row = self.result_table.currentRow()
        if 0 <= row < len(self.results):
            samples = self.results[row].get("result", {}).get("measurements") or []
            self.plot.set_samples(samples)
            parameters = self.results[row]["parameters"]
            details = [f"Band: {parameters['conveyor_speed_mm_per_sec']:g} mm/s"]
            for array in parameters["arrays"]:
                nozzles = ",".join(str(i) for i, on in enumerate(array["nozzles_enabled"], 1) if on)
                details.append(
                    f"A{array['index']} {'aktiv' if array['enabled'] else 'aus'} · "
                    f"Düsen {nozzles or 'keine'} · {array['pressure_mbar']} mbar · "
                    f"Puls {array['pulse_duration_ms']} ms · Verzögerung {array['delay_ms']} ms · "
                    f"Offset {array['offset_mm']:g} mm"
                )
            self.result_details.setText("\n".join(details))

    def save_result(self):
        row = self.result_table.currentRow()
        if 0 <= row < len(self.results):
            default_name = (
                f"{self.context['part_name']}_Transition_"
                f"{self.context.get('source_pose_id', 'manuell')}-"
                f"{self.context.get('target_pose_id', 'manuell')}_"
                f"{self.context.get('actuation', '')}.json"
            )
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Profil speichern",
                str(self.profile_directory / default_name),
                "JSON (*.json)",
            )
            if not path:
                return
            self.state = "selecting"
            self.update_controls()
            self.worker.submit(
                "save",
                {
                    "parameters": self.results[row]["parameters"],
                    "path": path,
                    "template": self.profile_template,
                },
            )

    def save_recipe(self):
        try:
            self.remember_edit()
            path, _ = QFileDialog.getSaveFileName(
                self, "Testreihe speichern", "testreihe.json", "JSON (*.json)"
            )
            if path:
                atomic_json(
                    path,
                    {
                        "schema_version": 1,
                        **context_fields(self.context),
                        "variants": self.variants,
                    },
                )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Testreihe", str(exc))

    def load_recipe(self):
        path, _ = QFileDialog.getOpenFileName(self, "Testreihe laden", "", "JSON (*.json)")
        if path:
            try:
                recipe = json.loads(Path(path).read_text("utf-8"))
                variants = validate_recipe(recipe, self.context)
                self.set_context(read_context(recipe))
                self.variants = variants
                self.editing = -1
                self.refresh_variants()
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Testreihe", str(exc))

    def close_if_stopping(self):
        if (
            self.worker.stopping.is_set()
            and not self.worker.isRunning()
            and not self.autotune_settings.workers_running()
        ):
            self.close()

    def closeEvent(self, event):
        self.autotune_settings.stop()
        if self.worker.isRunning() or self.autotune_settings.workers_running():
            self.worker.stopping.set()
            self.state_label.setText("Sitzung und Aufnahme werden beendet …")
            event.ignore()
        else:
            event.accept()


def main():
    from .pressure_service import add_plc_arguments, start_bundled

    parser = argparse.ArgumentParser(description="BiBaZu AutoTune · Parameter-Testreihen")
    parser.add_argument("--pressure-url", help="Externe AutoTune-PLC-API statt gebündeltem Dienst")
    parser.add_argument("--vision-url", default="http://127.0.0.1:8766")
    add_plc_arguments(parser)
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data/runs"
    )
    args = parser.parse_args()
    # Validate before starting a QThread so bad CLI arguments yield a clean error.
    for url in (args.pressure_url, args.vision_url):
        if url is None:
            continue
        client = PressureClient(url)
        client.close()
    application = QApplication(sys.argv[:1])
    service, server = None, None
    try:
        if args.pressure_url is None:
            service, server = start_bundled(args)
            args.pressure_url = f"http://127.0.0.1:{server.port}"
        window = MainWindow(args.pressure_url, args.vision_url, args.output_dir)
        window.show()
        return application.exec()
    except (OSError, RuntimeError) as exc:
        QMessageBox.critical(None, "AutoTune", str(exc))
        return 1
    finally:
        if server:
            server.stop()
        if service:
            service.stop()
