import math

import numpy as np
import pytest
import trimesh
from PyQt6.QtWidgets import QDialog, QLineEdit
from test_geometry import write_box
from test_gui import offline_window as offline_window

from bibazu_autotune.analytical_model import ModelConfiguration, resolve_turn
from bibazu_autotune.geometry import analyze_stl
from bibazu_autotune.pose_dialog import PartPoseDialog
from bibazu_autotune.trial_context import manual_context, manual_identity, validate_context
from bibazu_autotune.workpiece import WorkpieceSelection


def matrix(q):
    return trimesh.transformations.quaternion_matrix([q[3], *q[:3]])[:3, :3]


@pytest.fixture
def dialogs(offline_window, tmp_path):
    window, _, _, wait = offline_window
    path = write_box(tmp_path / "part.stl")
    opened = []

    def create(*, selection=None, source=None, expected_hash=None):
        dialog = PartPoseDialog(
            source or path, 1200, window, selection=selection, expected_hash=expected_hash
        )
        opened.append(dialog)
        dialog.show()
        wait(lambda: dialog.analysis is not None or "Hintergrund" not in dialog.status.text())
        return dialog

    yield create, wait, path
    for dialog in opened:
        dialog.reject()
        wait(lambda d=dialog: not d.worker.isRunning())
        dialog.deleteLater()


def test_rotation_buttons_and_relative_target_use_chute_axes(dialogs):
    create, wait, _ = dialogs
    dialog = create()
    assert dialog.unit.currentText() == "mm"
    assert dialog.part_name.text() == "part"
    assert dialog.step.value() == 90 and dialog.axis.currentText() == "Y"
    assert len(dialog.findChildren(QLineEdit)) >= 1  # Only native numeric editors and part name.
    assert not any(
        "quaternion" in field.placeholderText().lower() for field in dialog.findChildren(QLineEdit)
    )
    dialog.rotation_buttons["x", 1].click()
    rx = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]])
    assert matrix(dialog.source_quaternion) == pytest.approx(rx, abs=1e-12)
    dialog.rotation_buttons["y", 1].click()
    ry = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])
    assert matrix(dialog.source_quaternion) == pytest.approx(ry @ rx, abs=1e-12)
    dialog.axis.setCurrentText("Z")
    dialog.direction.setCurrentIndex(2)
    rz_negative = np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1]])
    assert matrix(dialog.target_quaternion()) == pytest.approx(rz_negative @ ry @ rx, abs=1e-12)
    assert not dialog.source_preview.pixmap().isNull()
    assert not dialog.target_preview.pixmap().isNull()
    dialog.accept_button.click()
    wait(lambda: not dialog.worker.isRunning() and not dialog.isVisible())
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selection.context["rotation_axis"] == "z"
    assert dialog.selection.context["signed_angle_deg"] == -90


def test_reset_fine_step_unit_and_cancel_are_staged(dialogs, offline_window):
    create, wait, _ = dialogs
    window, services, errors, _ = offline_window
    before_generation = window.autotune_settings.model_generation
    dialog = create()
    dialog.step.setValue(30)
    dialog.rotate_source("x", 1)
    assert matrix(dialog.source_quaternion)[1, 1] == pytest.approx(math.cos(math.pi / 6))
    dialog.reset_button.click()
    assert dialog.source_quaternion == (0, 0, 0, 1)
    dialog.unit.setCurrentText("cm")
    wait(lambda: dialog.analysis is not None and dialog.analysis.unit == "cm")
    assert dialog.analysis.geometry.dimensions_m[0] == pytest.approx(0.2)
    dialog.reject()
    wait(lambda: not dialog.worker.isRunning())
    assert dialog.selection is None
    assert window.context is None
    assert window.autotune_settings.model_generation == before_generation
    assert not services.calls and not errors


def test_legacy_180_requires_direction_and_records_it(dialogs):
    create, wait, path = dialogs
    analysis = analyze_stl(path, density=1200)
    legacy = manual_context("Old", analysis.sha256, [0, 0, 0, 1], [0, 1, 0, 0])
    dialog = create(selection=WorkpieceSelection(legacy, path, "mm", analysis))
    assert dialog.axis.currentText() == "Y" and dialog.angle.value() == 180
    assert dialog.direction.currentIndex() == 0
    assert not dialog.accept_button.isEnabled()
    dialog.direction.setCurrentIndex(2)
    dialog.accept_button.click()
    wait(lambda: not dialog.worker.isRunning())
    context = dialog.selection.context
    assert resolve_turn(context, ModelConfiguration()).signed_angle_deg == -180
    assert validate_context(context) == context


def test_missing_file_or_wrong_hash_cannot_commit(dialogs):
    create, _, path = dialogs
    missing = create(source=path.with_name("missing.stl"))
    assert missing.analysis is None and not missing.accept_button.isEnabled()
    wrong = create(expected_hash="a" * 64)
    assert "STL-Inhalt" in wrong.status.text() and not wrong.accept_button.isEnabled()


def test_explicit_commands_keep_same_endpoint_directions_distinct():
    positive = manual_context(
        "Part", "a" * 64, [0, 0, 0, 1], [0, 1, 0, 0], rotation_axis="y", signed_angle_deg=180
    )
    negative = manual_context(
        "Part", "a" * 64, [0, 0, 0, 1], [0, 1, 0, 0], rotation_axis="y", signed_angle_deg=-180
    )
    assert positive["target_quaternion_xyzw"] == negative["target_quaternion_xyzw"]
    assert manual_identity(positive) != manual_identity(negative)
    assert (
        resolve_turn(
            negative, ModelConfiguration(manual_axis="y", manual_angle_deg=180)
        ).signed_angle_deg
        == -180
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"rotation_axis": "y"},
        {"signed_angle_deg": 90},
        {"rotation_axis": None, "signed_angle_deg": None},
        {"rotation_axis": "q", "signed_angle_deg": 90},
        {"rotation_axis": "y", "signed_angle_deg": 0},
        {"rotation_axis": "y", "signed_angle_deg": float("nan")},
        {"rotation_axis": "x", "signed_angle_deg": 90},
    ],
)
def test_invalid_or_inconsistent_manual_commands_are_rejected(extra):
    context = manual_context("Part", "a" * 64, [0, 0, 0, 1], [0, 1, 0, 1])
    with pytest.raises(ValueError):
        validate_context({**context, **extra})
