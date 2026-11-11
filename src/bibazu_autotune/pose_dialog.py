"""Staged STL/pose editor. Closing it never changes the active part selection."""

from __future__ import annotations

import copy
import math
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .analytical_model import ModelConfiguration, resolve_turn
from .orientation_animation import multiply, rotate
from .pose_preview import render_mesh_preview
from .preparation import GeometryWorker
from .trial_context import manual_context, normalize_quaternion, validate_context
from .workpiece import WorkpieceSelection


class PartPoseDialog(QDialog):
    def __init__(self, path, density, parent=None, *, selection=None, expected_hash=None):
        super().__init__(parent)
        self.setWindowTitle("Bauteil und Posen")
        self.resize(920, 660)
        self.path = Path(path).resolve()
        self.density, self.expected_hash = density, expected_hash
        self.context = copy.deepcopy(selection.context) if selection else None
        self.roadmap = bool(self.context and self.context.get("kind") != "manual")
        self.source_quaternion = normalize_quaternion(
            (self.context or {}).get("source_quaternion_xyzw", (0, 0, 0, 1))
        )
        self.legacy_target = (self.context or {}).get("target_quaternion_xyzw")
        self.analysis = None
        self.selection = None
        self.generation = 0
        self.close_result = None
        layout = QVBoxLayout(self)
        source_form = QFormLayout()
        self.path_label = QLabel(str(self.path))
        self.path_label.setWordWrap(True)
        source_form.addRow("STL-Datei", self.path_label)
        self.part_name = QLineEdit((self.context or {}).get("part_name", self.path.stem))
        self.part_name.setReadOnly(self.roadmap)
        source_form.addRow("Bauteilname", self.part_name)
        self.unit = QComboBox()
        self.unit.addItems(["mm", "cm", "m"])
        self.unit.setCurrentText(selection.unit if selection else "mm")
        source_form.addRow("STL-Koordinateneinheit", self.unit)
        layout.addLayout(source_form)
        previews = QHBoxLayout()
        self.source_preview = QLabel("STL wird ausgewertet …")
        self.target_preview = QLabel("STL wird ausgewertet …")
        for preview in (self.source_preview, self.target_preview):
            preview.setMinimumSize(380, 240)
            preview.setWordWrap(True)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            previews.addWidget(preview)
        layout.addLayout(previews)
        controls = QHBoxLayout()
        self.source_group = QGroupBox("1. Ausgangslage ausrichten")
        source_layout = QVBoxLayout(self.source_group)
        self.step = QDoubleSpinBox()
        self.step.setDecimals(2)
        self.step.setRange(0.01, 360)
        self.step.setValue(90)
        self.step.setSuffix(" °")
        step_form = QFormLayout()
        step_form.addRow("Drehschritt", self.step)
        source_layout.addLayout(step_form)
        self.rotation_buttons = {}
        for axis in "xyz":
            row = QHBoxLayout()
            for sign in (-1, 1):
                button = QPushButton(("−" if sign < 0 else "+") + axis.upper())
                button.clicked.connect(lambda _, a=axis, s=sign: self.rotate_source(a, s))
                row.addWidget(button)
                self.rotation_buttons[axis, sign] = button
            source_layout.addLayout(row)
        self.reset_button = QPushButton("STL-Lage zurücksetzen")
        self.reset_button.clicked.connect(self.reset_source)
        source_layout.addWidget(self.reset_button)
        controls.addWidget(self.source_group)
        self.target_group = QGroupBox("2. Kippziel festlegen")
        target_form = QFormLayout(self.target_group)
        self.axis = QComboBox()
        self.axis.addItems(["X", "Y", "Z"])
        self.axis.setCurrentText("Y")
        self.direction = QComboBox()
        self.direction.addItems(["Drehsinn wählen …", "Positiv (+)", "Negativ (−)"])
        self.direction.setCurrentIndex(1)
        self.angle = QDoubleSpinBox()
        self.angle.setDecimals(2)
        self.angle.setRange(0.01, 360)
        self.angle.setValue(90)
        self.angle.setSuffix(" °")
        target_form.addRow("Kippachse", self.axis)
        target_form.addRow("Drehsinn", self.direction)
        target_form.addRow("Winkel", self.angle)
        help_text = QLabel(
            "Alle Drehungen beziehen sich auf die dargestellten Rinnenachsen. "
            "Die Zielpose entsteht aus der Ausgangspose durch diese eine Drehung. "
            "Auflage an Y=0 und Z=0 wird automatisch dargestellt."
        )
        help_text.setWordWrap(True)
        target_form.addRow(help_text)
        controls.addWidget(self.target_group)
        layout.addLayout(controls)
        if self.roadmap:
            self.source_group.hide()
            self.target_group.hide()
            layout.addWidget(QLabel("Ausgangs- und Zielpose bleiben aus der Roadmap übernommen."))
        elif self.context:
            self.restore_turn()
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.accept_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.accept_button.setText("Bauteil übernehmen")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        self.buttons.accepted.connect(self.accept_selection)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.worker = GeometryWorker(self)
        self.worker.completed.connect(self.accept_analysis)
        self.worker.finished.connect(self.finish_closing)
        self.unit.currentTextChanged.connect(self.request_analysis)
        self.part_name.textChanged.connect(self.update_previews)
        for control in (self.axis, self.direction):
            control.currentIndexChanged.connect(self.update_previews)
        self.angle.valueChanged.connect(self.update_previews)
        cached = selection.analysis if selection else None
        if (
            cached is not None
            and cached.path.resolve() == self.path
            and cached.unit == self.unit.currentText()
            and cached.material.density_kg_m3 == density
        ):
            self.analysis = cached
            self.update_previews()
        else:
            self.request_analysis()

    def restore_turn(self):
        try:
            turn = resolve_turn(self.context, ModelConfiguration())
            self.axis.setCurrentText(turn.axis.upper())
            self.direction.setCurrentIndex(1 if turn.signed_angle_deg > 0 else 2)
            self.angle.setValue(abs(turn.signed_angle_deg))
        except ValueError:
            # Legacy 180° endpoints cannot encode the intended direction.
            target = normalize_quaternion(self.legacy_target)
            source = self.source_quaternion
            relative = normalize_quaternion(
                multiply(target, (-source[0], -source[1], -source[2], source[3]))
            )
            axes = [i for i, value in enumerate(relative[:3]) if abs(value) > 1e-6]
            if len(axes) == 1:
                self.axis.setCurrentIndex(axes[0])
                self.angle.setValue(
                    math.degrees(2 * math.atan2(abs(relative[axes[0]]), relative[3]))
                )
            self.direction.setCurrentIndex(0)

    def target_quaternion(self):
        if self.roadmap and self.legacy_target is None:
            return None
        if self.roadmap or self.direction.currentIndex() == 0 and self.legacy_target:
            return normalize_quaternion(self.legacy_target)
        sign = -1 if self.direction.currentIndex() == 2 else 1
        return normalize_quaternion(
            rotate(
                self.source_quaternion, self.axis.currentText().lower(), sign * self.angle.value()
            )
        )

    def rotate_source(self, axis, sign):
        self.source_quaternion = normalize_quaternion(
            rotate(self.source_quaternion, axis, sign * self.step.value())
        )
        self.update_previews()

    def reset_source(self):
        self.source_quaternion = (0, 0, 0, 1)
        self.update_previews()

    def request_analysis(self, *_):
        self.generation += 1
        self.analysis = None
        self.status.setText("STL wird im Hintergrund ausgewertet …")
        self.worker.commands.put(
            (self.generation, self.path, self.unit.currentText(), self.density)
        )
        if not self.worker.isRunning() and not self.worker.stopping.is_set():
            self.worker.start()
        self.update_previews()

    def accept_analysis(self, generation, analysis, error):
        if generation != self.generation or self.close_result is not None:
            return
        self.analysis = analysis
        self.status.setText(error)
        self.update_previews()

    def update_previews(self, *_):
        error = ""
        if self.analysis is not None:
            if self.roadmap and (
                self.legacy_target is None or "source_quaternion_xyzw" not in self.context
            ):
                error = (
                    "Roadmap erneut laden: Ausgangs- oder Zielorientierung fehlt im Parametersatz."
                )
            elif self.expected_hash and self.analysis.sha256 != self.expected_hash:
                error = (
                    "STL-Inhalt passt nicht zum geladenen Parametersatz. "
                    "Für ein neues Bauteil »Aus STL laden« verwenden."
                )
            elif not self.part_name.text().strip():
                error = "Bauteilname fehlt."
            elif not self.roadmap and self.direction.currentIndex() == 0:
                error = (
                    "Drehsinn ausdrücklich wählen; aus der bisherigen Zielpose "
                    "ist er nicht eindeutig."
                )
            for label, quaternion, caption in (
                (self.source_preview, self.source_quaternion, "Ausgangspose"),
                (self.target_preview, self.target_quaternion(), "Zielpose"),
            ):
                if quaternion is None:
                    label.clear()
                    label.setText(caption + ": Orientierung fehlt")
                else:
                    label.setPixmap(
                        render_mesh_preview(
                            self.path,
                            400,
                            245,
                            quaternion_xyzw=quaternion,
                            caption=caption,
                            triangles=self.analysis.preview_triangles,
                        )
                    )
            self.status.setText(
                error or self.analysis.warning or "Bauteil und Posen prüfen und übernehmen."
            )
        else:
            for label in (self.source_preview, self.target_preview):
                label.clear()
                label.setText("STL auswerten")
        self.accept_button.setEnabled(
            self.analysis is not None and not error and self.close_result is None
        )

    def accept_selection(self):
        if not self.accept_button.isEnabled():
            return
        if self.roadmap:
            context = validate_context(self.context)
        else:
            sign = -1 if self.direction.currentIndex() == 2 else 1
            context = manual_context(
                self.part_name.text(),
                self.analysis.sha256,
                self.source_quaternion,
                self.target_quaternion(),
                rotation_axis=self.axis.currentText().lower(),
                signed_angle_deg=sign * self.angle.value(),
            )
        self.selection = WorkpieceSelection(
            context, self.path, self.unit.currentText(), self.analysis
        )
        self.done(QDialog.DialogCode.Accepted)

    def done(self, result):
        self.close_result = result
        self.worker.stopping.set()
        if self.worker.isRunning():
            self.buttons.setEnabled(False)
        else:
            super().done(result)

    def finish_closing(self):
        if self.close_result is not None:
            super().done(self.close_result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        if self.worker.isRunning():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
