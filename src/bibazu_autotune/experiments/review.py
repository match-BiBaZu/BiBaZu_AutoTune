"""Recorded angle plots and a small, usable dataset review surface."""

from __future__ import annotations

import csv

import numpy as np
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dataset import DatasetReader
from .models import LABELS
from .storage import CampaignStore, inside, read_json


class AngleCurve(QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.marker = None
        self.setMinimumHeight(190)

    def set_rows(self, rows):
        self.rows = rows
        self.marker = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f1f5f9"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(55, 25, -15, -35)
        painter.setPen(QColor("#334155"))
        painter.drawText(12, 17, "Winkel [°] · Zeit relativ zum ersten Messpunkt [s]")
        painter.drawRect(area)
        valid = [r for r in self.rows if r.get("continuous_angle_deg") is not None]
        if not valid:
            painter.drawText(70, 60, "Noch keine Winkelmessreihe.")
            return
        start, end = self.rows[0]["timestamp"], self.rows[-1]["timestamp"]
        low = min(-10, min(r["continuous_angle_deg"] for r in valid))
        high = max(10, max(r["continuous_angle_deg"] for r in valid))

        def point(stamp, angle):
            return QPointF(
                area.left() + (stamp - start) / max(1e-9, end - start) * area.width(),
                area.bottom() - (angle - low) / (high - low) * area.height(),
            )

        for value in (low, (low + high) / 2, high):
            pos = point(start, value)
            painter.setPen(QColor("#cbd5e1"))
            painter.drawLine(QPointF(area.left(), pos.y()), QPointF(area.right(), pos.y()))
            painter.setPen(QColor("#334155"))
            painter.drawText(3, int(pos.y()), f"{value:.0f}")
        painter.setPen(QPen(QColor("#0369a1"), 2))
        segment = []
        for row in self.rows:
            if row.get("continuous_angle_deg") is None or row.get("gap"):
                if len(segment) > 1:
                    painter.drawPolyline(QPolygonF(segment))
                segment = []
            if row.get("continuous_angle_deg") is not None:
                segment.append(point(row["timestamp"], row["continuous_angle_deg"]))
        if len(segment) > 1:
            painter.drawPolyline(QPolygonF(segment))
        if self.marker is not None:
            painter.setPen(QPen(QColor("#d97706"), 1))
            painter.drawLine(point(self.marker, low), point(self.marker, high))
        painter.setPen(QColor("#334155"))
        painter.drawText(area.left(), area.bottom() + 23, "0")
        painter.drawText(area.right() - 60, area.bottom() + 23, f"{end - start:.3f}")


class ReviewPage(QWidget):
    def __init__(self):
        super().__init__()
        self.store = None
        self.records = []
        self.frames = []
        self.frame_directory = None
        root = QVBoxLayout(self)
        actions = QHBoxLayout()
        for title, callback in (
            ("Kampagne öffnen", self.open_campaign),
            ("Aktualisieren", self.refresh),
            ("CSV exportieren", self.export),
        ):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, action=callback: action())
            actions.addWidget(button)
        self.filter = QComboBox()
        self.filter.addItems(["Alle Versuche", "Zu prüfen", *LABELS, "Ausgeschlossen"])
        self.filter.currentTextChanged.connect(self.refresh)
        actions.addWidget(self.filter)
        root.addLayout(actions)
        self.path_label = QLabel("Noch keine Kampagne geöffnet.")
        self.path_label.setWordWrap(True)
        root.addWidget(self.path_label)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Versuch",
                "Kombination / Wdh.",
                "Druck [mbar]",
                "Dauer [ms]",
                "Klasse",
                "Qualität",
                "Ausgeschlossen",
                "Grund",
            ]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.select)
        root.addWidget(self.table, 1)
        split = QSplitter()
        self.curve = AngleCurve()
        split.addWidget(self.curve)
        self.image_label = QLabel("Originalbild auswählen")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(300, 180)
        split.addWidget(self.image_label)
        root.addWidget(split, 1)
        self.frame_label = QLabel()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self.show_frame)
        root.addWidget(self.slider)
        root.addWidget(self.frame_label)
        self.details = QLabel()
        self.details.setWordWrap(True)
        root.addWidget(self.details)
        edit = QHBoxLayout()
        self.label = QComboBox()
        self.label.addItems(["Zu prüfen", *LABELS])
        self.excluded = QCheckBox("Vom Training ausschließen")
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Begründung für Korrektur / Ausschluss / Wiedereinschluss")
        self.save = QPushButton("Prüfung speichern")
        self.save.clicked.connect(self.annotate)
        for widget in (self.label, self.excluded, self.reason, self.save):
            edit.addWidget(widget)
        root.addLayout(edit)

    def open_campaign(self):
        path = QFileDialog.getExistingDirectory(self, "Kampagnenordner auswählen")
        if path:
            self.load(path)

    def load(self, path):
        try:
            self.store = CampaignStore(path)
            self.path_label.setText(str(self.store.directory))
            self.refresh()
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Datenprüfung", str(exc))

    def refresh(self, *_):
        if self.store is None:
            return
        try:
            rows = list(DatasetReader([self.store]).rows(include_excluded=True))
            wanted = self.filter.currentText()
            self.records = [
                row
                for row in rows
                if wanted == "Alle Versuche"
                or (
                    wanted == "Zu prüfen"
                    and (
                        row["label"] is None or (row["quality"] != "valid" and not row["reviewed"])
                    )
                )
                or wanted == row["label"]
                or (wanted == "Ausgeschlossen" and row["excluded"])
            ]
            self.table.blockSignals(True)
            self.table.setRowCount(len(self.records))
            for index, row in enumerate(self.records):
                values = [
                    row["trial_id"][:10],
                    f"{row['point_index'] + 1} / {row['repetition']}",
                    row["input_pressure_mbar"],
                    row["input_pulse_duration_ms"],
                    row["label"] or "Zu prüfen",
                    row["quality"],
                    "Ja" if row["excluded"] else "Nein",
                    row["review_reason"] or row["quality_reasons"],
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    self.table.setItem(index, column, item)
            self.table.blockSignals(False)
            if self.records:
                self.table.selectRow(0)
            self.select()
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Datenprüfung", str(exc))

    def selected(self):
        index = self.table.currentRow()
        return self.records[index] if 0 <= index < len(self.records) else None

    def select(self):
        self.frames = []
        self.slider.setEnabled(False)
        self.image_label.clear()
        self.curve.set_rows([])
        self.details.clear()
        self.frame_label.clear()
        row = self.selected()
        self.save.setEnabled(row is not None)
        if row is None:
            return
        self.label.setCurrentText(row["label"] or "Zu prüfen")
        self.excluded.setChecked(row["excluded"])
        self.reason.setText(row["review_reason"])
        directory = self.store.trial_directory(row["trial_id"])
        try:
            path = directory / "measurements.csv"
            if path.exists():
                with path.open(encoding="utf-8", newline="") as stream:
                    samples = list(csv.DictReader(stream))
                for sample in samples:
                    sample["timestamp"] = float(sample["timestamp"])
                    sample["continuous_angle_deg"] = (
                        float(sample["continuous_angle_deg"])
                        if sample["continuous_angle_deg"]
                        else None
                    )
                self.curve.set_rows(samples)
            record = read_json(directory / "trial.json")
            self.details.setText(
                f"Status: {record['status']} · Auflösung: {record.get('resolution') or 'offen'} · "
                f"Enddrehung: {row['outcome_rotation_deg']}° · "
                f"Maximaldrehung: {row['outcome_max_rotation_deg']}° · "
                + (record.get("error") or row["quality_reasons"])
            )
            if record.get("clip"):
                self.frame_directory = inside(directory, record["clip"])
                self.frames = read_json(self.frame_directory / "manifest.json")["frames"]
                self.slider.setRange(0, max(0, len(self.frames) - 1))
                self.slider.setEnabled(bool(self.frames))
                self.slider.setValue(0)
                self.show_frame(0)
        except (OSError, ValueError, KeyError) as exc:
            self.details.setText("Aufnahme nicht vollständig verfügbar: " + str(exc))

    def show_frame(self, index):
        if not 0 <= index < len(self.frames):
            return
        frame = self.frames[index]
        try:
            data = np.load(inside(self.frame_directory, frame["file"]), allow_pickle=False)
            if data.dtype != np.uint8 or data.ndim not in (2, 3):
                raise ValueError("Vorschau unterstützt Mono8 und BGR8.")
            data = np.ascontiguousarray(data)
            if data.ndim == 2:
                fmt = QImage.Format.Format_Grayscale8
            elif data.shape[2] == 3:
                fmt = QImage.Format.Format_BGR888
            else:
                raise ValueError("Unbekannte Kanalzahl.")
            image = QImage(data.data, data.shape[1], data.shape[0], data.strides[0], fmt).copy()
            self.image_label.setPixmap(
                QPixmap.fromImage(image).scaled(
                    self.image_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.frame_label.setText(
                f"Bild {index + 1}/{len(self.frames)} · Frame-ID {frame.get('frame_id')} · "
                f"Rohwinkel {frame['measurement'].get('angle_deg')}°"
            )
            self.curve.marker = frame["timestamp"]
            self.curve.update()
        except (OSError, ValueError, KeyError) as exc:
            self.image_label.setText(str(exc))

    def annotate(self):
        row = self.selected()
        if row is None:
            return
        try:
            label = self.label.currentText()
            self.store.annotate(
                row["trial_id"],
                None if label == "Zu prüfen" else label,
                self.excluded.isChecked(),
                self.reason.text(),
            )
            self.refresh()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Datenprüfung", str(exc))

    def export(self):
        if self.store is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Versuchsdaten exportieren", "versuche.csv", "CSV (*.csv)"
        )
        if path:
            try:
                count = DatasetReader([self.store]).export_csv(path)
                self.details.setText(f"{count} Versuche exportiert; Ausschlussstatus enthalten.")
            except (OSError, ValueError, KeyError) as exc:
                QMessageBox.warning(self, "Export", str(exc))
