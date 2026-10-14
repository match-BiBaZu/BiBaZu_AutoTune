"""Desktop utility; the worker alone owns network IO and trial orchestration."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QPointF, QThread, QTimer, pyqtSignal
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
    QVBoxLayout,
    QWidget,
)

from .clients import PressureClient, VisionClient
from .engine import Engine, atomic_json
from .recipes import LIMITS, transition_key, validate_parameters, validate_recipe

STATES = {
    "disconnected": "Nicht verbunden",
    "connecting": "Verbinde und lese SPS-Werte …",
    "ready": "Bereit · Bauteil in Ausgangslage bringen und nächsten Versuch freigeben",
    "applying": "Parameter schreiben und zurücklesen …",
    "waiting": "Kamera wird scharfgeschaltet / wartet auf Durchfahrt · Band manuell bedienen",
    "recording": "Durchfahrt wird aufgezeichnet",
    "saving": "Aufzeichnung wird gespeichert …",
    "selecting": "Ausgewählten Parametersatz übernehmen …",
    "save_dialog": "Profil in Pressure Control speichern",
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
                    elif name == "start":
                        engine.start(value)
                    elif name == "save":
                        engine.save(value)
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
    def __init__(self):
        super().__init__()
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
        self.table.setMinimumHeight(180)
        layout.addWidget(self.table)

    def set_parameters(self, parameters):
        parameters = validate_parameters(parameters)
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
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.context_label = QLabel(
            "In Pressure Control eine Transition wählen; Kamera-GUI verbinden."
        )
        self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)
        self.connection_label = QLabel(f"Pressure: {pressure_url}   Kamera: {vision_url}")
        layout.addWidget(self.connection_label)
        self.state_label = QLabel(STATES[self.state])
        layout.addWidget(self.state_label)
        actions = QHBoxLayout()
        self.connect_button = self.button(
            actions, "Verbinden", lambda: self.worker.submit("connect")
        )
        self.release_button = self.button(
            actions, "Sitzung beenden", lambda: self.worker.submit("release")
        )
        self.load_button = self.button(actions, "Testreihe laden", self.load_recipe)
        self.save_recipe_button = self.button(actions, "Testreihe speichern", self.save_recipe)
        self.add_button = self.button(actions, "Variante kopieren", self.add_variant)
        self.remove_button = self.button(actions, "Variante entfernen", self.remove_variant)
        layout.addLayout(actions)
        splitter = QSplitter()
        self.variant_list = QListWidget()
        self.variant_list.currentRowChanged.connect(self.select_variant)
        splitter.addWidget(self.variant_list)
        self.editor = ParameterEditor()
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
        self.plot = AnglePlot()
        layout.addWidget(self.plot)
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(
            ["Versuch", "Status", "Endgrund / Fehler", "Kamera-Clip"]
        )
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.itemSelectionChanged.connect(self.select_result)
        layout.addWidget(self.result_table)
        self.result_details = QLabel(
            "Ergebnis auswählen, um Parameter und Winkelverlauf zu vergleichen."
        )
        self.result_details.setWordWrap(True)
        layout.addWidget(self.result_details)
        self.worker = NetworkWorker(pressure_url, vision_url, output_dir)
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.close_if_stopping)
        self.worker.start()
        self.update_controls()
        QTimer.singleShot(0, lambda: self.worker.submit("connect"))

    def button(self, layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def update_controls(self):
        ready = self.state == "ready"
        self.connect_button.setEnabled(self.state == "disconnected")
        self.release_button.setEnabled(self.state != "disconnected")
        for control in (
            self.load_button,
            self.save_recipe_button,
            self.add_button,
            self.remove_button,
            self.variant_list,
            self.editor,
            self.start_button,
        ):
            control.setEnabled(ready)
        self.save_recipe_button.setEnabled(self.context is not None and bool(self.variants))
        self.trigger_button.setEnabled(self.state == "waiting" and self.manual_capture)
        self.abort_button.setEnabled(self.state not in {"disconnected", "ready"})
        self.result_button.setEnabled(ready and bool(self.results))

    def handle_event(self, name, value):
        if name == "state":
            self.state = value
            self.state_label.setText(STATES.get(value, value))
            self.update_controls()
        elif name == "connected":
            same_transition = transition_key(self.context) == transition_key(value["context"])
            self.context = value["context"]
            self.context_label.setText(
                f"{self.context['part_name']} · {self.context['title']} · "
                f"{self.context['actuation']} · Bandstart und Bandstopp manuell"
            )
            if not same_transition or not self.variants:
                self.variants = [{"name": "Ausgangswerte", "parameters": value["parameters"]}]
                self.results = []
                self.result_table.setRowCount(0)
                self.editing = -1
                self.refresh_variants()
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
            self.state_label.setText("Profildialog geschlossen: " + value["status"])
        elif name == "error":
            self.state_label.setText("Fehler: " + value)
            QMessageBox.warning(self, "AutoTune", value)

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
            self.state = "selecting"
            self.update_controls()
            self.worker.submit("save", self.results[row]["parameters"])

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
                        "roadmap_transition": self.context,
                        "variants": self.variants,
                    },
                )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Testreihe", str(exc))

    def load_recipe(self):
        path, _ = QFileDialog.getOpenFileName(self, "Testreihe laden", "", "JSON (*.json)")
        if path:
            try:
                variants = validate_recipe(json.loads(Path(path).read_text("utf-8")), self.context)
                self.variants = variants
                self.refresh_variants()
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Testreihe", str(exc))

    def close_if_stopping(self):
        if self.worker.stopping.is_set():
            self.close()

    def closeEvent(self, event):
        if self.worker.isRunning():
            self.worker.stopping.set()
            self.state_label.setText("Sitzung und Aufnahme werden beendet …")
            event.ignore()
        else:
            event.accept()


def main():
    parser = argparse.ArgumentParser(description="BiBaZu AutoTune · Parameter-Testreihen")
    parser.add_argument("--pressure-url", default="http://127.0.0.1:8765")
    parser.add_argument("--vision-url", default="http://127.0.0.1:8766")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data/runs"
    )
    args = parser.parse_args()
    # Validate before starting a QThread so bad CLI arguments yield a clean error.
    for url in (args.pressure_url, args.vision_url):
        client = PressureClient(url)
        client.close()
    application = QApplication(sys.argv[:1])
    window = MainWindow(args.pressure_url, args.vision_url, args.output_dir)
    window.show()
    return application.exec()
