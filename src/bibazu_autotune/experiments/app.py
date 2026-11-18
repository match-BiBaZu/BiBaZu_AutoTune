"""Separate desktop application for experimental acquisition and dataset review."""

from __future__ import annotations

import argparse
import copy
import itertools
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QEvent, QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..analytical_model import AnalyticalPressureModel
from ..app import ParameterEditor
from ..candidate_transfer import candidate_parameters
from ..clients import PressureClient, VisionClient
from ..model_results import CalculationResultsWidget
from ..pose_dialog import PartPoseDialog
from ..pose_preview import render_mesh_preview
from ..preparation import AutotuneSettingsPage, GeometryWorker
from ..pressure_scaling import scale_pressure
from ..profiles import empty_parameters
from ..roadmap_transition_dialog import (
    RoadmapTransitionDialog,
    default_roadmap_directory,
    load_roadmap_document,
    roadmap_transition_metadata,
)
from ..workpiece import WorkpieceSelection
from .dataset import MODEL_FAMILIES, TASKS, DatasetReader
from .models import ClassificationConfig, ExperimentPlan, SweepRange
from .review import AngleCurve, ReviewPage
from .runner import ExperimentRunner
from .storage import CampaignStore, inside

STATES = {
    "disconnected": "Nicht verbunden",
    "connecting": "Verbinde SPS und Kamera …",
    "ready": "Verbunden · Kampagne anlegen oder wiederaufnehmen",
    "gate": "Warte auf Freigabe der nächsten Reihe",
    "applying": "Parameter übertragen …",
    "settling": "Druck einschwingen lassen …",
    "arming": "Kamera scharfschalten …",
    "waiting": "BEREIT · nächstes Bauteil zuführen",
    "recording": "Aufnahme läuft …",
    "saving": "Kamera speichert …",
    "archiving": "Originaldaten archivieren …",
    "disabling": "Versuchsarray deaktivieren …",
    "paused": "Pausiert · Versuch prüfen",
    "complete": "Kampagne abgeschlossen · Arrays aus",
    "stopping": "Bandstopp läuft · Parameterübertragung wartet …",
    "review_pending": "Reihe beendet · unsichere Versuche zur Wiederholung vorgemerkt",
}


def spin(low, high, initial, *, decimals=None):
    widget = QSpinBox() if decimals is None else QDoubleSpinBox()
    if decimals is not None:
        widget.setDecimals(decimals)
    widget.setRange(low, high)
    widget.setValue(initial)
    return widget


def button(layout, title, callback):
    widget = QPushButton(title)
    widget.clicked.connect(lambda checked=False: callback())
    layout.addWidget(widget)
    return widget


class AcquisitionWorker(QThread):
    event = pyqtSignal(str, object)

    def __init__(self, pressure_url, vision_url, *, demo=False):
        super().__init__()
        self.pressure_url, self.vision_url, self.demo = pressure_url, vision_url, demo
        self.commands = queue.PriorityQueue()
        self.sequence = itertools.count()
        self.cancel = threading.Event()
        self.external_stop = threading.Event()
        self.stopping = threading.Event()
        self.generation = 0
        self.session_id = None

    def submit(self, name, *arguments):
        self.commands.put(
            (10, next(self.sequence), self.generation, name, copy.deepcopy(arguments))
        )

    def request_pause(self):
        self.generation += 1
        self.cancel.set()

    def run(self):
        pressure = vision = runner = None
        try:
            if self.demo:
                from .demo import DemoPressure, DemoVision

                pressure = DemoPressure()
                vision = DemoVision(pressure)
            else:
                pressure, vision = PressureClient(self.pressure_url), VisionClient(self.vision_url)
            runner = ExperimentRunner(pressure, vision, self.event.emit)
            runner.external_stop = self.external_stop.is_set
            closing_started = None
            releasing = False
            ending = None
            while True:
                try:
                    _, _, generation, name, arguments = self.commands.get(timeout=0.1)
                except queue.Empty:
                    generation, name, arguments = self.generation, None, ()
                try:
                    if self.cancel.is_set():
                        self.cancel.clear()
                        runner.pause()
                    if self.stopping.is_set():
                        if closing_started is None:
                            closing_started = time.monotonic()
                            runner.pause()
                        name = None
                        if runner.state in {
                            "paused",
                            "ready",
                            "gate",
                            "complete",
                            "review_pending",
                            "disconnected",
                        }:
                            break
                        if time.monotonic() - closing_started > 20:
                            break
                    if name and generation == self.generation:
                        if name == "band_start":
                            runner.conveyor(True)
                        elif name == "band_stop":
                            runner.conveyor(False)
                        elif name == "release":
                            releasing = True
                            runner.pause()
                        elif name == "end_campaign":
                            ending = arguments
                            runner.pause()
                        else:
                            getattr(runner, name)(*arguments)
                    runner.tick()
                    if (
                        ending
                        and runner.state
                        in {
                            "paused",
                            "ready",
                            "gate",
                            "complete",
                            "review_pending",
                            "disconnected",
                        }
                        and not (runner.operation or runner.motor_operation or runner.archive)
                    ):
                        arguments, ending = ending, None
                        runner.end_campaign(*arguments)
                    if releasing and runner.state in {
                        "paused",
                        "ready",
                        "gate",
                        "complete",
                        "review_pending",
                        "disconnected",
                    }:
                        runner.release()
                        releasing = False
                    self.session_id = pressure.session_id
                except Exception as exc:
                    ending = None
                    runner.fail(exc)
        except Exception as exc:
            self.event.emit("error", str(exc))
        finally:
            if runner:
                runner.close()
            for client in (vision, pressure):
                if client:
                    client.close()


class BandStopWorker(QThread):
    completed = pyqtSignal(str)

    def __init__(self, url, token):
        super().__init__()
        self.url, self.token = url, token

    def run(self):
        client = None
        try:
            client = PressureClient(self.url)
            client.session_id = self.token
            result = client.stop_conveyor()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                operation = client.operation(result["operation_id"])
                if operation["status"] != "pending":
                    if operation["status"] != "succeeded":
                        raise RuntimeError(operation.get("error", "Bandstopp fehlgeschlagen"))
                    self.completed.emit("Bandstopp bestätigt.")
                    return
                time.sleep(0.05)
            raise RuntimeError("Bandstopp wurde nicht bestätigt.")
        except Exception as exc:
            self.completed.emit("Bandstopp: " + str(exc))
        finally:
            if client:
                client.close()


class TrainingWindow(QMainWindow):
    def __init__(self, pressure_url, vision_url, output_dir, *, demo=False):
        super().__init__()
        self.setWindowTitle(
            "BiBaZu AutoTune · Versuche und Trainingsdaten" + (" · DEMO" if demo else "")
        )
        self.resize(1450, 950)
        self.state = "disconnected"
        self.command_pending = False
        self.ending_campaign = False
        self.motor_pending = False
        self.can_skip = False
        self.camera_manual = False
        self.capture_mode = None
        self.candidate = None
        self.campaign_path = None
        self.roadmap_path = None
        self.closing = False
        self.band_worker = None
        self.active_dialog = None
        self.live_rows = []
        self.run_context = None
        self.pose_generation = 0
        self.pose_worker = GeometryWorker(self)
        self.pose_worker.completed.connect(self.accept_run_analysis)
        self.pose_worker.finished.connect(self.finish_close)
        self.pressure_url = pressure_url
        self.editor = ParameterEditor()
        parameters = empty_parameters()
        parameters["conveyor_speed_mm_per_sec"] = 50.0
        self.editor.set_parameters(parameters)
        self.settings = AutotuneSettingsPage(self.editor.parameters, self)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        connections = QHBoxLayout()
        self.connect_button = button(connections, "SPS und Kamera verbinden", self.connect_services)
        self.release_button = button(connections, "Sitzung beenden", self.release_session)
        self.band_start = button(connections, "Band starten", self.start_band)
        self.band_stop = button(connections, "Band stoppen", self.stop_band)
        self.band_label = QLabel("Band: nicht verbunden")
        connections.addWidget(self.band_label)
        layout.addLayout(connections)
        endpoint = QLabel(f"SPS: {pressure_url or 'Simulation'} · Kamera: {vision_url}")
        layout.addWidget(endpoint)
        self.status = QLabel(STATES[self.state])
        self.status.setStyleSheet("font-size:18px;font-weight:bold;padding:10px;background:#e2e8f0")
        layout.addWidget(self.status)
        self.pages = QTabWidget()
        self.storage_label = QLabel("Speicher: kompakte Prüfvideos · Mindestreserve 1 GiB")
        layout.addWidget(self.storage_label)
        layout.addWidget(self.pages, 1)
        acquisition = QWidget()
        self.pages.addTab(acquisition, "Versuche aufnehmen")
        acq = QVBoxLayout(acquisition)
        self.setup = QTabWidget()
        acq.addWidget(self.setup, 1)
        first_page = QSplitter(Qt.Orientation.Horizontal)
        self.setup.addTab(first_page, "1 · Bauteil und Raster")
        part = QWidget()
        first_page.addWidget(part)
        pl = QVBoxLayout(part)
        sources = QHBoxLayout()
        button(sources, "Roadmap laden", self.load_roadmap)
        button(sources, "STL laden", self.load_stl)
        button(sources, "Posen bearbeiten", self.edit_poses)
        button(sources, "STL zuordnen", self.assign_mesh)
        button(sources, "Standardordner …", self.configure_source_folders)
        pl.addLayout(sources)
        self.context_label = QLabel("Roadmap oder STL auswählen.")
        self.context_label.setWordWrap(True)
        pl.addWidget(self.context_label)
        previews = QHBoxLayout()
        self.previews = [QLabel("Ausgangspose"), QLabel("Zielpose")]
        for preview in self.previews:
            preview.setMinimumSize(280, 160)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            previews.addWidget(preview)
        pl.addLayout(previews)
        calc = QHBoxLayout()
        button(calc, "Berechnung durchführen", self.settings.calculate_model)
        self.take_candidate = button(
            calc, "Kandidat als realen Aufbau festlegen", self.select_candidate
        )
        pl.addLayout(calc)
        self.results = CalculationResultsWidget()
        pl.addWidget(self.results, 1)
        self.chosen = QLabel("Noch kein Versuchsarray festgelegt.")
        self.chosen.setWordWrap(True)
        pl.addWidget(self.chosen)
        self.raster_panel = QWidget()
        first_page.addWidget(self.raster_panel)
        first_page.setStretchFactor(0, 3)
        first_page.setStretchFactor(1, 2)
        raster_form = QFormLayout(self.raster_panel)
        self.setup.addTab(self.settings, "Modell und Geometrie")
        self.setup.addTab(self.editor, "Ausgangswerte (lokal)")
        raster = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(raster)
        self.setup.addTab(scroll, "2 · Kamera und Kampagne")
        form = QFormLayout(raster)
        self.capture_start_mode = QComboBox()
        self.capture_start_mode.addItem("Bauteil im Bild · ohne Hardwaretrigger", "presence")
        self.capture_start_mode.addItem("Startmodus der Kamera-GUI verwenden", None)
        form.addRow("Durchlauferkennung", self.capture_start_mode)
        self.presence_confirm_ms = spin(50, 500, 100)
        self.presence_confirm_ms.setSuffix(" ms")
        self.presence_confirm_ms.setToolTip(
            "Aufnahme erst nach durchgehend gültiger, vollständig sichtbarer Geometrie starten."
        )
        form.addRow("Bauteilbestätigung", self.presence_confirm_ms)
        self.capture_start_mode.currentIndexChanged.connect(
            lambda: self.presence_confirm_ms.setEnabled(
                self.capture_start_mode.currentData() == "presence"
            )
        )
        self.uncertain_repeat_timing = QComboBox()
        self.uncertain_repeat_timing.addItem("Nach jeder Versuchsreihe anbieten", "series_end")
        self.uncertain_repeat_timing.addItem("Am Kampagnenende gesammelt anbieten", "campaign_end")
        form.addRow("Unsichere Versuche wiederholen", self.uncertain_repeat_timing)
        ranges = QWidget()
        grid = QGridLayout(ranges)
        for column, title in enumerate(("", "Minimum", "Maximum", "Schrittweite")):
            grid.addWidget(QLabel(title), 0, column)
        self.pressures = [spin(0, 6000, 0), spin(0, 6000, 0), spin(1, 6000, 100)]
        self.pulses = [spin(1, 500, 100), spin(1, 500, 100), spin(1, 500, 10)]
        for row, title, values in (
            (1, "SPS-Druck [mbar]", self.pressures),
            (2, "Impulsdauer [ms]", self.pulses),
        ):
            grid.addWidget(QLabel(title), row, 0)
            for column, value in enumerate(values, 1):
                grid.addWidget(value, row, column)
                value.valueChanged.connect(self.update_raster)
        raster_form.addRow(ranges)
        self.repetitions = spin(1, 10000, 5)
        self.repetitions.valueChanged.connect(self.update_raster)
        raster_form.addRow("Wiederholungen je Kombination", self.repetitions)
        reference_hint = QLabel(
            "Analytischer Druck je Impulsdauer für den festgelegten Kandidaten. "
            "Zum Einstellen des Druckrasters den skalierten SPS-Sollwert vergleichen."
        )
        reference_hint.setWordWrap(True)
        raster_form.addRow(reference_hint)
        self.reference_table = QTableWidget(0, 4)
        self.reference_table.setHorizontalHeaderLabels(
            ["Impuls [ms]", "Analytisch [mbar]", "SPS-Sollwert [mbar]", "Hinweise"]
        )
        self.reference_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.reference_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.reference_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.reference_table.setMinimumHeight(200)
        raster_form.addRow(self.reference_table)
        self.reference_status = QLabel("Zuerst einen berechneten Kandidaten festlegen.")
        self.reference_status.setWordWrap(True)
        raster_form.addRow(self.reference_status)
        self.settling = spin(0, 3600, 5, decimals=1)
        form.addRow("Einschwingzeit nach Parameterwechsel [s]", self.settling)
        self.timeout = spin(1, 3600, 60)
        form.addRow("Zeitlimit je Aufnahme [s]", self.timeout)
        self.tolerance = spin(0.1, 90, 10, decimals=1)
        form.addRow("Toleranz um Ziel-Endpose [°]", self.tolerance)
        self.direction = QComboBox()
        self.direction.addItem("Positive Kameradrehung führt zum Ziel", 1)
        self.direction.addItem("Negative Kameradrehung führt zum Ziel", -1)
        form.addRow("Kameradrehsinn", self.direction)
        self.stable_ms = spin(1, 10000, 50)
        self.stable_span = spin(0.1, 90, 5, decimals=1)
        self.gap_ms = spin(1, 1000, 100)
        form.addRow("Stabiler Abschnitt mindestens [ms]", self.stable_ms)
        form.addRow("Winkelspanne im stabilen Abschnitt höchstens [°]", self.stable_span)
        form.addRow("Zeitlücke höchstens [ms]", self.gap_ms)
        self.observable = QCheckBox(
            "Ausgangs-/Endlage sichtbar, Flipachse in Kameraebene und Drehsinn geprüft"
        )
        form.addRow(self.observable)
        self.confirm_setup = QCheckBox("Reales Array, aktive Düsen, Offset und Raster geprüft")
        form.addRow(self.confirm_setup)
        output = QHBoxLayout()
        self.output = QLineEdit(str(output_dir))
        output.addWidget(self.output)
        button(output, "Ausgabeordner wählen", self.choose_output)
        form.addRow("Datenspeicherung", output)
        self.raster_summary = QTextBrowser()
        self.raster_summary.setMaximumHeight(160)
        raster_form.addRow("Geplanter Ablauf", self.raster_summary)
        self.create_button = QPushButton("Kampagne anlegen · Versuchsarrays deaktivieren")
        self.create_button.clicked.connect(self.create_campaign)
        form.addRow(self.create_button)
        self.run_page = QWidget()
        self.setup.addTab(self.run_page, "3 · Durchführung")
        run = QVBoxLayout(self.run_page)
        self.progress = QLabel("Noch keine Kampagne angelegt.")
        self.progress.setWordWrap(True)
        run.addWidget(self.progress)
        self.capture_info = QLabel(
            "Gespeicherte Versuchsdatensätze: 0 · Livekurve ist keine Aufnahme"
        )
        self.saved_attempts = 0
        self.recorded_passages = 0
        run.addWidget(self.capture_info)
        self.run_pose_title = QLabel("Ausgangspose und Zielpose")
        run.addWidget(self.run_pose_title)
        run_poses = QHBoxLayout()
        self.run_previews = [QLabel("Ausgangspose"), QLabel("Zielpose")]
        for preview in self.run_previews:
            preview.setMinimumSize(280, 150)
            preview.setMaximumHeight(170)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            run_poses.addWidget(preview)
        run.addLayout(run_poses)
        actions = QHBoxLayout()
        self.confirm_button = button(actions, "Reihe starten [Leertaste]", self.confirm_series)
        self.pause_button = button(actions, "Pause / Aufnahme abbrechen", self.pause)
        self.retry_button = button(actions, "Versuch wiederholen", lambda: self.send("retry"))
        self.skip_button = button(actions, "Ohne Ersatz fortsetzen", lambda: self.send("skip"))
        self.trigger_button = button(
            actions, "Manueller Kameratrigger", lambda: self.worker.submit("trigger")
        )
        run.addLayout(actions)
        repeats = QHBoxLayout()
        self.repeat_uncertain_button = button(
            repeats, "Unsichere Versuche wiederholen", lambda: self.send("repeat_uncertain")
        )
        self.defer_uncertain_button = button(
            repeats, "Vorerst ohne Wiederholung weiter", lambda: self.send("defer_uncertain")
        )
        self.repeat_info = QLabel("Unsichere Versuche: 0")
        repeats.addWidget(self.repeat_info)
        run.addLayout(repeats)
        resume = QHBoxLayout()
        self.resume_button = button(resume, "Kampagne wiederaufnehmen", self.resume_campaign)
        self.end_campaign_button = button(
            resume, "Kampagne abbrechen / löschen", self.end_campaign_dialog
        )
        button(resume, "In Datenprüfung öffnen", self.open_review)
        run.addLayout(resume)
        self.curve = AngleCurve()
        run.addWidget(self.curve, 1)
        self.log = QTextBrowser()
        self.log.document().setMaximumBlockCount(300)
        run.addWidget(self.log, 1)
        self.review = ReviewPage()
        self.review.storage_changed.connect(
            lambda path: self.worker.submit("refresh_storage", path)
        )
        self.review.review_changed.connect(lambda path: self.worker.submit("refresh_reviews", path))
        self.pages.addTab(self.review, "Daten prüfen")
        training = QWidget()
        self.pages.addTab(training, "Modelltraining")
        tl = QFormLayout(training)
        self.family = QComboBox()
        self.family.addItems(MODEL_FAMILIES)
        self.task = QComboBox()
        self.task.addItems(["Klassifikator", "Regressor"])
        tl.addRow("Modellfamilie", self.family)
        tl.addRow("Aufgabe", self.task)
        self.training_info = QLabel()
        self.training_info.setWordWrap(True)
        tl.addRow(self.training_info)
        self.task.currentIndexChanged.connect(self.update_training)
        self.training_count = QLabel()
        tl.addRow(self.training_count)
        count = QPushButton("Geprüften Datenbestand einlesen")
        count.clicked.connect(self.training_dataset)
        tl.addRow(count)
        pending = QPushButton("Training folgt in einem späteren Schritt")
        pending.setEnabled(False)
        tl.addRow(pending)
        self.settings.selection_changed.connect(self.update_selection)
        self.settings.inputs_changed.connect(self.invalidate_candidate)
        self.settings.results_changed.connect(
            lambda result: self.results.set_results(result.candidates)
        )
        self.settings.status_changed.connect(self.statusBar().showMessage)
        self.editor.parameters_changed.connect(self.settings.refresh_status)
        self.worker = AcquisitionWorker(pressure_url, vision_url, demo=demo)
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.finish_close)
        self.worker.start()
        QApplication.instance().installEventFilter(self)
        self.settings.refresh_status()
        self.update_raster()
        self.update_training()
        self.controls()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space:
            focus = QApplication.focusWidget()
            if (
                self.isActiveWindow()
                and self.state == "gate"
                and (self.pages.currentIndex() == 0 and self.setup.currentWidget() == self.run_page)
                and not isinstance(
                    focus, (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox)
                )
            ):
                if not event.isAutoRepeat():
                    self.confirm_series()
                return True
        return super().eventFilter(watched, event)

    def connect_services(self):
        self.send("connect")

    def send(self, name, *arguments):
        if self.command_pending:
            return
        self.command_pending = True
        self.controls()
        self.worker.submit(name, *arguments)

    def release_session(self):
        self.worker.request_pause()
        self.worker.submit("release")

    def pause(self):
        self.worker.request_pause()
        self.pause_button.setEnabled(False)

    def start_band(self):
        if not self.motor_pending:
            self.motor_pending = True
            self.controls()
            self.worker.submit("band_start")

    def stop_band(self):
        if not self.worker.demo and self.worker.session_id:
            self.worker.external_stop.set()
        self.worker.request_pause()
        if self.worker.demo:
            self.worker.submit("band_stop")
        elif self.worker.session_id and not (self.band_worker and self.band_worker.isRunning()):
            self.band_worker = BandStopWorker(self.pressure_url, self.worker.session_id)
            self.band_worker.completed.connect(self.log.append)
            self.band_worker.finished.connect(self.worker.external_stop.clear)
            self.band_worker.finished.connect(self.finish_close)
            self.band_worker.start()

    def invalidate_candidate(self):
        self.candidate = None
        self.chosen.setText("Kandidat nach Änderungen neu berechnen und festlegen.")
        self.confirm_setup.setChecked(False)
        self.update_pressure_reference()

    def update_selection(self):
        context = self.settings.context
        self.context_label.setText(
            f"{context['part_name']} · {context.get('title', '')} · STL: {self.settings.path}"
            if context
            else "Roadmap oder STL auswählen."
        )
        for preview, key, caption in zip(
            self.previews,
            ("source_quaternion_xyzw", "target_quaternion_xyzw"),
            ("Ausgangspose", "Zielpose"),
            strict=True,
        ):
            if self.settings.analysis and context:
                try:
                    preview.setPixmap(
                        render_mesh_preview(
                            self.settings.path,
                            300,
                            160,
                            quaternion_xyzw=context[key],
                            caption=caption,
                            triangles=self.settings.analysis.preview_triangles,
                        )
                    )
                except (OSError, ValueError, KeyError) as exc:
                    preview.setText(str(exc))
            else:
                preview.setText(caption)
        if self.campaign_path is None:
            self.run_pose_title.setText(
                f"{context['part_name']} · Ausgangs- und Zielpose"
                if context
                else "Ausgangspose und Zielpose"
            )
            for source, target in zip(self.previews, self.run_previews, strict=True):
                pixmap = source.pixmap()
                if pixmap and not pixmap.isNull():
                    target.setPixmap(pixmap)
                else:
                    target.setText(source.text())

    def load_run_poses(self, directory):
        self.pose_generation += 1
        self.run_context = None
        for preview in self.run_previews:
            preview.setText("Kampagnenpose wird geladen …")
        try:
            store = CampaignStore(directory)
            asset = next(a for a in store.manifest["assets"] if a["kind"] == "mesh")
            path = inside(store.directory, asset["path"])
            self.run_context = store.plan.context
            self.run_pose_title.setText(f"{self.run_context['part_name']} · Kampagnenposen")
            snapshot = store.plan.snapshot
            self.pose_worker.commands.put(
                (
                    self.pose_generation,
                    path,
                    snapshot.get("import_unit", "mm"),
                    snapshot.get("material", {}).get("density_kg_m3"),
                )
            )
            if not self.pose_worker.isRunning():
                self.pose_worker.start()
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            for preview in self.run_previews:
                preview.setText("Kampagnenpose nicht verfügbar: " + str(exc))

    def accept_run_analysis(self, generation, analysis, error):
        if generation != self.pose_generation or self.closing or self.run_context is None:
            return
        for preview, key, caption in zip(
            self.run_previews,
            ("source_quaternion_xyzw", "target_quaternion_xyzw"),
            ("Ausgangspose", "Zielpose"),
            strict=True,
        ):
            try:
                if analysis is None:
                    raise ValueError(error)
                preview.setPixmap(
                    render_mesh_preview(
                        analysis.path,
                        300,
                        160,
                        quaternion_xyzw=self.run_context[key],
                        caption=caption,
                        triangles=analysis.preview_triangles,
                    )
                )
            except (ValueError, OSError, KeyError) as exc:
                preview.setText(caption + ": " + str(exc))

    def end_campaign_dialog(self):
        if not self.campaign_path or self.command_pending:
            return
        self.worker.request_pause()
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Kampagne abbrechen / löschen")
        dialog.setText("Die Kampagne wird pausiert. Was soll mit den Daten geschehen?")
        dialog.setInformativeText(
            f"Kampagnenordner:\n{self.campaign_path}\n\n"
            "Abbrechen beendet die Sitzung und erhält alle Daten. Löschen entfernt diesen "
            "Kampagnenordner vollständig. Originalaufnahmen im Kamera-Repo bleiben erhalten."
        )
        keep = dialog.addButton("Abbrechen · Daten behalten", QMessageBox.ButtonRole.AcceptRole)
        delete = dialog.addButton(
            "Abbrechen und Daten löschen", QMessageBox.ButtonRole.DestructiveRole
        )
        dialog.addButton("Nur pausieren", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(keep)
        dialog.exec()
        if dialog.clickedButton() in (keep, delete):
            self.ending_campaign = True
            self.send("end_campaign", dialog.clickedButton() == delete, self.campaign_path)

    def part_dialog(self, path, selection=None):
        dialog = PartPoseDialog(
            path, self.settings.model_panel.density_value(), self, selection=selection
        )
        self.active_dialog = dialog
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.settings.accept_selection(dialog.selection)
        finally:
            self.active_dialog = None
            dialog.deleteLater()

    def source_folder(self, kind):
        fallback = default_roadmap_directory() if kind == "roadmap" else Path.home()
        saved = QSettings("BiBaZu", "AutoTuneTraining").value(f"source_folders/{kind}", "")
        return str(Path(saved) if saved and Path(saved).is_dir() else fallback)

    def configure_source_folders(self):
        preferences = QSettings("BiBaZu", "AutoTuneTraining")
        dialog = QDialog(self)
        dialog.setWindowTitle("Standardordner für die Dateiauswahl")
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel("Roadmap und STL können unterschiedliche Standardordner verwenden.")
        )
        form = QFormLayout()
        layout.addLayout(form)
        fields = {}
        for kind, caption in (("roadmap", "Roadmap"), ("stl", "STL")):
            row = QHBoxLayout()
            field = QLineEdit(preferences.value(f"source_folders/{kind}", ""))
            field.setPlaceholderText(self.source_folder(kind))
            field.setMinimumWidth(380)
            row.addWidget(field)

            def browse(checked=False, field=field, kind=kind, caption=caption):
                path = QFileDialog.getExistingDirectory(
                    dialog,
                    f"Standardordner für {caption}",
                    field.text() or self.source_folder(kind),
                )
                if path:
                    field.setText(path)

            button(row, "Auswählen …", browse)
            form.addRow(caption, row)
            fields[kind] = field
        layout.addWidget(QLabel("Leere Felder verwenden den bisherigen Standardordner."))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.rejected.connect(dialog.reject)

        def save():
            folders = {kind: field.text().strip() for kind, field in fields.items()}
            for folder in folders.values():
                if folder and not Path(folder).is_dir():
                    QMessageBox.warning(
                        dialog, "Standardordner", f"Ordner nicht gefunden: {folder}"
                    )
                    return
            for kind, folder in folders.items():
                key = f"source_folders/{kind}"
                if folder:
                    preferences.setValue(key, str(Path(folder).resolve()))
                else:
                    preferences.remove(key)
            preferences.sync()
            if preferences.status() != QSettings.Status.NoError:
                QMessageBox.warning(
                    dialog, "Standardordner", "Ordner konnten nicht gespeichert werden."
                )
                return
            dialog.accept()

        buttons.accepted.connect(save)
        layout.addWidget(buttons)
        dialog.exec()

    def load_stl(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "STL laden", self.source_folder("stl"), "STL (*.stl)"
        )
        if path:
            self.roadmap_path = None
            self.guarded(lambda: self.part_dialog(path))

    def edit_poses(self):
        if self.settings.path and self.settings.context:
            self.guarded(lambda: self.part_dialog(self.settings.path, self.settings.selection()))

    def assign_mesh(self):
        if not self.settings.context:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "STL zuordnen", self.source_folder("stl"), "STL (*.stl)"
        )
        if path:
            selection = WorkpieceSelection(
                self.settings.context,
                self.settings.path,
                self.settings.unit,
                self.settings.analysis,
            )
            self.guarded(lambda: self.part_dialog(path, selection))

    def load_roadmap(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Roadmap laden", self.source_folder("roadmap"), "Roadmap (*.json *.yaml *.yml)"
        )
        if not path:
            return

        def load():
            dialog = RoadmapTransitionDialog(
                load_roadmap_document(path), self, Path(self.output.text())
            )
            if dialog.exec() and dialog.selected_transition:
                selected = dialog.selected_transition
                self.roadmap_path = Path(path)
                self.settings.set_context(
                    roadmap_transition_metadata(selected), selected.source_pose.mesh_path
                )

        self.guarded(load)

    def select_candidate(self):
        def select():
            candidate = self.results.selected_candidate()
            if candidate is None or self.settings.model_result is None:
                raise ValueError("Zuerst einen aktuellen analytischen Kandidaten berechnen.")
            parameters = candidate_parameters(self.editor.parameters(), candidate)
            self.candidate = candidate
            array = parameters["arrays"][candidate.mount.array_index - 1]
            self.chosen.setText(
                f"Realer Aufbau: Array {candidate.mount.array_index}, {candidate.nozzle_label}, "
                f"Offset {array['offset_mm']} mm; "
                f"Band {parameters['conveyor_speed_mm_per_sec']} mm/s. "
                "Andere Arrays sind im Versuch aus."
            )
            if all(widget.value() == 0 for widget in self.pressures[:2]):
                for widget in self.pressures[:2]:
                    widget.setValue(array["pressure_mbar"])
            self.confirm_setup.setChecked(False)
            self.update_raster()

        self.guarded(select)

    def ranges(self):
        return (
            SweepRange(*(w.value() for w in self.pressures)),
            SweepRange(*(w.value() for w in self.pulses)),
        )

    def update_raster(self, *_):
        self.update_pressure_reference()
        try:
            pressure, pulse = self.ranges()
            p, t = pressure.values(0, 6000), pulse.values(1, 500)
            order = (
                (value, duration)
                for i, duration in enumerate(t)
                for value in (p if i % 2 == 0 else reversed(p))
            )
            combinations = len(p) * len(t)
            preview = " → ".join(f"{p} mbar / {t} ms" for p, t in itertools.islice(order, 30))
            self.raster_summary.setPlainText(
                f"{combinations} Kombinationen × {self.repetitions.value()} Wiederholungen = "
                f"{combinations * self.repetitions.value()} Versuche\n"
                + preview
                + (" → …" if combinations > 30 else "")
            )
        except ValueError as exc:
            self.raster_summary.setPlainText(str(exc))

    def pressure_for_duration(self, duration):
        candidate = self.candidate
        if candidate is None:
            raise ValueError("Zuerst einen berechneten Kandidaten festlegen.")
        configuration = self.settings.model_panel.configuration()
        prediction = AnalyticalPressureModel().predict(
            SimpleNamespace(mass_kg=self.settings.analysis.material.mass_kg),
            candidate.mechanics,
            candidate.impact,
            duration,
            configuration,
        )
        scaled = scale_pressure(
            prediction.gauge_pa, configuration.pressure_scale_a, configuration.pressure_scale_b_pa
        )
        return {"pressure": asdict(prediction), "sps_pressure": asdict(scaled)}

    def update_pressure_reference(self):
        self.reference_table.setRowCount(0)
        if self.candidate is None:
            self.reference_status.setText("Zuerst einen berechneten Kandidaten festlegen.")
            return
        try:
            durations = SweepRange(*(w.value() for w in self.pulses)).values(1, 500)
        except ValueError as exc:
            self.reference_status.setText(str(exc))
            return
        self.reference_table.setRowCount(len(durations))
        failures = 0
        for row, duration in enumerate(durations):
            try:
                result = self.pressure_for_duration(duration)
                analytical = result["pressure"]["gauge_pa"] / 100
                scaled = result["sps_pressure"]["gauge_pa"] / 100
                warnings = (*result["pressure"]["warnings"], *result["sps_pressure"]["warnings"])
                values = (duration, analytical, scaled, "; ".join(warnings) or "OK")
            except (ValueError, OverflowError) as exc:
                values = (duration, None, None, str(exc))
                failures += 1
            for column, value in enumerate(values):
                text = "—" if value is None else f"{value:.2f}" if column in (1, 2) else str(value)
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, value)
                item.setToolTip(text)
                self.reference_table.setItem(row, column, item)
        self.reference_status.setText(
            f"Array {self.candidate.mount.array_index} · {self.candidate.nozzle_label} · "
            f"{len(durations)} Impulswerte"
            + (f" · {failures} Berechnungsfehler" if failures else "")
        )

    def build_plan(self):
        candidate = self.candidate
        if candidate is None or not self.confirm_setup.isChecked():
            raise ValueError(
                "Aktuellen Kandidaten festlegen und realen Aufbau samt Raster bestätigen."
            )
        parameters = candidate_parameters(self.editor.parameters(), candidate)
        for array in parameters["arrays"]:
            if array["index"] != candidate.mount.array_index:
                array.update(enabled=False, nozzles_enabled=[False] * 6)
        pressure, pulse = self.ranges()
        configuration = self.settings.model_panel.configuration()
        analysis = self.settings.analysis
        analytical = {
            str(duration): self.pressure_for_duration(duration) for duration in pulse.values(1, 500)
        }
        plan = ExperimentPlan(
            copy.deepcopy(self.settings.context),
            parameters,
            candidate.mount.array_index,
            pressure,
            pulse,
            ClassificationConfig(
                abs(candidate.turn.signed_angle_deg),
                self.direction.currentData(),
                self.tolerance.value(),
                self.stable_ms.value() / 1000,
                self.stable_span.value(),
                self.gap_ms.value() / 1000,
                self.observable.isChecked(),
            ),
            repetitions=self.repetitions.value(),
            settling_seconds=self.settling.value(),
            trial_timeout_seconds=self.timeout.value(),
            capture_start_mode=self.capture_start_mode.currentData(),
            presence_confirm_seconds=(
                self.presence_confirm_ms.value() / 1000
                if self.capture_start_mode.currentData() == "presence"
                else None
            ),
            uncertain_repeat_timing=self.uncertain_repeat_timing.currentData(),
            snapshot={
                "mesh_sha256": analysis.sha256,
                "import_unit": analysis.unit,
                "geometry": asdict(analysis.geometry),
                "material": asdict(analysis.material),
                "configuration": asdict(configuration),
                "candidate": asdict(candidate),
                "candidate_mount": asdict(candidate.mount),
                "analytical_by_duration": analytical,
            },
        )
        plan.validate()
        return plan

    def create_campaign(self):
        def create():
            plan = self.build_plan()
            assets = {"mesh": str(self.settings.path)}
            if self.roadmap_path:
                assets["roadmap"] = str(self.roadmap_path)
                sidecar = self.roadmap_path.with_name(self.roadmap_path.name + ".consumer.json")
                if sidecar.is_file():
                    assets["roadmap_consumer"] = str(sidecar)
            self.create_button.setEnabled(False)
            self.send("create", self.output.text(), plan, assets, software_metadata())
            self.setup.setCurrentWidget(self.run_page)

        self.guarded(create)

    def resume_campaign(self):
        path = QFileDialog.getExistingDirectory(self, "Kampagne zur Wiederaufnahme auswählen")
        if path:
            self.send("resume", path)

    def confirm_series(self):
        if self.state == "gate" and self.confirm_button.isEnabled():
            self.confirm_button.setEnabled(False)
            self.live_rows = []
            self.curve.set_rows([])
            self.send("confirm")

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "Ausgabeordner auswählen", self.output.text())
        if path:
            self.output.setText(path)

    def open_review(self):
        if self.campaign_path:
            self.review.load(self.campaign_path)
        self.pages.setCurrentWidget(self.review)

    def update_training(self, *_):
        task = TASKS["classifier" if self.task.currentIndex() == 0 else "regressor"]
        self.training_info.setText(
            task["description"] + ". Training ist vorbereitet und noch nicht implementiert. "
            "Aufteilung später nach Bauteil/Transition/Kampagne, damit Wiederholungen nicht "
            "zwischen Trainings- und Testdaten verteilt werden."
        )

    def training_dataset(self):
        store = self.review.store
        if store is None:
            self.training_count.setText("Zuerst eine Kampagne in der Datenprüfung öffnen.")
            return
        self.guarded(
            lambda: self.training_count.setText(
                f"{len(list(DatasetReader([store]).rows()))} nicht ausgeschlossene Versuche; "
                f"{len(DatasetReader([store]).classifier_rows())} "
                "klassifizierbare Trainingsbeispiele."
            )
        )

    def guarded(self, action):
        try:
            action()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, "Versuchsdaten", str(exc))

    def handle_event(self, name, value):
        if name == "state":
            self.command_pending = False
            self.state = value
            self.status.setText(STATES.get(value, value))
            if value == "waiting" and self.capture_mode == "hardware":
                self.status.setText("BEREIT · warte auf Hardwaretrigger, noch keine Aufnahme")
            elif (
                value == "waiting"
                and self.capture_mode == "presence"
                and getattr(self, "capture_guard", None)
            ):
                self.status.setText("BEREIT · warte auf bestätigtes Bauteil")
        elif name == "progress":
            self.can_skip = value["can_skip"]
            point = value["point"]
            self.pending_repeats = value["pending_repeats"]
            self.repeat_info.setText(
                f"Offene unsichere Versuche: {value['pending_repeats']}"
                + (" · Wiederholungsrunde" if value["repeat_active"] else "")
            )
            self.progress.setText(
                f"{value['sweep_slot']} / {value['total']} reguläre Versuchsplätze erledigt "
                "(inkl. unsicher/übersprungen) · "
                f"Kombination {point['index'] + 1}: {point['pressure_mbar']} mbar, "
                f"{point['pulse_duration_ms']} ms · Wiederholung {value['repetition']}\n"
                + value["directory"]
            )
        elif name == "campaign":
            self.campaign_path = value
            self.log.append("Kampagne: " + value)
            self.load_run_poses(value)
            records = CampaignStore(value).trials()
            self.saved_attempts = len(records)
            self.recorded_passages = sum(
                bool(r.get("clip")) and r.get("camera_status") == "completed" for r in records
            )
            self.update_capture_info()
        elif name == "campaign_ended":
            self.ending_campaign = False
            self.campaign_path = None
            self.pose_generation += 1
            self.run_context = None
            self.update_selection()
            self.live_rows = []
            self.curve.set_rows([])
            self.progress.setText(
                "Kampagne abgebrochen · "
                + (
                    "Daten gelöscht"
                    if value["deleted"]
                    else "Löschen fehlgeschlagen"
                    if value.get("delete_requested")
                    else "Daten behalten"
                )
                + "\n"
                + value["directory"]
            )
            self.log.append(self.progress.text())
            if (
                value["deleted"]
                and self.review.store
                and (self.review.store.directory == Path(value["directory"]))
            ):
                self.review.store = None
                self.review.records = self.review.frames = []
                self.review.table.setRowCount(0)
                self.review.select()
                self.review.path_label.setText("Kampagne gelöscht.")
                self.training_count.clear()
        elif name == "result":
            if value.get("media", {}).get("state") == "failed":
                self.log.append(
                    "Prüfvideo nicht verfügbar; Messdaten erhalten: "
                    + (value["media"].get("error") or "Videofehler")
                )
            self.saved_attempts += 1
            self.recorded_passages += (
                bool(value.get("clip")) and value.get("camera_status") == "completed"
            )
            self.update_capture_info()
            self.live_rows = []
            evaluation = value["evaluation"]
            self.log.append(
                f"{value['trial_id'][:10]}: {evaluation['label'] or 'Zu prüfen'} · "
                f"Enddrehung {evaluation['rotation_deg']}° · " + "; ".join(evaluation["reasons"])
            )
        elif name == "measurements":
            # Raw live curve is explicitly separate from the recorded endpoint evaluation.
            for row in value:
                self.live_rows.append({**row, "continuous_angle_deg": row.get("angle_deg")})
            self.live_rows = self.live_rows[-20000:]
            self.curve.set_rows(self.live_rows)
        elif name == "attempt_started":
            self.live_rows = []
            self.curve.set_rows([])
        elif name == "storage_info":
            self.storage_label.setText(
                f"Kampagne ≈ {value['campaign_bytes'] / 1024**2:.1f} MiB · "
                f"Frei: {value['free_bytes'] / 1024**3:.2f} GiB · Mindestreserve 1 GiB"
            )
        elif name == "pressure_state":
            motor = value.get("conveyor") or {}
            self.band_label.setText("Band: " + ("läuft" if motor.get("moving") else "steht"))
        elif name == "motor_pending":
            self.motor_pending = value
        elif name == "capture_mode":
            self.capture_mode = value
        elif name == "capture_guard":
            self.capture_guard = value
            self.camera_manual = value == "manual"
        elif name == "error":
            self.ending_campaign = False
            self.command_pending = False
            self.log.append("Fehler: " + value)
            self.statusBar().showMessage(value)
        self.controls()

    def update_capture_info(self):
        self.capture_info.setText(
            f"Vollständig aufgezeichnete Durchläufe: {self.recorded_passages} · "
            f"Versuchsdatensätze: {self.saved_attempts} · "
            "Livekurve ist keine bestätigte Flip-Auswertung"
        )

    def controls(self):
        editable = self.state in {"disconnected", "ready", "complete"} and not self.closing
        for index in range(self.setup.count() - 1):
            self.setup.widget(index).setEnabled(editable)
        self.create_button.setEnabled(self.state in {"ready", "complete"} and not self.closing)
        self.connect_button.setEnabled(self.state == "disconnected" and not self.closing)
        self.release_button.setEnabled(self.state != "disconnected" and not self.closing)
        self.confirm_button.setEnabled(self.state == "gate" and not self.closing)
        self.pause_button.setEnabled(
            self.state
            not in {
                "disconnected",
                "connecting",
                "ready",
                "complete",
                "paused",
                "disabling",
                "review_pending",
            }
            and not self.closing
        )
        self.retry_button.setEnabled(self.state == "paused" and not self.closing)
        self.skip_button.setEnabled(self.state == "paused" and not self.closing)
        self.trigger_button.setEnabled(
            self.state == "waiting" and self.camera_manual and not self.closing
        )
        self.resume_button.setEnabled(self.state in {"ready", "complete"} and not self.closing)
        self.band_start.setEnabled(
            self.state
            in {
                "ready",
                "gate",
                "paused",
                "waiting",
                "recording",
                "complete",
                "review_pending",
            }
            and not self.closing
        )
        self.band_stop.setEnabled(self.state not in {"disconnected", "connecting"})
        self.repeat_uncertain_button.setEnabled(
            self.state in {"review_pending", "complete"}
            and bool(getattr(self, "pending_repeats", 0))
            and not self.closing
        )
        self.defer_uncertain_button.setEnabled(self.state == "review_pending" and not self.closing)
        self.end_campaign_button.setEnabled(
            bool(self.campaign_path)
            and not self.closing
            and not self.command_pending
            and not self.ending_campaign
        )
        if not self.can_skip:
            self.skip_button.setEnabled(False)
        if self.command_pending or self.motor_pending or self.ending_campaign:
            for widget in (
                self.connect_button,
                self.create_button,
                self.confirm_button,
                self.retry_button,
                self.skip_button,
                self.resume_button,
                self.band_start,
                self.repeat_uncertain_button,
                self.defer_uncertain_button,
            ):
                widget.setEnabled(False)
        if self.ending_campaign:
            self.release_button.setEnabled(False)

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            self.worker.stopping.set()
            self.worker.request_pause()
            self.settings.stop()
            self.pose_worker.stopping.set()
            self.controls()
            self.status.setText("Aufnahme abschließen und Sitzung beenden …")
        if (
            self.worker.isRunning()
            or self.settings.workers_running()
            or self.pose_worker.isRunning()
            or (self.band_worker and self.band_worker.isRunning())
        ):
            event.ignore()
            QTimer.singleShot(200, self.finish_close)
        else:
            event.accept()

    def finish_close(self):
        if self.closing:
            self.close()


def software_metadata():
    result = {"software": "bibazu-autotune", "version": version("bibazu-autotune")}
    try:
        root = Path(__file__).resolve().parents[3]
        options = {
            "cwd": root,
            "capture_output": True,
            "text": True,
            "timeout": 3,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        git = ["git", "-c", f"safe.directory={root.as_posix()}"]
        commit = subprocess.run([*git, "rev-parse", "HEAD"], **options)
        dirty = subprocess.run([*git, "status", "--porcelain"], **options)
        if commit.returncode == 0 and dirty.returncode == 0:
            result.update(git_commit=commit.stdout.strip(), git_dirty=bool(dirty.stdout))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def main():
    from ..pressure_service import add_plc_arguments, start_bundled

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pressure-url", help="Externe AutoTune-SPS-API")
    parser.add_argument("--vision-url", default="http://127.0.0.1:8766")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parents[3] / "data/experiments"
    )
    parser.add_argument("--demo", action="store_true", help="Simulation ohne SPS/Kamera")
    add_plc_arguments(parser)
    args = parser.parse_args()
    application = QApplication(sys.argv[:1])
    service = server = None
    try:
        try:
            __import__("cv2")
        except ImportError as exc:
            raise RuntimeError(
                "OpenCV für Prüfvideos fehlt. Bitte Setup.ps1 erneut ausführen."
            ) from exc
        if not args.demo and args.pressure_url is None:
            service, server = start_bundled(args)
            args.pressure_url = f"http://127.0.0.1:{server.port}"
        window = TrainingWindow(args.pressure_url, args.vision_url, args.output_dir, demo=args.demo)
        window.show()
        return application.exec()
    except (OSError, RuntimeError) as exc:
        QMessageBox.critical(None, "Versuchsdaten", str(exc))
        return 1
    finally:
        if server:
            server.stop()
        if service:
            service.stop()


if __name__ == "__main__":
    sys.exit(main())
