import base64
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from bibazu_autotune.roadmap_transition_dialog import (
    RoadmapTransitionDialog,
    default_roadmap_directory,
    load_roadmap_document,
    pose_pixmap,
    roadmap_transition_metadata,
    selection_from_roadmap_transition_metadata,
)


@pytest.fixture
def qt():
    return QApplication.instance() or QApplication([])


def roadmap_payload():
    path = Path(__file__).resolve().parents[1] / "examples/roadmap.json"
    return json.loads(path.read_text("utf-8"))


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_local_roadmap_browser_and_pose_images(tmp_path, qt, suffix):
    payload = roadmap_payload()
    image = QImage(16, 16, QImage.Format.Format_RGB32)
    image.fill(QColor("blue"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    for node in payload["nodes"]:
        node["thumbnail_png_base64"] = base64.b64encode(bytes(buffer.data())).decode("ascii")
    path = tmp_path / f"roadmap{suffix}"
    path.write_text(yaml.safe_dump(payload) if suffix == ".yaml" else json.dumps(payload), "utf-8")
    document = load_roadmap_document(path)
    assert document.part_name == "Dk1i"
    assert not pose_pixmap(document.pose(5), 120, 80).isNull()
    chooser = RoadmapTransitionDialog(document)
    chooser._set_pose_selection(5, 0)
    assert chooser.transition_table.rowCount() == 1
    chooser.transition_table.selectRow(0)
    chooser._accept_selected_transition()
    selection = chooser.selected_transition
    assert selection.source_pose.pose_id == 5
    assert selection.target_pose.pose_id == 0
    metadata = roadmap_transition_metadata(selection)
    assert metadata["edge_id"] == "example:5->0:free_y"
    restored = selection_from_roadmap_transition_metadata(metadata)
    assert restored.transition.edge_id == selection.transition.edge_id
    # A missing roadmap does not destroy the identity saved in a profile.
    path.unlink()
    assert (
        selection_from_roadmap_transition_metadata(metadata).transition.edge_id
        == metadata["edge_id"]
    )


def test_grouped_choice_retains_source_group_and_member_context(tmp_path, qt):
    payload = roadmap_payload()
    payload["member_states"] = [
        {"member_id": "a", "group_id": 5, "retained": True, "quaternion_xyzw": [0, 0, 0, 1]},
        {"member_id": "b", "group_id": 0, "retained": True, "quaternion_xyzw": [0, 0, 0, 1]},
    ]
    payload["consumer_export"] = {
        "version": 2,
        "group_keys": {"5": "source-group", "0": "target-group"},
        "execution_routes": [],
        "calibration_choices": [
            {
                "edge_id": "group-choice",
                "source": 5,
                "target": 0,
                "transition_kind": "group_transfer",
                "actuation": "free_y",
                "actuation_count": 1,
                "source_group_key": "source-group",
                "target_group_key": "target-group",
                "display_name": "Grouped choice",
                "source_member_ids": ["a"],
                "possible_target_member_ids": ["b"],
                "source_group_member_ids": ["a"],
                "commands": [],
            }
        ],
    }
    path = tmp_path / "grouped.json"
    path.write_text(json.dumps(payload), "utf-8")
    document = load_roadmap_document(path)
    chooser = RoadmapTransitionDialog(document)
    chooser._set_pose_selection(5, 0)
    assert chooser.transition_table.rowCount() == 1
    chooser.transition_table.selectRow(0)
    chooser.group_coverage_checkbox.setChecked(True)
    chooser._accept_selected_transition()
    metadata = roadmap_transition_metadata(chooser.selected_transition)
    assert metadata["source_group_key"] == "source-group"
    assert metadata["calibrated_source_group_key"] == "source-group"
    assert metadata["member_context"]["source_member_ids"] == ["a"]
    restored = selection_from_roadmap_transition_metadata(metadata)
    assert restored is not None
    assert restored.transition.member_context["source_member_ids"] == ["a"]


def test_multi_flip_context_preserves_each_component(tmp_path, qt):
    payload = roadmap_payload()
    payload["nodes"].append({"node_id": 1, "pose_ids": [1], "kind": "robust"})
    payload["edges"].append(
        {
            "edge_id": "step2",
            "source": 0,
            "target": 1,
            "transition_kind": "actuated",
            "actuation": "free_z",
            "signed_angle_deg": 45,
        }
    )
    payload["consumer_export"] = {
        "version": 1,
        "execution_routes": [],
        "calibration_choices": [
            {
                "edge_id": "multi",
                "source": 5,
                "target": 1,
                "transition_kind": "multi_reorientation",
                "actuation": "multiple_reorientation_2",
                "actuation_count": 2,
                "via_pose_ids": [0],
                "component_edge_ids": ["example:5->0:free_y", "step2"],
                "commands": [],
            }
        ],
    }
    path = tmp_path / "multi.json"
    path.write_text(json.dumps(payload), "utf-8")
    chooser = RoadmapTransitionDialog(load_roadmap_document(path))
    chooser._set_pose_selection(5, 1)
    chooser.transition_table.selectRow(0)
    chooser._accept_selected_transition()
    metadata = roadmap_transition_metadata(chooser.selected_transition)
    assert [item["signed_angle_deg"] for item in metadata["component_flips"]] == [-90, 45]
    restored = selection_from_roadmap_transition_metadata(metadata)
    assert len(restored.component_flips) == 2
    assert restored.transition == replace(chooser.selected_transition.transition)


def test_roadmap_location_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("BIBAZU_ROADMAPS_DIR", str(tmp_path))
    assert default_roadmap_directory() == tmp_path
