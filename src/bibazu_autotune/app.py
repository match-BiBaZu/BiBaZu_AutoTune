"""Operator UI for analytical parameter proposals and explicit SPS operation."""

from __future__ import annotations

import argparse
import copy
import json
import queue
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .candidate_transfer import candidate_parameters, check_candidate
from .clients import PressureClient
from .engine import atomic_json
from .model_results import CalculationResultsWidget
from .plc_session import PlcSession
from .pose_dialog import PartPoseDialog
from .pose_preview import render_mesh_preview
from .preparation import AutotuneSettingsPage
from .profiles import empty_parameters, make_profile, profile_parameters
from .recipes import LIMITS, transition_identity, validate_parameters
from .roadmap_transition_dialog import (
    RoadmapTransitionDialog,
    default_roadmap_directory,
    load_roadmap_document,
    roadmap_transition_metadata,
    selection_from_roadmap_transition_metadata,
)
from .trial_context import normalize_quaternion, read_context
from .workpiece import WorkpieceSelection

STATES = {
    "disconnected": "Nicht verbunden",
    "connecting": "Verbinde und lese SPS-Werte …",
    "ready": "SPS verbunden · Array- und Bandsteuerung bereit",
    "updating": "Parameter schreiben und zurücklesen …",
    "disconnecting": "Sitzung wird beendet …",
}


class NetworkWorker(QThread):
    event = pyqtSignal(str, object)

    def __init__(self, pressure_url):
        super().__init__()
        self.url = pressure_url
        self.commands = queue.Queue()
        self.stopping = threading.Event()

    def submit(self, name, value=None):
        self.commands.put((name, copy.deepcopy(value)))

    def run(self):
        pressure = PressureClient(self.url)
        session = PlcSession(pressure, self.event.emit)
        try:
            while not self.stopping.is_set():
                try:
                    name, value = self.commands.get(timeout=0.1)
                except queue.Empty:
                    name, value = None, None
                try:
                    if name == "connect":
                        session.connect()
                    elif name == "apply_parameters":
                        session.apply_parameters(value)
                    elif name == "conveyor":
                        session.conveyor(value)
                    elif name == "release":
                        session.release()
                    elif name is not None:
                        raise ValueError("Unbekannter SPS-Auftrag: " + name)
                    session.tick()
                except Exception as exc:
                    session.fail(exc)
        finally:
            session.release()
            pressure.close()


class ParameterEditor(QWidget):
    pulse_changed = pyqtSignal()
    parameters_changed = pyqtSignal()

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
            for key in LIMITS:
                row[key].valueChanged.connect(self.on_parameters_changed)
            for checkbox in [
                row["enabled"],
                row["conveyor_velocity_delay"],
                *row["nozzles_enabled"],
            ]:
                checkbox.toggled.connect(self.on_parameters_changed)
        self.speed.valueChanged.connect(self.on_parameters_changed)

    def on_parameters_changed(self):
        if not self.setting_parameters:
            self.parameters_changed.emit()

    def on_pulse_changed(self):
        if not self.setting_parameters:
            self.pulse_changed.emit()

    def set_parameters(self, parameters):
        parameters = validate_parameters(parameters)
        try:
            previous = self.parameters()
        except ValueError:
            previous = None
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
        if previous != parameters:
            self.parameters_changed.emit()

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
    def __init__(self, pressure_url, output_dir):
        super().__init__()
        self.setWindowTitle("BiBaZu AutoTune · Parameter berechnen")
        self.resize(1400, 900)
        self.state = "disconnected"
        self.motor_pending = False
        self.pressure_state = {}
        self.pending_parameters = self.sent_parameters = self.confirmed_parameters = None
        self.parameter_error = ""
        self.syncing_parameters = self.parameters_locked = self.has_local_parameters = False
        self.file_dialog_open = self.closing = False
        self.active_dialog = None
        self.profile_template = {}
        self.profile_directory = Path(output_dir).parent / "profiles"
        self.live_timer = QTimer(self)
        self.live_timer.setSingleShot(True)
        self.live_timer.setInterval(250)
        self.live_timer.timeout.connect(self.flush_live_parameters)
        file_menu = self.menuBar().addMenu("Datei")
        self.load_parameters_action = QAction("Parametersatz laden …", self)
        self.save_parameters_action = QAction("Parametersatz speichern …", self)
        self.load_parameters_action.setShortcut("Ctrl+O")
        self.save_parameters_action.setShortcut("Ctrl+S")
        self.load_parameters_action.triggered.connect(self.load_parameters)
        self.save_parameters_action.triggered.connect(self.save_parameters)
        file_menu.addAction(self.load_parameters_action)
        file_menu.addAction(self.save_parameters_action)
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        self.pages = QTabWidget()
        root_layout.addWidget(self.pages)
        self.main_page = QWidget()
        self.pages.addTab(self.main_page, "Hauptseite")
        layout = QVBoxLayout(self.main_page)
        sources = QHBoxLayout()
        self.roadmap_button = self.button(sources, "Roadmap laden", self.load_roadmap)
        self.stl_button = self.button(sources, "Aus STL laden", self.load_stl)
        self.edit_poses_button = self.button(
            sources, "Bauteil und Posen bearbeiten", self.edit_poses
        )
        self.replace_stl_button = self.button(sources, "STL zuordnen / ersetzen", self.replace_stl)
        self.prepare_button = self.button(
            sources, "Autotune-Einstellungen", self.open_autotune_settings
        )
        layout.addLayout(sources)
        header = QHBoxLayout()
        information = QVBoxLayout()
        self.context_label = QLabel()
        self.context_label.setWordWrap(True)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        self.state_label = QLabel(STATES[self.state])
        information.addWidget(self.context_label)
        information.addWidget(self.source_label)
        information.addWidget(self.state_label)
        information.addStretch()
        header.addLayout(information, 1)
        self.source_preview, self.target_preview = QLabel("Ausgangspose"), QLabel("Zielpose")
        for preview in (self.source_preview, self.target_preview):
            preview.setMinimumSize(260, 150)
            preview.setWordWrap(True)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.addWidget(preview)
        layout.addLayout(header)
        self.editor = ParameterEditor()
        self.editor.set_parameters(empty_parameters())
        layout.addWidget(self.editor)
        sps_actions = QHBoxLayout()
        self.connect_button = self.button(
            sps_actions, "SPS verbinden", lambda: self.worker.submit("connect")
        )
        self.release_button = self.button(sps_actions, "Sitzung beenden", self.release_session)
        self.parameter_lock_button = self.button(
            sps_actions, "Parameter sperren", self.set_parameters_locked
        )
        self.parameter_lock_button.setCheckable(True)
        self.parameter_lock_button.setToolTip(
            "Parameteränderungen und Übernahme sperren. Die Bandbedienung bleibt verfügbar."
        )
        self.apply_parameters_button = self.button(
            sps_actions, "Parameter übernehmen", self.apply_parameters
        )
        self.apply_parameters_button.setToolTip(
            "Jetzt schreiben und zurücklesen. Änderungen werden auch automatisch "
            "nach kurzer Eingabepause übertragen."
        )
        self.conveyor_start_button = self.button(
            sps_actions, "Band starten", lambda: self.request_conveyor(True)
        )
        self.conveyor_stop_button = self.button(
            sps_actions, "Band stoppen", lambda: self.request_conveyor(False)
        )
        layout.addLayout(sps_actions)
        statuses = QHBoxLayout()
        self.live_status = QLabel("SPS-Live: offline · Änderungen bleiben lokal")
        self.conveyor_label = QLabel("Band: nicht verbunden")
        statuses.addWidget(self.live_status, 1)
        statuses.addWidget(self.conveyor_label)
        layout.addLayout(statuses)
        self.autotune_results = CalculationResultsWidget()
        actions = QHBoxLayout()
        self.calculate_button = self.button(
            actions, "Berechnung durchführen", self.calculate_autotune
        )
        self.import_button = self.button(
            actions, "Ergebnisse in Ausgangswerte schreiben", self.import_autotune_result
        )
        self.import_button.setToolTip(
            "Ausgewählten SPS-Sollwert und Offset in den Editor übernehmen. "
            "Bei SPS-Verbindung erfolgt die bestehende Live-Übertragung."
        )
        self.autotune_status = QLabel()
        self.autotune_status.setWordWrap(True)
        self.autotune_results.layout().insertLayout(0, actions)
        self.autotune_results.layout().insertWidget(1, self.autotune_status)
        layout.addWidget(self.autotune_results, 1)
        self.autotune_settings = AutotuneSettingsPage(self.editor.parameters, self)
        self.pages.addTab(self.autotune_settings, "Autotune-Einstellungen")
        self.worker = NetworkWorker(pressure_url)
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.close_if_stopping)
        settings = self.autotune_settings
        settings.selection_changed.connect(self.update_selection_ui)
        settings.inputs_changed.connect(self.invalidate_autotune_results)
        settings.results_changed.connect(self.show_autotune_results)
        settings.status_changed.connect(self.autotune_status.setText)
        settings.calculation_state_changed.connect(self.update_autotune_controls)
        settings.geometry_worker.finished.connect(self.close_if_stopping)
        settings.model_worker.finished.connect(self.close_if_stopping)
        self.autotune_results.selection_changed.connect(self.update_autotune_controls)
        self.editor.pulse_changed.connect(settings.refresh_status)
        self.editor.parameters_changed.connect(self.on_editor_changed)
        self.worker.start()
        settings.refresh_status()
        self.update_controls()

    @property
    def context(self):
        return self.autotune_settings.context

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def open_autotune_settings(self):
        self.pages.setCurrentWidget(self.autotune_settings)

    def can_write_parameters(self):
        return (
            self.state == "ready"
            and self.pressure_state.get("connected", False)
            and not self.parameters_locked
            and not self.motor_pending
            and not self.file_dialog_open
            and not self.parameter_error
            and not self.closing
        )

    def set_parameters_locked(self, locked):
        self.parameters_locked = locked
        self.parameter_lock_button.setChecked(locked)
        self.parameter_lock_button.setText(
            "Parameter entsperren" if locked else "Parameter sperren"
        )
        self.live_timer.stop()
        self.update_controls()

    def on_editor_changed(self, mark_local=True):
        if self.syncing_parameters:
            return
        self.has_local_parameters |= mark_local
        self.live_timer.stop()
        try:
            parameters = self.editor.parameters()
        except ValueError as exc:
            self.parameter_error, self.pending_parameters = str(exc), None
        else:
            self.parameter_error = ""
            target = self.sent_parameters or self.confirmed_parameters
            self.pending_parameters = parameters if parameters != target else None
        self.update_controls()

    def update_live_parameters(self):
        if self.parameter_error:
            text = "Eingabe ungültig: " + self.parameter_error
        elif self.state in {"disconnected", "connecting", "disconnecting"}:
            text = "offline · Änderungen bleiben lokal"
        elif self.sent_parameters is not None:
            text = "Parameter schreiben und zurücklesen …"
            if self.parameters_locked:
                text += " · Parametersperre aktiv"
        elif self.parameters_locked:
            text = "Parameter gesperrt"
            if self.pending_parameters is not None:
                text += " · Änderungen warten auf Entsperren"
        elif self.pending_parameters is not None:
            if self.can_write_parameters():
                if not self.live_timer.isActive():
                    self.live_timer.start()
                text = "Änderungen werden automatisch übertragen …"
            else:
                text = "Änderungen warten auf Abschluss der laufenden Operation"
        else:
            text = "Werte bestätigt"
        self.live_status.setText("SPS-Live: " + text)

    def flush_live_parameters(self):
        if self.pending_parameters is not None and self.can_write_parameters():
            self.apply_parameters()

    def apply_parameters(self):
        if not self.can_write_parameters():
            return
        try:
            parameters = self.editor.parameters()
            self.live_timer.stop()
            self.pending_parameters = None
            self.sent_parameters = parameters
            self.state = "updating"
            self.update_controls()
            self.worker.submit("apply_parameters", parameters)
        except ValueError as exc:
            QMessageBox.warning(self, "Parameter", str(exc))

    def release_session(self):
        self.live_timer.stop()
        self.pending_parameters = None
        self.state = "disconnecting"
        self.update_controls()
        self.worker.submit("release")

    def request_conveyor(self, start):
        self.motor_pending = True
        self.update_controls()
        self.worker.submit("conveyor", start)

    def invalidate_autotune_results(self):
        self.autotune_results.set_results(())
        self.update_autotune_controls()

    def show_autotune_results(self, result):
        self.autotune_results.set_results(result.candidates)
        if result.missing:
            self.autotune_results.details.setPlainText("\n".join(result.missing))
        self.update_autotune_controls()

    def update_autotune_controls(self, *_):
        editable = self.state in {"disconnected", "ready"} and not self.file_dialog_open
        settings = self.autotune_settings
        preview = settings.preview_result
        self.calculate_button.setEnabled(
            bool(
                editable
                and not settings.calculation_running
                and preview is not None
                and not preview.missing
                and any(c.status == "pending" for c in preview.candidates)
            )
        )
        candidate = self.autotune_results.selected_candidate()
        available = (
            editable
            and not self.parameters_locked
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
        if self.state not in {"disconnected", "ready"} or self.file_dialog_open:
            return
        self.pages.setCurrentWidget(self.main_page)
        self.autotune_settings.calculate_model()

    def import_autotune_result(self):
        try:
            if self.parameters_locked:
                raise ValueError("Parameter zuerst entsperren.")
            if self.state not in {"disconnected", "ready"} or self.file_dialog_open:
                raise ValueError("Laufende Operation zuerst abschließen.")
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
        self.pages.setCurrentWidget(self.main_page)
        array = parameters["arrays"][candidate.mount.array_index - 1]
        self.autotune_status.setText(
            f"Array {candidate.mount.array_index} / {candidate.nozzle_label} "
            f"mit skaliertem SPS-Sollwert {array['pressure_mbar']} mbar "
            "in Ausgangswerte übernommen. "
            "Bei SPS-Verbindung erfolgt die Live-Übertragung. " + candidate.message
        )
        self.update_controls()

    def update_controls(self):
        ready = self.state == "ready"
        editable = self.state in {"disconnected", "ready"} and not self.closing
        pending = (
            self.pending_parameters is not None
            or self.sent_parameters is not None
            or bool(self.parameter_error)
        )
        selection_editable = editable and not self.file_dialog_open and not (ready and pending)
        self.connect_button.setEnabled(self.state == "disconnected" and not self.closing)
        self.release_button.setEnabled(self.state != "disconnected")
        for control in (
            self.roadmap_button,
            self.stl_button,
            self.replace_stl_button,
            self.edit_poses_button,
        ):
            control.setEnabled(selection_editable)
        self.prepare_button.setEnabled(editable and not self.file_dialog_open)
        self.editor.setEnabled(
            (editable or self.state == "updating") and not self.parameters_locked
        )
        self.parameter_lock_button.setEnabled(editable or self.state == "updating")
        self.autotune_settings.setEnabled(editable)
        self.load_parameters_action.setEnabled(selection_editable and not self.parameters_locked)
        self.save_parameters_action.setEnabled(
            editable and not self.parameter_error and not self.file_dialog_open
        )
        self.apply_parameters_button.setEnabled(self.can_write_parameters())
        motor = self.pressure_state.get("conveyor") or {}
        self.conveyor_start_button.setEnabled(
            ready
            and not self.motor_pending
            and not pending
            and not motor.get("moving", True)
            and not motor.get("fault_code")
            and not motor.get("error")
        )
        self.conveyor_stop_button.setEnabled(
            self.state not in {"disconnected", "connecting"}
            and bool(self.pressure_state.get("connected"))
        )
        self.update_autotune_controls()
        self.update_live_parameters()

    def handle_event(self, name, value):
        if name == "state":
            self.state = value
            if value == "disconnected":
                self.live_timer.stop()
                self.pending_parameters = self.sent_parameters = self.confirmed_parameters = None
            self.state_label.setText(STATES.get(value, value))
        elif name == "connected":
            self.confirmed_parameters = copy.deepcopy(value["parameters"])
            if not self.has_local_parameters:
                self.syncing_parameters = True
                try:
                    self.editor.set_parameters(value["parameters"])
                finally:
                    self.syncing_parameters = False
            self.on_editor_changed(mark_local=False)
        elif name == "pressure_state":
            self.pressure_state = value
            if self.sent_parameters is None and value.get("parameters") is not None:
                self.confirmed_parameters = copy.deepcopy(value["parameters"])
                if self.pending_parameters == self.confirmed_parameters:
                    self.pending_parameters = None
                    self.live_timer.stop()
            motor = value.get("conveyor") or {}
            self.conveyor_label.setText(
                f"Band: {'läuft' if motor.get('moving') else 'steht'} · "
                f"Fehler: {motor.get('fault_code', '—')}"
            )
        elif name == "conveyor_pending":
            self.motor_pending = value
        elif name == "parameters_applied":
            self.confirmed_parameters = copy.deepcopy(value)
            if self.pending_parameters is None and not self.parameter_error:
                self.syncing_parameters = True
                try:
                    self.editor.set_parameters(value)
                finally:
                    self.syncing_parameters = False
            elif self.pending_parameters == value:
                self.pending_parameters = None
            self.sent_parameters = None
            self.statusBar().showMessage("Parameter bestätigt.")
        elif name == "error":
            self.live_timer.stop()
            self.pending_parameters = self.sent_parameters = None
            self.state_label.setText("Fehler: " + value)
            QMessageBox.warning(self, "AutoTune", value)
        self.update_controls()

    def set_context(self, context, selection=None):
        if self.state not in {"disconnected", "ready"}:
            raise ValueError("Laufende SPS-Operation zuerst abschließen.")
        previous = transition_identity(self.context)
        if context and context.get("kind") != "manual" and selection is None:
            selection = selection_from_roadmap_transition_metadata(context)
        mesh_path = selection.source_pose.mesh_path if selection else None
        self.autotune_settings.set_context(context, mesh_path)
        if transition_identity(self.context) != previous:
            self.profile_template = {}
        self.update_controls()

    def accept_selection(self, selection):
        previous = transition_identity(self.context)
        self.autotune_settings.accept_selection(selection)
        if transition_identity(self.context) != previous:
            self.profile_template = {}
        self.update_controls()

    def update_selection_ui(self):
        settings = self.autotune_settings
        context = self.context or settings.unconfirmed_context
        if context is None:
            self.context_label.setText("Kein Bauteil ausgewählt · Roadmap laden oder Aus STL laden")
        else:
            origin = "STL" if context.get("kind") == "manual" else "Roadmap"
            turn = (
                f"{context['rotation_axis'].upper()} {context['signed_angle_deg']:+g}°"
                if "rotation_axis" in context
                else context.get("title", "Posen prüfen")
            )
            self.context_label.setText(f"{context['part_name']} · {origin} · {turn}")
        self.source_label.setText(
            f"STL: {settings.path} · Einheit: {settings.unit}"
            if settings.path
            else "Noch keine STL zugeordnet."
        )
        manual = bool(context and context.get("kind") == "manual")
        self.edit_poses_button.setVisible(manual and settings.path is not None)
        self.replace_stl_button.setVisible(context is not None)
        for preview, key, caption in (
            (self.source_preview, "source_quaternion_xyzw", "Ausgangspose"),
            (self.target_preview, "target_quaternion_xyzw", "Zielpose"),
        ):
            preview.clear()
            if settings.analysis is None:
                preview.setText(caption + ": " + settings.geometry_error)
                continue
            try:
                quaternion = normalize_quaternion((context or {}).get(key))
                preview.setPixmap(
                    render_mesh_preview(
                        settings.path,
                        280,
                        160,
                        quaternion_xyzw=quaternion,
                        caption=caption,
                        triangles=settings.analysis.preview_triangles,
                    )
                )
            except (ValueError, OSError) as exc:
                preview.setText(caption + ": " + str(exc))
        self.update_controls()

    @contextmanager
    def file_operation(self):
        self.file_dialog_open = True
        self.live_timer.stop()
        self.update_controls()
        try:
            yield
        finally:
            self.file_dialog_open = False
            self.update_controls()

    def can_select(self):
        return (
            self.state in {"disconnected", "ready"}
            and not self.file_dialog_open
            and not self.closing
        )

    def show_part_dialog(self, path, selection=None, expected_hash=None):
        density = self.autotune_settings.model_panel.density_value()
        dialog = PartPoseDialog(
            path, density, self, selection=selection, expected_hash=expected_hash
        )
        self.active_dialog = dialog
        dialog.worker.finished.connect(self.close_if_stopping)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and not self.closing:
                self.accept_selection(dialog.selection)
        finally:
            self.active_dialog = None
            dialog.deleteLater()

    def load_stl(self):
        if not self.can_select():
            return
        with self.file_operation():
            path, _ = QFileDialog.getOpenFileName(self, "Aus STL laden", "", "STL (*.stl)")
            if path:
                try:
                    self.show_part_dialog(path)
                except (OSError, ValueError) as exc:
                    QMessageBox.warning(self, "Bauteil", str(exc))

    def edit_poses(self):
        settings = self.autotune_settings
        context = self.context or settings.unconfirmed_context
        if (
            not self.can_select()
            or not context
            or context.get("kind") != "manual"
            or not settings.path
        ):
            return
        with self.file_operation():
            selection = WorkpieceSelection(context, settings.path, settings.unit, settings.analysis)
            try:
                self.show_part_dialog(settings.path, selection)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Bauteil", str(exc))

    def replace_stl(self):
        settings = self.autotune_settings
        context = self.context or settings.unconfirmed_context
        if not self.can_select() or context is None:
            return
        with self.file_operation():
            path, _ = QFileDialog.getOpenFileName(
                self, "STL zuordnen / ersetzen", "", "STL (*.stl)"
            )
            if path:
                try:
                    selection = WorkpieceSelection(
                        context, settings.path, settings.unit, settings.analysis
                    )
                    expected = context.get("mesh_sha256") if settings.path is None else None
                    self.show_part_dialog(path, selection, expected)
                except (OSError, ValueError) as exc:
                    QMessageBox.warning(self, "STL", str(exc))

    def load_roadmap(self):
        if not self.can_select():
            return
        with self.file_operation():
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Roadmap laden",
                str(default_roadmap_directory()),
                "Roadmap (*.json *.yaml *.yml)",
            )
            if path:
                try:
                    dialog = RoadmapTransitionDialog(
                        load_roadmap_document(path), self, self.profile_directory
                    )
                    if dialog.exec() and dialog.selected_transition:
                        self.set_context(
                            roadmap_transition_metadata(dialog.selected_transition),
                            dialog.selected_transition,
                        )
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    QMessageBox.warning(self, "Roadmap", str(exc))

    def load_parameters(self):
        if not self.can_select() or self.parameters_locked:
            return
        with self.file_operation():
            path, _ = QFileDialog.getOpenFileName(
                self, "Parametersatz laden", str(self.profile_directory), "JSON (*.json)"
            )
            if path:
                try:
                    profile = json.loads(Path(path).read_text("utf-8"))
                    parameters = profile_parameters(profile)
                    if any(
                        profile.get(key) is not None
                        for key in ("trial_context", "roadmap_transition")
                    ):
                        self.set_context(read_context(profile))
                    self.profile_template = profile
                    self.editor.set_parameters(parameters)
                    # Loading is an explicit local choice even if values equal the empty editor.
                    self.on_editor_changed()
                    self.profile_directory = Path(path).parent
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    QMessageBox.warning(self, "Parametersatz", str(exc))

    def save_parameters(self):
        if not self.can_select():
            return
        with self.file_operation():
            try:
                profile = make_profile(
                    self.editor.parameters(),
                    self.context,
                    self.pressure_state.get("machine_metadata"),
                    self.profile_template,
                    self.pressure_state.get("conveyor"),
                )
                path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Parametersatz speichern",
                    str(self.profile_directory / "parametersatz.json"),
                    "JSON (*.json)",
                )
                if path:
                    atomic_json(path, profile)
                    self.profile_directory = Path(path).parent
                    self.statusBar().showMessage("Parametersatz gespeichert: " + path)
            except (OSError, ValueError, KeyError) as exc:
                QMessageBox.warning(self, "Parametersatz", str(exc))

    def close_if_stopping(self):
        dialog_running = self.active_dialog and self.active_dialog.worker.isRunning()
        if (
            self.closing
            and not self.worker.isRunning()
            and not self.autotune_settings.workers_running()
            and not dialog_running
        ):
            self.close()

    def closeEvent(self, event):
        self.closing = True
        self.live_timer.stop()
        if self.active_dialog:
            self.active_dialog.reject()
        self.autotune_settings.stop()
        if self.worker.isRunning() or self.autotune_settings.workers_running():
            self.worker.stopping.set()
            self.state_label.setText("Sitzung wird beendet …")
            event.ignore()
        else:
            event.accept()


def main():
    from .pressure_service import add_plc_arguments, start_bundled

    parser = argparse.ArgumentParser(description="BiBaZu AutoTune · Parameter berechnen")
    parser.add_argument("--pressure-url", help="Externe AutoTune-PLC-API statt gebündeltem Dienst")
    parser.add_argument("--vision-url", help=argparse.SUPPRESS)  # Accepted for old launch commands.
    add_plc_arguments(parser)
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data/runs"
    )
    args = parser.parse_args()
    if args.pressure_url is not None:
        client = PressureClient(args.pressure_url)
        client.close()
    application = QApplication(sys.argv[:1])
    service = server = None
    try:
        if args.pressure_url is None:
            service, server = start_bundled(args)
            args.pressure_url = f"http://127.0.0.1:{server.port}"
        window = MainWindow(args.pressure_url, args.output_dir)
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


if __name__ == "__main__":
    main()
