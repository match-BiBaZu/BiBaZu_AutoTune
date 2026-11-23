"""Recorded angle plots and a small, usable dataset review surface."""

from __future__ import annotations

import csv

import numpy as np
from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
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
from .models import AUTOMATIC_REVIEW_REASON, LABELS, current_review_reason
from .storage import CampaignStore, inside, read_json
from .video import VideoFrames


def quality_explanation(row):
    reasons = {
        "capture_incomplete": "Aufnahme unvollständig",
        "dropped_frames": "Bilder im Aufnahmepuffer verworfen",
        "observability_not_confirmed": "Sichtbarkeit der Drehung nicht bestätigt",
        "no_valid_angles": "Keine gültigen Winkel erkannt",
        "no_stable_initial_pose": "Keine stabile Ausgangspose erkannt",
        "no_stable_final_pose": "Keine stabile Endpose erkannt",
        "interrupted_rotation": "Winkelverfolgung unterbrochen (Mess- oder Bildlücke)",
        "insufficient_observation": "Zu kurze Beobachtung zwischen Ausgangs- und Endpose",
    }
    messages = [reasons.get(code, code) for code in row["quality_reasons"].split(";") if code]
    warnings = {
        "rest_gap_bridged": "Kurze Messlücke in bestätigter Ruhephase toleriert",
        "unusable_prefix_ignored": "Unbrauchbaren Vorlauf außerhalb der Auswertung belassen",
        "unusable_suffix_ignored": "Unbrauchbaren Nachlauf außerhalb der Auswertung belassen",
        "memory_limit_after_stable_final_pose": (
            "Speicherabbruch nach beobachteter Bewegung und stabiler Endpose toleriert"
        ),
    }
    messages.extend(
        "Hinweis: " + warnings.get(code, code)
        for code in row.get("quality_warnings", "").split(";")
        if code
    )
    stop = row.get("stop_reason")
    if stop and stop != "part_left":
        messages.append(
            "Aufnahmeende: "
            + {
                "memory_limit": "Aufnahmepuffer voll",
                "max_duration": "Maximale Aufnahmedauer erreicht",
                "write_error": "Schreibfehler",
                "no_motion_timeout": "Zeitlimit ohne Bewegung erreicht",
            }.get(stop, stop)
        )
    return "; ".join(messages)


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
    storage_changed = pyqtSignal(str)
    review_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.store = None
        self.records = []
        self.frames = []
        self.frame_directory = None
        self.video_frames = None
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
        self.confirm_automatic_button = QPushButton("Alle automatisch gültigen bestätigen")
        self.confirm_automatic_button.setToolTip(
            "Alle noch ungeprüften, automatisch gültigen Versuche dieser Kampagne bestätigen. "
            "Vorhandene manuelle Entscheidungen bleiben erhalten; Tabellenfilter gelten hier nicht."
        )
        self.confirm_automatic_button.setEnabled(False)
        self.confirm_automatic_button.clicked.connect(self.confirm_automatic)
        root.addWidget(self.confirm_automatic_button)
        media_actions = QHBoxLayout()
        self.delete_video_button = QPushButton("Prüfvideo löschen")
        self.delete_all_videos_button = QPushButton("Geprüfte Videos dieser Kampagne löschen")
        self.delete_video_button.clicked.connect(lambda: self.delete_videos(False))
        self.delete_all_videos_button.clicked.connect(lambda: self.delete_videos(True))
        media_actions.addWidget(self.delete_video_button)
        media_actions.addWidget(self.delete_all_videos_button)
        root.addLayout(media_actions)
        self.path_label = QLabel("Noch keine Kampagne geöffnet.")
        self.path_label.setWordWrap(True)
        root.addWidget(self.path_label)
        self.table = QTableWidget(0, 9)
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
                "Prüfung",
            ]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().moveSection(8, 7)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
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
            selected = self.selected()
            selected_id = selected["trial_id"] if selected else None
            rows = list(DatasetReader([self.store]).rows(include_excluded=True))
            self.confirm_automatic_button.setEnabled(
                any(self.automatic_candidate(row) for row in rows)
            )
            wanted = self.filter.currentText()
            self.records = [
                row
                for row in rows
                if wanted == "Alle Versuche"
                or (
                    wanted == "Zu prüfen"
                    and not row["excluded"]
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
                checked_label = None
                if row["excluded"]:
                    quality = "Ungültig"
                elif row["reviewed"] and row["label"] in LABELS:
                    quality = "Gültig"
                    checked_label = row["label"]
                elif self.automatic_candidate(row):
                    quality = "Automatisch gültig"
                    checked_label = row["automatic_label"]
                else:
                    quality = "Zu prüfen"
                values = [
                    row["trial_id"][:10],
                    f"{row['point_index'] + 1} / {row['repetition']}",
                    row["input_pressure_mbar"],
                    row["input_pulse_duration_ms"],
                    row["label"] or "Zu prüfen",
                    quality,
                    "Ja" if row["excluded"] else "Nein",
                    row["review_reason"] or quality_explanation(row),
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    self.table.setItem(index, column, item)
                actions = QWidget()
                buttons = QGridLayout(actions)
                buttons.setContentsMargins(2, 2, 2, 2)
                buttons.setSpacing(2)
                for label, title, button_row, button_column in (
                    ("not_flipped", "Not Flipped", 0, 0),
                    ("flipped", "Flipped", 0, 1),
                    ("overflipped", "Overflipped", 1, 0),
                    (None, "Ungültig", 1, 1),
                ):
                    action = QPushButton(title)
                    action.setToolTip(
                        f"Als {title} bestätigen und fürs Training freigeben."
                        if label is not None
                        else "Fehlerhafte Daten vom Training ausschließen."
                    )
                    action.setCheckable(True)
                    action.setChecked(row["excluded"] if label is None else label == checked_label)
                    color = "#fecaca" if label is None else "#bbf7d0"
                    action.setStyleSheet(f"QPushButton:checked {{ background: {color}; }}")
                    action.clicked.connect(
                        lambda checked=False, trial_id=row["trial_id"], label=label: (
                            self.quick_review(trial_id, label)
                        )
                    )
                    buttons.addWidget(action, button_row, button_column)
                self.table.setCellWidget(index, 8, actions)
            self.table.blockSignals(False)
            if self.records:
                index = next(
                    (i for i, row in enumerate(self.records) if row["trial_id"] == selected_id), 0
                )
                self.table.selectRow(index)
            self.select()
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Datenprüfung", str(exc))

    def selected(self):
        index = self.table.currentRow()
        return self.records[index] if 0 <= index < len(self.records) else None

    def select(self):
        if self.store is None:
            self.confirm_automatic_button.setEnabled(False)
        if self.video_frames is not None:
            self.video_frames.close()
        self.video_frames = None
        self.frames = []
        self.slider.setEnabled(False)
        self.image_label.clear()
        self.curve.set_rows([])
        self.details.clear()
        self.frame_label.clear()
        self.delete_video_button.setEnabled(False)
        candidates = self.store.reviewed_videos() if self.store else []
        self.delete_all_videos_button.setEnabled(bool(candidates))
        row = self.selected()
        self.save.setEnabled(row is not None)
        if row is None:
            return
        self.label.setCurrentText(row["label"] or "Zu prüfen")
        self.excluded.setChecked(row["excluded"])
        self.reason.setText(row["review_reason"])
        directory = self.store.trial_directory(row["trial_id"])
        self.delete_video_button.setEnabled(
            any(v["trial_id"] == row["trial_id"] for v in candidates)
        )
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
                + (record.get("error") or quality_explanation(row))
            )
            if record.get("clip"):
                self.frame_directory = inside(directory, record["clip"])
                manifest = read_json(self.frame_directory / "manifest.json")
                self.frames = manifest["frames"]
                if manifest.get("schema_version") == 2:
                    self.video_frames = VideoFrames(self.frame_directory, manifest)
                    if not len(self.video_frames):
                        self.image_label.setText(
                            "Prüfvideo gelöscht"
                            if manifest["media"]["state"] in {"deleted", "deleting"}
                            else "Prüfvideo nicht verfügbar: "
                            + (manifest["media"].get("error") or "")
                        )
                        return
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
            data = (
                self.video_frames[index]
                if self.video_frames is not None
                else np.load(inside(self.frame_directory, frame["file"]), allow_pickle=False)
            )
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

    def quick_review(self, trial_id, label):
        index = next((i for i, row in enumerate(self.records) if row["trial_id"] == trial_id), None)
        if index is None:
            return
        self.table.selectRow(index)
        row = self.records[index]
        excluded = label is None
        if excluded:
            label = row["label"]
        reason = current_review_reason(row["review_reason"], excluded, quick=True)
        try:
            self.store.annotate(trial_id, label, excluded, reason)
            self.refresh()
            self.review_changed.emit(str(self.store.directory))
        except (OSError, ValueError) as exc:
            self.refresh()
            QMessageBox.warning(self, "Datenprüfung", str(exc))

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
            self.review_changed.emit(str(self.store.directory))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Datenprüfung", str(exc))

    @staticmethod
    def automatic_candidate(row):
        return (
            row["quality"] == "valid"
            and row["automatic_label"] in LABELS
            and not row["reviewed"]
            and not row["excluded"]
        )

    def confirm_automatic(self):
        if self.store is None:
            return
        confirmed, errors = 0, []
        try:
            rows = list(DatasetReader([self.store]).rows(include_excluded=True))
            for row in rows:
                if not self.automatic_candidate(row):
                    continue
                try:
                    if self.store.annotation(row["trial_id"]) is not None:
                        continue
                    self.store.annotate(
                        row["trial_id"],
                        row["automatic_label"],
                        False,
                        AUTOMATIC_REVIEW_REASON,
                    )
                    confirmed += 1
                except (OSError, ValueError) as exc:
                    errors.append(f"{row['trial_id'][:10]}: {exc}")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(str(exc))
        self.refresh()
        if confirmed:
            self.review_changed.emit(str(self.store.directory))
            self.details.setText(f"{confirmed} automatisch gültige Versuche bestätigt.")
        if errors:
            QMessageBox.warning(self, "Bestätigung nicht vollständig", "\n".join(errors))

    def delete_videos(self, all_reviewed):
        if self.store is None:
            return
        row = self.selected()
        candidates = self.store.reviewed_videos()
        if not all_reviewed:
            candidates = [v for v in candidates if row and v["trial_id"] == row["trial_id"]]
        if not candidates:
            return
        size = sum(v["bytes"] for v in candidates)
        answer = QMessageBox.question(
            self,
            "Geprüfte Videos löschen",
            f"{len(candidates)} geprüfte Videos ({size / 1024**2:.2f} MiB) löschen?\n"
            f"Kampagne: {self.store.directory}\n"
            "Messwerte, Klassen, Prüfentscheidungen und alte Originalbilder bleiben erhalten.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.video_frames is not None:
            self.video_frames.close()
        errors = []
        for video in candidates:
            try:
                self.store.delete_review_video(video["trial_id"])
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        self.refresh()
        self.storage_changed.emit(str(self.store.directory))
        if errors:
            QMessageBox.warning(self, "Videolöschung nicht vollständig", "\n".join(errors))

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
