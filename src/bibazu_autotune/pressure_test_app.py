"""Standalone pressure-test window. No actuation on normal open/connect."""

from __future__ import annotations

import argparse
import copy
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .clients import PressureClient
from .pressure_service import add_plc_arguments, start_bundled
from .pressure_test import SCALING


class PressureTestWorker(QThread):
    event = pyqtSignal(str, object)

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        self.cancel = threading.Event()
        self.session_id = None
        self.request_id = None

    def submit(self, name):
        self.commands.put((name, self.request_id))

    def await_operation(self, client, response):
        operation = response.get("snapshot_operation_id") or response["operation_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = client.operation(operation)
            if value["status"] != "pending":
                if value["status"] != "succeeded":
                    raise RuntimeError(value.get("error", "SPS-Auftrag fehlgeschlagen."))
                return value
            time.sleep(0.02)
        raise RuntimeError("SPS-Auftrag antwortet nicht; keine Wiederholung.")

    def run(self):
        client = PressureClient(self.url)
        connected = False
        heartbeat = 0
        try:
            while not self.stopping.is_set():
                try:
                    command, request_id = self.commands.get(timeout=0.1)
                except queue.Empty:
                    command, request_id = None, None
                try:
                    if command == "connect":
                        self.await_operation(client, client.acquire())
                        self.session_id = client.session_id
                        self.await_operation(client, client.check_pressure_test())
                        heartbeat = time.monotonic()
                        connected = True
                        self.event.emit("connected", True)
                    elif command == "start" and connected and not self.cancel.is_set():
                        self.await_operation(client, client.start_pressure_test(request_id))
                    if connected:
                        if time.monotonic() - heartbeat >= 0.4:
                            client.heartbeat()
                            heartbeat = time.monotonic()
                        state = client.state()
                        self.event.emit("state", state["pressure_test"])
                except Exception as exc:
                    self.cancel.set()
                    self.event.emit("error", str(exc))
                    connected = False
                    try:
                        client.release()
                    except Exception:
                        pass
                    self.session_id = None
        finally:
            try:
                client.release()
            except Exception:
                pass
            self.session_id = None
            client.close()


class PressureAbortWorker(QThread):
    event = pyqtSignal(str, object)

    def __init__(self, url, token, request_id):
        super().__init__()
        self.url, self.token = url, token
        self.request_id = request_id

    def run(self):
        client = PressureClient(self.url)
        client.session_id = self.token
        try:
            client.abort_pressure_test(self.request_id)
        except Exception as exc:
            self.event.emit("error", "Abbruch nicht bestätigt: " + str(exc))
        finally:
            client.close()


class PressureCurve(QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.setMinimumHeight(220)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f1f5f9"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(55, 35, -25, -35)
        painter.setPen(QColor("#334155"))
        painter.drawText(10, 20, "Druck [bar] · Zeit relativ zum Ventilkommando [ms]")
        painter.drawRect(area)
        if not self.rows:
            painter.drawText(
                75, 65, "Einzelstoß auswählen. Die Kurven werden nach jedem Stoß angezeigt."
            )
            return
        xmin, xmax = min(r["time_ms"] for r in self.rows), max(r["time_ms"] for r in self.rows)
        ymax = max(1.0, max((r.get("bar2") or 0) for r in self.rows) * 1.1)

        def point(t, value):
            return QPointF(
                area.left() + (t - xmin) / max(1, xmax - xmin) * area.width(),
                area.bottom() - value / ymax * area.height(),
            )

        for value in range(int(ymax) + 1):
            painter.setPen(QColor("#cbd5e1"))
            p = point(xmin, value)
            painter.drawLine(QPointF(area.left(), p.y()), QPointF(area.right(), p.y()))
            painter.setPen(QColor("#334155"))
            painter.drawText(10, int(p.y()), str(value))
        for signal, color, label in (
            ("bar1", "#d97706", "Kanal 1"),
            ("bar2", "#0369a1", "Kanal 2"),
        ):
            painter.setPen(QPen(QColor(color), 2))
            segment = []
            for row in self.rows:
                if row.get(signal) is None:
                    if len(segment) > 1:
                        painter.drawPolyline(QPolygonF(segment))
                    segment = []
                else:
                    segment.append(point(row["time_ms"], row[signal]))
            if len(segment) > 1:
                painter.drawPolyline(QPolygonF(segment))
            painter.drawText(
                area.right() - 100, area.top() + (18 if signal == "bar1" else 38), label
            )
        opened = [r["time_ms"] for r in self.rows if r.get("valve")]
        if opened:
            painter.setPen(QPen(QColor("#64748b"), 1, Qt.PenStyle.DashLine))
            for t in (min(opened), max(opened) + 1):
                painter.drawLine(point(t, 0), point(t, ymax))
        painter.setPen(QColor("#334155"))
        for t in (xmin, 0, xmax):
            painter.drawText(point(t, 0) + QPointF(-10, 22), f"{t:g}")


class PressureTestWindow(QMainWindow):
    def __init__(self, url, *, auto_run=False):
        super().__init__()
        self.setWindowTitle("BiBaZu · Drucktest Array 2 / Düse 1")
        self.resize(1100, 760)
        self.auto_run = auto_run
        self.connected = False
        self.busy = False
        self.closing = False
        self.abort_worker = None
        self.result_directory = None
        self.results = []
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.addWidget(
            QLabel("Array 2 · Düse 1 · Kanal 2 · OpenValve5 · Band bleibt ausgeschaltet")
        )
        layout.addWidget(
            QLabel("20 / 50 / 100 ms × 1000–6000 mbar × 5 Wiederholungen = 90 Stöße · Pause 3 s")
        )
        hint = QLabel(SCALING + " · Aufzeichnung über ADS-Serverzeitstempel, 1 ms.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        actions = QHBoxLayout()
        self.connect_button = QPushButton("SPS verbinden")
        self.start_button = QPushButton("Testreihe starten")
        self.abort_button = QPushButton("Abbrechen")
        self.abort_button.setStyleSheet("background: #b91c1c; color: white; font-weight: 600;")
        self.start_button.setEnabled(False)
        self.abort_button.setEnabled(False)
        for button in (self.connect_button, self.start_button, self.abort_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel("Nicht verbunden. Verbinden startet keinen Luftstoß.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 90)
        layout.addWidget(self.progress)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Stoß", "Puls [ms]", "Soll [mbar]", "Wdh.", "Spitze [bar]", "Mittel [bar]", "Status"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(200)
        layout.addWidget(self.table)
        self.curve = PressureCurve()
        layout.addWidget(self.curve, 1)
        self.path_label = QLabel("Messdaten: noch kein Lauf")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        self.worker = PressureTestWorker(url)
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.close_when_stopped)
        self.worker.start()
        self.connect_button.clicked.connect(self.connect_plc)
        self.start_button.clicked.connect(self.start_test)
        self.abort_button.clicked.connect(self.abort_test)
        self.table.itemSelectionChanged.connect(self.select_result)
        if auto_run:
            QTimer.singleShot(0, self.connect_plc)

    def connect_plc(self):
        self.connect_button.setEnabled(False)
        self.worker.cancel.clear()
        self.status.setText("Verbinde und prüfe Kurzimpuls-Schnittstelle …")
        self.worker.submit("connect")

    def start_test(self):
        if not self.connected or self.busy:
            return
        self.busy = True
        self.worker.request_id = uuid.uuid4().hex
        self.worker.cancel.clear()
        self.results = []
        self.table.setRowCount(0)
        self.progress.setValue(0)
        self.start_button.setEnabled(False)
        self.abort_button.setEnabled(True)
        self.status.setText("Testreihe startet; 90 Luftstöße …")
        self.worker.submit("start")

    def abort_test(self):
        self.worker.cancel.set()
        self.abort_button.setEnabled(False)
        self.status.setText("Abbruch angefordert; Ventilschluss wird geprüft …")
        if self.worker.session_id and (
            self.abort_worker is None or not self.abort_worker.isRunning()
        ):
            self.abort_worker = PressureAbortWorker(
                self.worker.url, self.worker.session_id, self.worker.request_id
            )
            self.abort_worker.event.connect(self.handle_event)
            self.abort_worker.finished.connect(self.close_when_stopped)
            self.abort_worker.start()

    def handle_event(self, name, value):
        if name == "connected":
            self.connected = True
            self.start_button.setEnabled(not self.closing)
            self.status.setText("SPS bereit. Start führt die festgelegten 90 Luftstöße aus.")
            if self.auto_run and not self.closing:
                self.auto_run = False
                self.start_test()
        elif name == "error":
            self.status.setText("Fehler: " + value)
            self.connected = self.busy = False
            self.start_button.setEnabled(False)
            self.connect_button.setEnabled(not self.closing)
            self.abort_button.setEnabled(bool(self.worker.session_id))
        elif name == "state":
            if value["status"] == "idle":
                return
            self.busy = value.get("active", False)
            self.start_button.setEnabled(self.connected and not self.busy and not self.closing)
            self.abort_button.setEnabled(self.busy and not self.worker.cancel.is_set())
            self.progress.setValue(value["completed"])
            current = value.get("current", {})
            self.status.setText(
                f"{value['status']} · {value['phase']} · {value['completed']}/90 · "
                f"{current.get('pulse_ms')} ms / {current.get('pressure_mbar')} mbar / "
                f"Wiederholung {current.get('repetition')}"
                + (" · " + value["error"] if value.get("error") else "")
            )
            self.result_directory = value.get("directory")
            self.path_label.setText("Messdaten: " + str(self.result_directory))
            results = value.get("results", [])
            if len(results) != len(self.results):
                self.results = copy.deepcopy(results)
                self.table.setRowCount(len(results))
                for row, result in enumerate(results):
                    values = [
                        result["number"],
                        result["pulse_ms"],
                        result["pressure_mbar"],
                        result["repetition"],
                        f"{result['peak_bar']:.4f}" if result["peak_bar"] is not None else "—",
                        f"{result['mean_open_bar']:.4f}"
                        if result["mean_open_bar"] is not None
                        else "—",
                        "vollständig" if result["complete"] else result["error"],
                    ]
                    for col, text in enumerate(values):
                        self.table.setItem(row, col, QTableWidgetItem(str(text)))
                self.table.selectRow(len(results) - 1)
            if not self.busy and self.closing:
                self.worker.stopping.set()

    def select_result(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.results) and self.result_directory:
            # Small immutable per-pulse files only; all recording itself is in the service worker.
            path = Path(self.result_directory) / f"pulse_{self.results[row]['number']:03d}.csv"
            import csv

            with path.open(encoding="utf-8", newline="") as stream:
                self.curve.rows = [
                    {
                        key: (
                            (float(value) if value else None)
                            if key in {"time_ms", "bar1", "bar2"}
                            else value == "True"
                            if key == "valve"
                            else value
                        )
                        for key, value in values.items()
                    }
                    for values in csv.DictReader(stream)
                ]
            self.curve.update()

    def close_when_stopped(self):
        if (
            self.closing
            and not self.worker.isRunning()
            and not (self.abort_worker and self.abort_worker.isRunning())
        ):
            self.close()

    def closeEvent(self, event):
        if self.worker.isRunning():
            if not self.closing:
                self.closing = True
                if self.busy:
                    self.abort_test()
                else:
                    self.worker.stopping.set()
            event.ignore()
        elif self.abort_worker and self.abort_worker.isRunning():
            event.ignore()
        else:
            event.accept()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_plc_arguments(parser)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data/pressure_tests",
    )
    parser.add_argument(
        "--auto-run",
        action="store_true",
        help="Explizit verbinden UND Testreihe starten; Standard startet nichts.",
    )
    args = parser.parse_args()
    args.pressure_test_mode = True
    args.pressure_test_dir = args.output_dir
    application = QApplication(sys.argv[:1])
    service = server = None
    try:
        service, server = start_bundled(args)
        window = PressureTestWindow(f"http://127.0.0.1:{server.port}", auto_run=args.auto_run)
        window.show()
        return application.exec()
    except Exception as exc:
        QMessageBox.critical(None, "Drucktest", str(exc))
        return 1
    finally:
        if server:
            server.stop()
        if service:
            service.stop()


if __name__ == "__main__":
    main()
