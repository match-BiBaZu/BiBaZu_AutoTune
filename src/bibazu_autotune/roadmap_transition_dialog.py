"""Local roadmap selection and previews, ported from BiBaZu Pressure Control (MIT)."""

from __future__ import annotations

import base64
import json
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .export_records import (
    group_evidence_details,
    read_export,
    relocated_path,
    source_coverage,
    validate_commands,
    validate_transfer,
    with_consumer_sidecar,
)
from .orientation_animation import command_frame, preview_frame
from .pose_preview import render_mesh_preview


@dataclass(frozen=True, slots=True)
class RoadmapPose:
    pose_id: int
    equivalent_pose_ids: tuple[int, ...]
    stability: str
    floor_contact: str
    wall_contact: str
    thumbnail_png: bytes | None
    quaternion_xyzw: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    mesh_path: Path | None = None


@dataclass(frozen=True, slots=True)
class RoadmapTransition:
    edge_id: str
    source_pose_id: int
    target_pose_id: int
    transition_kind: str
    actuation: str
    signed_angle_deg: float | None
    capture_width_deg: float | None
    geometric_score: float | None
    flip_count: int = 1
    via_pose_ids: tuple[int, ...] = ()
    component_edge_ids: tuple[str, ...] = ()
    commands: tuple[dict, ...] = ()
    evidence_status: str = ""
    member_context: dict | None = None
    covered_source_member_ids: tuple[str, ...] = ()
    preview_segments: tuple[dict, ...] = ()
    display_label: str = ""
    preference_cost: tuple[float, ...] = ()
    source_group_key: str = ""
    calibrated_source_group_key: str = ""

    @property
    def display_name(self) -> str:
        if self.source_group_key:
            return self.display_label
        return f"Transition {self.source_pose_id}-{self.target_pose_id}"

    @property
    def calibratable(self) -> bool:
        return self.transition_kind in {
            "actuated",
            "multi_reorientation",
            "member_transfer",
            "group_transfer",
            "group_route",
        }

    @property
    def is_multi_reorientation(self) -> bool:
        return self.transition_kind in {
            "multi_reorientation",
            "member_transfer",
            "group_route",
        } or (self.transition_kind == "group_transfer" and self.flip_count > 1)

    @property
    def category_label(self) -> str:
        if self.source_group_key:
            return "Single-axis rotation" if self.flip_count == 1 else "Multiple rotations"
        if self.evidence_status == "geometry_only":
            return "Composite candidate · geometry only · calibration required"
        if self.is_multi_reorientation:
            return f"Multiple reorientation ({self.flip_count}×) · experimental"
        if self.transition_kind == "actuated":
            return "Direct · preferred"
        return "Passive · information only"


@dataclass(frozen=True, slots=True)
class RoadmapDocument:
    path: Path
    part_name: str
    mesh_path: Path | None
    cad_status: str
    poses: tuple[RoadmapPose, ...]
    transitions: tuple[RoadmapTransition, ...]
    member_states: tuple[dict, ...] = ()
    member_route_data: tuple[dict, ...] = ()
    consumer_export_version: int = 1

    @property
    def overview_transitions(self):
        if self.consumer_export_version == 2:
            pairs = {}
            for edge in sorted(self.transitions, key=lambda e: (e.preference_cost, e.edge_id)):
                if edge.transition_kind == "group_transfer":
                    pairs.setdefault((edge.source_pose_id, edge.target_pose_id), edge)
            return tuple(pairs.values())
        if not self.member_states:
            return self.transitions
        by_pair = {}
        for edge in sorted(self.transitions, key=lambda e: e.edge_id):
            if (
                edge.transition_kind == "member_transfer"
                and edge.source_pose_id != edge.target_pose_id
            ):
                by_pair.setdefault((edge.source_pose_id, edge.target_pose_id), edge)
        return tuple(by_pair.values())

    def pose(self, pose_id: int) -> RoadmapPose:
        for pose in self.poses:
            if pose.pose_id == pose_id:
                return pose
        raise KeyError(pose_id)

    def route_options(self, start, target):
        choices = tuple(
            e
            for e in self.transitions
            if e.calibratable
            and e.source_pose_id == start
            and e.target_pose_id == target
            and (self.consumer_export_version != 2 or e.source_group_key)
        )
        return (
            tuple(sorted(choices, key=lambda e: (e.preference_cost, e.edge_id)))
            if self.consumer_export_version == 2
            else choices
        )


@dataclass(frozen=True, slots=True)
class SelectedRoadmapTransition:
    roadmap_path: Path
    part_name: str
    transition: RoadmapTransition
    source_pose: RoadmapPose
    target_pose: RoadmapPose
    component_flips: tuple[RoadmapTransition, ...] = ()

    @property
    def profile_name_stem(self) -> str:
        raw = (
            f"{self.part_name}_Transition_"
            f"{self.transition.source_pose_id}-{self.transition.target_pose_id}_"
            f"{self.transition.actuation}"
        )
        if self.transition.evidence_status:
            raw += "_" + self.transition.edge_id.rsplit(":", 1)[-1]
        return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")


def default_roadmap_directory() -> Path:
    override = os.environ.get("BIBAZU_ROADMAPS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Documents" / "BiBaZu_Roadmaps"


def roadmap_transition_metadata(selection: SelectedRoadmapTransition) -> dict[str, Any]:
    """Return the portable transition identity stored in a pressure profile."""
    transition = selection.transition
    return {
        "title": transition.display_name,
        "roadmap_path": str(selection.roadmap_path),
        "part_name": selection.part_name,
        "edge_id": transition.edge_id,
        "source_pose_id": transition.source_pose_id,
        "target_pose_id": transition.target_pose_id,
        "transition_kind": transition.transition_kind,
        "actuation": transition.actuation,
        "signed_angle_deg": transition.signed_angle_deg,
        "flip_count": transition.flip_count,
        "via_pose_ids": list(transition.via_pose_ids),
        "component_edge_ids": list(transition.component_edge_ids),
        "display_label": transition.display_label,
        "preference_cost": list(transition.preference_cost),
        "source_group_key": transition.source_group_key,
        "calibrated_source_group_key": transition.calibrated_source_group_key,
        "commands": list(transition.commands),
        "preview_segments": list(transition.preview_segments),
        "evidence_status": transition.evidence_status,
        "member_context": transition.member_context,
        "covered_source_member_ids": list(transition.covered_source_member_ids),
        "source_quaternion_xyzw": list(selection.source_pose.quaternion_xyzw),
        "target_quaternion_xyzw": list(selection.target_pose.quaternion_xyzw),
        "component_flips": [
            {
                "edge_id": flip.edge_id,
                "source_pose_id": flip.source_pose_id,
                "target_pose_id": flip.target_pose_id,
                "actuation": flip.actuation,
                "signed_angle_deg": flip.signed_angle_deg,
            }
            for flip in selection.component_flips
        ],
    }


def selection_from_roadmap_transition_metadata(
    value: Any,
) -> SelectedRoadmapTransition | None:
    """Restore a selected transition without making old profiles invalid.

    Keep the saved transition identity even if regeneration removed its edge.
    Use the referenced roadmap for pose images whenever it remains available.
    """
    if not isinstance(value, dict):
        return None
    try:
        roadmap_path = relocated_path(value["roadmap_path"], default_roadmap_directory())
        edge_id = str(value["edge_id"])
        source_id = int(value["source_pose_id"])
        target_id = int(value["target_pose_id"])
        transition = RoadmapTransition(
            edge_id=edge_id,
            source_pose_id=source_id,
            target_pose_id=target_id,
            transition_kind=str(value.get("transition_kind", "actuated")),
            actuation=str(value["actuation"]),
            signed_angle_deg=(
                None if value.get("signed_angle_deg") is None else float(value["signed_angle_deg"])
            ),
            capture_width_deg=None,
            geometric_score=None,
            flip_count=int(value.get("flip_count", 1)),
            via_pose_ids=tuple(int(item) for item in value.get("via_pose_ids", [])),
            component_edge_ids=tuple(str(item) for item in value.get("component_edge_ids", [])),
            commands=validate_commands(value.get("commands", [])),
            display_label=str(value.get("display_label", "")),
            preference_cost=tuple(value.get("preference_cost", [])),
            source_group_key=str(value.get("source_group_key", "")),
            calibrated_source_group_key=str(value.get("calibrated_source_group_key", "")),
            preview_segments=tuple(value.get("preview_segments", [])),
            evidence_status=str(value.get("evidence_status", "")),
            member_context=value.get("member_context"),
            covered_source_member_ids=tuple(value.get("covered_source_member_ids", [])),
        )
        if (
            transition.calibrated_source_group_key
            and transition.calibrated_source_group_key != transition.source_group_key
        ):
            raise ValueError("Saved group calibration has a different source group")
        if transition.member_context is not None:
            context = transition.member_context
            if not isinstance(context, dict) or not all(
                isinstance(context.get(k), list)
                and context[k]
                and all(isinstance(m, str) for m in context[k])
                for k in ("source_member_ids", "possible_target_member_ids")
            ):
                raise ValueError("Invalid saved member context")
            source_coverage(
                transition.covered_source_member_ids,
                context.get("source_group_member_ids", context["source_member_ids"]),
            )
        source_pose = RoadmapPose(source_id, (source_id,), "robust", "unknown", "unknown", None)
        target_pose = RoadmapPose(target_id, (target_id,), "robust", "unknown", "unknown", None)
        document = None
        try:
            document = load_roadmap_document(roadmap_path)
            source_pose = document.pose(source_id)
            target_pose = document.pose(target_id)
        except (KeyError, OSError, TypeError, ValueError):
            pass
        if transition.member_context:
            source_pose = replace(
                source_pose,
                quaternion_xyzw=_quaternion(value.get("source_quaternion_xyzw")),
                thumbnail_png=None,
            )
            target_pose = replace(
                target_pose,
                quaternion_xyzw=_quaternion(value.get("target_quaternion_xyzw")),
                thumbnail_png=None,
            )
        return SelectedRoadmapTransition(
            roadmap_path=roadmap_path,
            part_name=str(value.get("part_name", roadmap_path.stem)),
            transition=transition,
            source_pose=source_pose,
            target_pose=target_pose,
            component_flips=_restore_component_flips(value, transition, document),
        )
    except (KeyError, OSError, TypeError, ValueError, StopIteration):
        return None


def _restore_component_flips(
    metadata: dict[str, Any],
    transition: RoadmapTransition,
    document: RoadmapDocument | None,
) -> tuple[RoadmapTransition, ...]:
    """Prefer saved flip angles; recover older profiles from their exact edges."""
    by_id = {edge.edge_id: edge for edge in document.transitions} if document else {}
    saved_flips = metadata.get("component_flips", [])
    if isinstance(saved_flips, list):
        for value in saved_flips:
            try:
                flip = RoadmapTransition(
                    edge_id=str(value["edge_id"]),
                    source_pose_id=int(value["source_pose_id"]),
                    target_pose_id=int(value["target_pose_id"]),
                    transition_kind="actuated",
                    actuation=str(value["actuation"]),
                    signed_angle_deg=(
                        None
                        if value.get("signed_angle_deg") is None
                        else float(value["signed_angle_deg"])
                    ),
                    capture_width_deg=None,
                    geometric_score=None,
                )
                by_id[flip.edge_id] = flip
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    pose_ids = (
        transition.source_pose_id,
        *transition.via_pose_ids,
        transition.target_pose_id,
    )
    flips = []
    for index, edge_id in enumerate(transition.component_edge_ids):
        flip = by_id.get(edge_id)
        if (
            flip is not None
            and index + 1 < len(pose_ids)
            and flip.source_pose_id == pose_ids[index]
            and flip.target_pose_id == pose_ids[index + 1]
        ):
            flips.append(flip)
    return tuple(flips)


def selection_from_pressure_profile(
    profile: dict[str, Any], profile_path: Path
) -> SelectedRoadmapTransition | None:
    """Restore metadata, or recover the pose pair from a legacy profile name."""
    metadata = profile.get("roadmap_transition")
    if isinstance(metadata, dict):
        return selection_from_roadmap_transition_metadata(metadata)
    match = re.fullmatch(
        r"(.+)_(?:Uebergang|Transition)_(\d+)-(\d+)_"
        r"(floor_main_(?:pos|neg)_x|wall_main_(?:pos|neg)_x|free_[yz]|"
        r"multiple_reorientation_[23])(?:_.*)?",
        profile_path.stem,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    part_name, source_id, target_id, actuation = match.groups()
    actuation = actuation.lower()
    roots = (
        profile_path.parent,
        default_roadmap_directory(),
    )
    roadmap_path = profile_path.parent / f"{part_name}_roadmap.json"
    for root in roots:
        candidates = sorted(root.rglob(f"{part_name}_roadmap.json")) if root.is_dir() else []
        if candidates:
            roadmap_path = candidates[0]
            break
    multi = actuation.startswith("multiple_reorientation_")
    return selection_from_roadmap_transition_metadata(
        {
            "roadmap_path": str(roadmap_path),
            "part_name": part_name,
            "edge_id": f"legacy:{profile_path.stem}",
            "source_pose_id": int(source_id),
            "target_pose_id": int(target_id),
            "transition_kind": "multi_reorientation" if multi else "actuated",
            "actuation": actuation,
            "flip_count": int(actuation.rsplit("_", 1)[1]) if multi else 1,
        }
    )


def _decode_thumbnail(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None


def _mesh_path(roadmap_path: Path, value: Any) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = roadmap_path.parent / path
    return path.resolve()


def _quaternion(value: Any) -> tuple[float, float, float, float]:
    if value is None:
        return (0.0, 0.0, 0.0, 1.0)
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Roadmap pose quaternion must contain four values")
    quaternion = tuple(float(component) for component in value)
    norm = math.sqrt(sum(component * component for component in quaternion))
    if not math.isfinite(norm) or norm < 1e-12:
        raise ValueError("Roadmap pose quaternion must be finite and non-zero")
    return tuple(component / norm for component in quaternion)  # type: ignore[return-value]


def _optional_float(value: Any, field: str) -> float | None:
    """Uncomputed roadmap metrics are null, rather than measured zeroes."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Roadmap {field} must be a finite number or null") from error
    if isinstance(value, bool) or not math.isfinite(result):
        raise ValueError(f"Roadmap {field} must be a finite number or null")
    return result


def load_roadmap_document(path: str | Path) -> RoadmapDocument:
    source = relocated_path(path, default_roadmap_directory())
    raw_text = source.read_text(encoding="utf-8")
    payload = with_consumer_sidecar(
        yaml.load(raw_text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
        if source.suffix.lower() in {".yaml", ".yml"}
        else json.loads(raw_text),
        source,
    )
    if payload.get("format") == "bibazu_pose_roadmap_handover":
        part = payload["part"]
        payload = dict(
            payload,
            source=part.get("mesh_source"),
            part_name=part.get("name"),
            geometry_status=part.get("cad_status", "unknown"),
            nodes=[
                dict(
                    n,
                    node_id=n["id"],
                    pose_ids=n.get("equivalent_catalog_pose_ids", [n["id"]]),
                    kind=n["stability"],
                    representative_quaternion_xyzw=n["orientation_quaternion_xyzw"],
                    floor_contact_topology=n.get("contacts", {}).get("floor", "unknown"),
                    wall_contact_topology=n.get("contacts", {}).get("wall", "unknown"),
                )
                for n in payload["poses"]
            ],
            edges=[
                dict(
                    e,
                    edge_id=e["id"],
                    source=e["from_pose"],
                    target=e["to_pose"],
                    transition_kind=e["type"],
                    actuation=e.get("action", {}).get("name", ""),
                    signed_angle_deg=e.get("action", {}).get("commanded_angle_deg"),
                    capture_width_deg=e.get("geometry", {}).get("capture_width_deg"),
                    geometric_score=e.get("geometry", {}).get("geometric_score"),
                )
                for e in payload["transitions"]
            ],
        )
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Unsupported roadmap schema version")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise TypeError("Roadmap JSON must contain node and edge lists")

    mesh_path = _mesh_path(source, payload.get("source"))

    poses: list[RoadmapPose] = []
    for node in raw_nodes:
        if not isinstance(node, dict):
            raise TypeError("Every roadmap node must be an object")
        stability = str(node.get("kind", ""))
        if stability not in {"robust", "metastable"}:
            raise ValueError("Roadmap node kind must be robust or metastable")
        poses.append(
            RoadmapPose(
                pose_id=int(node["node_id"]),
                equivalent_pose_ids=tuple(int(value) for value in node["pose_ids"]),
                stability=stability,
                floor_contact=str(node.get("floor_contact_topology", "unknown")),
                wall_contact=str(node.get("wall_contact_topology", "unknown")),
                thumbnail_png=_decode_thumbnail(node.get("thumbnail_png_base64")),
                quaternion_xyzw=_quaternion(node.get("representative_quaternion_xyzw")),
                mesh_path=mesh_path,
            )
        )

    pose_ids = {pose.pose_id for pose in poses}
    transitions: list[RoadmapTransition] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            raise TypeError("Every roadmap edge must be an object")
        source_id = int(edge["source"])
        target_id = int(edge["target"])
        if source_id not in pose_ids or target_id not in pose_ids:
            raise ValueError("Roadmap edge references an unknown pose")
        transitions.append(
            RoadmapTransition(
                edge_id=str(edge["edge_id"]),
                source_pose_id=source_id,
                target_pose_id=target_id,
                transition_kind=str(edge["transition_kind"]),
                actuation=str(edge["actuation"]),
                signed_angle_deg=_optional_float(edge.get("signed_angle_deg"), "signed_angle_deg"),
                capture_width_deg=_optional_float(
                    edge.get("capture_width_deg"), "capture_width_deg"
                ),
                geometric_score=_optional_float(edge.get("geometric_score"), "geometric_score"),
            )
        )

    known = {p.pose_id: p.quaternion_xyzw for p in poses}
    for raw in payload.get("composite_transfers", []):
        commands = validate_transfer(raw, known)
        transitions.append(
            RoadmapTransition(
                raw["edge_id"],
                int(raw["source"]),
                int(raw["target"]),
                "multi_reorientation",
                f"multiple_reorientation_{len(commands)}",
                None,
                None,
                None,
                len(commands),
                (),
                (),
                commands,
                "geometry_only",
            )
        )

    states, records, routes = read_export(payload)
    by_id = {e.edge_id: e for e in transitions}
    by_id.update({r["edge_id"]: _exported_choice(r) for r in records})
    transitions = list(by_id.values())
    mesh_source = str(payload.get("source", source.stem))
    part_name = str(
        payload.get("part_name") or Path(mesh_source).stem or source.stem.replace("_roadmap", "")
    )
    return RoadmapDocument(
        path=source,
        part_name=part_name,
        mesh_path=mesh_path,
        cad_status=str(payload.get("geometry_status", "unknown")),
        poses=tuple(poses),
        transitions=tuple(transitions),
        member_states=states,
        member_route_data=routes,
        consumer_export_version=payload.get("consumer_export", {}).get("version", 1),
    )


def _exported_choice(record):
    commands = tuple(record.get("commands", []))
    return RoadmapTransition(
        edge_id=record["edge_id"],
        source_pose_id=record["source"],
        target_pose_id=record["target"],
        transition_kind=record.get("transition_kind", "member_transfer"),
        actuation=record.get("actuation", ""),
        signed_angle_deg=record.get(
            "signed_angle_deg",
            commands[0]["signed_angle_deg"] if len(commands) == 1 else None,
        ),
        capture_width_deg=record.get("capture_width_deg"),
        geometric_score=record.get("geometric_score"),
        flip_count=record.get("actuation_count", len(commands) or 1),
        via_pose_ids=tuple(record.get("via_pose_ids", [])),
        component_edge_ids=tuple(record.get("component_edge_ids", [])),
        commands=commands,
        evidence_status=record.get("evidence_status", "geometry_only"),
        member_context=record if record.get("source_member_ids") else None,
        preview_segments=tuple(record.get("preview_segments", [])),
        display_label=record.get("display_name", ""),
        preference_cost=tuple(record.get("preference_cost", [])),
        source_group_key=record.get("source_group_key", ""),
    )


def pose_pixmap(pose: RoadmapPose, width: int, height: int) -> QPixmap:
    if pose.mesh_path is not None and pose.mesh_path.is_file():
        try:
            return render_mesh_preview(
                pose.mesh_path,
                width,
                height,
                quaternion_xyzw=pose.quaternion_xyzw,
                caption=f"Roadmap pose {pose.pose_id}",
            )
        except (ImportError, OSError, ValueError):
            pass
    pixmap = QPixmap()
    if pose.thumbnail_png and pixmap.loadFromData(pose.thumbnail_png, "PNG"):
        return pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


_ACTION_LABELS = {
    "floor_main_neg_x": "−X · main face on floor",
    "floor_main_pos_x": "+X · main face on floor",
    "wall_main_neg_x": "−X · main face on wall",
    "wall_main_pos_x": "+X · main face on wall",
    "free_y": "free Y rotation",
    "free_z": "free Z rotation",
    "passive": "passive tipping",
    "multiple_reorientation_2": "Multiple reorientation · 2 flips",
    "multiple_reorientation_3": "Multiple reorientation · 3 flips",
}


def action_display_label(actuation: str) -> str:
    if actuation.startswith("multiple_reorientation_"):
        return f"Multiple reorientation · {actuation.rsplit('_', 1)[-1]} rotations"
    return _ACTION_LABELS.get(actuation, actuation)


class RoadmapMap(QWidget):
    """Clickable robust-pose layer using the exported geometry roadmap layout."""

    def __init__(self, document: RoadmapDocument, on_pose_clicked: Any) -> None:
        super().__init__()
        self.document = document
        self.on_pose_clicked = on_pose_clicked
        self.poses = tuple(pose for pose in document.poses if pose.stability == "robust")
        self.start_pose_id: int | None = None
        self.end_pose_id: int | None = None
        self._pixmaps = {pose.pose_id: self._thumbnail(pose) for pose in self.poses}
        self._grid_slots, self._columns, self._rows = self._optimised_grid_slots()
        self.setMinimumHeight(560)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click a pose image to choose start, then end pose")

    def _thumbnail(self, pose: RoadmapPose) -> QPixmap:
        """Use the exact thumbnail embedded by geometry_to_pose when available."""
        pixmap = QPixmap()
        if pose.thumbnail_png and pixmap.loadFromData(pose.thumbnail_png, "PNG"):
            return pixmap
        return pose_pixmap(pose, 160, 120)

    @property
    def layout_summary(self) -> str:
        return f"Layout: {self._rows} row(s) × {self._columns} column(s) · path distance optimized"

    def _optimised_grid_slots(self) -> tuple[dict[int, tuple[int, int]], int, int]:
        """Place robust poses on a compact grid and shorten all visible paths.

        This follows the geometry-to-pose plot strategy: choose a grid near its
        3:2 canvas ratio, then make deterministic best-improvement swaps.  Unlike
        the exported plot, metastable nodes are excluded before laying out cards,
        so robust thumbnails never inherit crowded or empty metastable slots.
        """
        count = len(self.poses)
        if not count:
            return {}, 1, 1
        candidates: list[tuple[float, int, int]] = []
        for columns in range(1, count + 1):
            rows = math.ceil(count / columns)
            aspect_error = abs(math.log((columns / rows) / 1.5))
            unused_fraction = (columns * rows - count) / count
            candidates.append((aspect_error + 0.12 * unused_fraction, columns, rows))
        _, columns, rows = min(candidates)
        pose_ids = [pose.pose_id for pose in self.poses]
        slots = {
            pose_id: (index % columns, index // columns) for index, pose_id in enumerate(pose_ids)
        }
        robust_ids = set(pose_ids)
        weights: dict[tuple[int, int], int] = {}
        for edge in self.document.overview_transitions:
            if (
                (
                    edge.is_multi_reorientation
                    and edge.transition_kind not in {"member_transfer", "group_transfer"}
                )
                or not edge.calibratable
                or edge.source_pose_id not in robust_ids
                or edge.target_pose_id not in robust_ids
            ):
                continue
            key = tuple(sorted((edge.source_pose_id, edge.target_pose_id)))
            weights[key] = weights.get(key, 0) + 1

        def distance(left: int, right: int) -> float:
            left_slot, right_slot = slots[left], slots[right]
            return math.hypot(left_slot[0] - right_slot[0], left_slot[1] - right_slot[1])

        # The grid is deliberately fixed; only which pose occupies a cell moves.
        # Greedy best-improvement is stable and practical for roadmap sizes here.
        for _ in range(max(1, 2 * count)):
            best_gain = 1e-9
            best_swap: tuple[int, int] | None = None
            for left_index, left in enumerate(pose_ids):
                for right in pose_ids[left_index + 1 :]:
                    affected = [
                        (source, target, weight)
                        for (source, target), weight in weights.items()
                        if left in {source, target} or right in {source, target}
                    ]
                    before = sum(
                        weight * distance(source, target) for source, target, weight in affected
                    )
                    slots[left], slots[right] = slots[right], slots[left]
                    after = sum(
                        weight * distance(source, target) for source, target, weight in affected
                    )
                    slots[left], slots[right] = slots[right], slots[left]
                    if before - after > best_gain:
                        best_gain = before - after
                        best_swap = (left, right)
            if best_swap is None:
                break
            left, right = best_swap
            slots[left], slots[right] = slots[right], slots[left]
        return slots, columns, rows

    def _card_size(self) -> tuple[int, int]:
        rect = self.contentsRect().adjusted(26, 22, -26, -36)
        cell_width = max(1, rect.width() / self._columns)
        cell_height = max(1, rect.height() / self._rows)
        width = max(64, min(160, round(cell_width - 24)))
        height = max(48, min(round(width * 0.75), round(cell_height - 30)))
        return width, height

    def _positions(self) -> dict[int, tuple[float, float]]:
        rect = self.contentsRect().adjusted(26, 22, -26, -22)
        return {
            pose.pose_id: (
                rect.left()
                + (self._grid_slots[pose.pose_id][0] + 0.5) * rect.width() / self._columns,
                rect.top() + (self._grid_slots[pose.pose_id][1] + 0.5) * rect.height() / self._rows,
            )
            for pose in self.poses
        }

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fbfdff"))
        positions = self._positions()
        card_width, card_height = self._card_size()
        for edge in self.document.overview_transitions:
            if (
                (
                    edge.is_multi_reorientation
                    and edge.transition_kind not in {"member_transfer", "group_transfer"}
                )
                or not edge.calibratable
                or edge.source_pose_id not in positions
                or edge.target_pose_id not in positions
            ):
                continue
            start, end = positions[edge.source_pose_id], positions[edge.target_pose_id]
            color = QColor(
                "#0e827b"
                if edge.member_context
                else "#d62728"
                if edge.actuation.endswith("_x")
                else "#2ca02c"
                if edge.actuation == "free_y"
                else "#1f77b4"
            )
            painter.setPen(QPen(color, 2.4, Qt.PenStyle.SolidLine))
            painter.drawLine(round(start[0]), round(start[1]), round(end[0]), round(end[1]))
            if self.document.consumer_export_version == 2:
                # Offset opposite directions and label the exported preferred command.
                dx, dy = end[0] - start[0], end[1] - start[1]
                length = math.hypot(dx, dy) or 1.0
                ux, uy = dx / length, dy / length
                x = start[0] + dx * 0.42 - uy * 14
                y = start[1] + dy * 0.42 + ux * 14
                painter.drawLine(
                    round(x),
                    round(y),
                    round(x - ux * 9 - uy * 5),
                    round(y - uy * 9 + ux * 5),
                )
                painter.drawLine(
                    round(x),
                    round(y),
                    round(x - ux * 9 + uy * 5),
                    round(y - uy * 9 - ux * 5),
                )
                label = edge.display_label
                width = painter.fontMetrics().horizontalAdvance(label) + 8
                painter.fillRect(round(x - width / 2), round(y - 23), width, 18, QColor("#fbfdff"))
                painter.drawText(round(x - width / 2 + 4), round(y - 9), label)
        for pose_id, (x, y) in positions.items():
            if pose_id == self.start_pose_id:
                border = QColor("#e3a008")
            elif pose_id == self.end_pose_id:
                border = QColor("#18a558")
            else:
                border = QColor("#084081")
            pixmap = self._pixmaps[pose_id]
            width, height = (card_width, card_height) if not pixmap.isNull() else (82, 58)
            left, top = round(x - width / 2), round(y - height / 2)
            painter.setPen(QPen(border, 4))
            painter.setBrush(QBrush(QColor("white")))
            painter.drawRect(left - 3, top - 3, width + 6, height + 6)
            if pixmap.isNull():
                painter.setPen(QPen(QColor("#374151")))
                painter.drawText(
                    left,
                    top,
                    width,
                    height,
                    Qt.AlignmentFlag.AlignCenter,
                    ("Group" if self.document.consumer_export_version == 2 else "Pose")
                    + f" {pose_id}",
                )
            else:
                painter.drawPixmap(left, top, width, height, pixmap)
            painter.setPen(QPen(QColor("#111827")))
            painter.drawText(
                left,
                top + height + 4,
                width,
                18,
                Qt.AlignmentFlag.AlignCenter,
                f"{'Group' if self.document.consumer_export_version == 2 else 'Pose'} {pose_id}",
            )

    def mousePressEvent(self, event) -> None:
        positions = self._positions()
        card_width, card_height = self._card_size()
        for pose in self.poses:
            x, y = positions[pose.pose_id]
            if (
                abs(event.position().x() - x) <= card_width / 2 + 4
                and abs(event.position().y() - y) <= card_height / 2 + 4
            ):
                self.on_pose_clicked(pose.pose_id)
                return


class RoadmapTransitionDialog(QDialog):
    def __init__(
        self,
        document: RoadmapDocument,
        parent: QWidget | None = None,
        profile_directory: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.document = document
        self.profile_directory = profile_directory
        self.selected_transition: SelectedRoadmapTransition | None = None
        self.start_pose_id: int | None = None
        self.end_pose_id: int | None = None
        self._transitions_by_id = {item.edge_id: item for item in document.transitions}
        self._saved_profiles_by_edge = self._find_saved_profiles()
        self.setWindowTitle(f"Select roadmap path · {document.part_name}")
        self.setModal(True)
        self.resize(1320, 980)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(f"<b>{self.document.part_name}</b> · CAD status: {self.document.cad_status}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:18px; padding:6px;")
        layout.addWidget(title)
        hint = QLabel(
            "Click the blue roadmap map: first choose the <b>start</b> pose, then the "
            "<b>end</b> pose. Only paths joining those two poses are shown below. "
            "They are ordered from one flip to multi-flip alternatives."
        )
        hint.setWordWrap(True)
        if self.document.member_states:
            hint.setText(
                hint.text()
                + " Transfers depend on the listed source members. Capture intervals describe "
                "sampled downhill geometry; declare tested source coverage before runtime use."
            )
        hint.setStyleSheet(
            "background:#eef6ff;border:1px solid #7aa7d9;border-radius:5px;padding:8px;"
        )
        layout.addWidget(hint)
        map_box = QGroupBox("Pose roadmap")
        map_layout = QVBoxLayout(map_box)
        self.map_widget = RoadmapMap(self.document, self._pose_clicked)
        map_layout.addWidget(self.map_widget)
        map_footer = QHBoxLayout()
        self.pose_selection_label = QLabel("Start: —    End: —")
        self.pose_selection_label.setStyleSheet("font-weight:600; color:#1f3b53;")
        map_footer.addWidget(self.pose_selection_label)
        layout_label = QLabel(self.map_widget.layout_summary)
        layout_label.setStyleSheet("color:#4b5563;")
        map_footer.addWidget(layout_label)
        map_footer.addStretch(1)
        clear_button = QPushButton("Choose start again")
        clear_button.clicked.connect(self._clear_pose_selection)
        map_footer.addWidget(clear_button)
        map_layout.addLayout(map_footer)
        layout.addWidget(map_box)

        transition_box = QGroupBox("Paths for selected poses")
        transition_layout = QVBoxLayout(transition_box)
        self.path_hint = QLabel("Select a start and end pose on the map.")
        self.path_hint.setWordWrap(True)
        transition_layout.addWidget(self.path_hint)
        self.transition_table = QTableWidget(0, 4)
        self.transition_table.setHorizontalHeaderLabels(
            ["Path", "Flips", "Flip axis / angle", "Saved profile"]
        )
        self.transition_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.transition_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.transition_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.transition_table.setAlternatingRowColors(True)
        self.transition_table.verticalHeader().setVisible(False)
        header = self.transition_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.transition_table.itemSelectionChanged.connect(self._on_transition_selection_changed)
        self.transition_table.cellDoubleClicked.connect(
            lambda _row, _column: self._accept_selected_transition()
        )
        transition_layout.addWidget(self.transition_table)
        layout.addWidget(transition_box, 1)

        self.command_preview = QLabel()
        self.command_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.command_preview.setVisible(False)
        layout.addWidget(self.command_preview)
        self.group_coverage_checkbox = QCheckBox("Calibrated for source group")
        self.group_coverage_checkbox.setVisible(self.document.consumer_export_version == 2)
        layout.addWidget(self.group_coverage_checkbox)
        self.command_slider = QSlider(Qt.Orientation.Horizontal)
        self.command_slider.setRange(0, 1000)
        self.command_slider.setVisible(False)
        self.command_slider.valueChanged.connect(self._preview_command)
        layout.addWidget(self.command_slider)
        self.coverage_edit = QLineEdit()
        self.coverage_edit.setPlaceholderText(
            "Explicitly tested source member IDs, comma separated; blank = calibration only"
        )
        self.coverage_edit.setVisible(
            bool(self.document.member_states) and self.document.consumer_export_version != 2
        )
        layout.addWidget(self.coverage_edit)

        self.selection_label = QLabel("No path selected")
        footer = QHBoxLayout()
        footer.addWidget(self.selection_label)
        footer.addStretch(1)
        self.use_button = QPushButton("Use selected path")
        self.use_button.setEnabled(False)
        self.use_button.setStyleSheet(
            "QPushButton:enabled {background:#1677c8;color:white;font-weight:600;padding:7px 14px;}"
        )
        self.use_button.clicked.connect(self._accept_selected_transition)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        footer.addWidget(self.use_button)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def _find_saved_profiles(self) -> dict[str, tuple[Path, ...]]:
        found: dict[str, list[Path]] = {}
        directory = self.profile_directory
        if directory is None or not directory.is_dir():
            return {}
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            metadata = payload.get("roadmap_transition") if isinstance(payload, dict) else None
            for transition in self._transitions_by_id.values():
                matches_metadata = False
                if isinstance(metadata, dict):
                    try:
                        matches_metadata = str(
                            metadata.get("edge_id", "")
                        ) == transition.edge_id or (
                            not transition.evidence_status
                            and int(metadata.get("source_pose_id", -1)) == transition.source_pose_id
                            and int(metadata.get("target_pose_id", -1)) == transition.target_pose_id
                            and str(metadata.get("actuation", "")) == transition.actuation
                            and int(metadata.get("flip_count", 1)) == transition.flip_count
                        )
                    except (TypeError, ValueError):
                        pass
                name_stem = SelectedRoadmapTransition(
                    self.document.path,
                    self.document.part_name,
                    transition,
                    self.document.pose(transition.source_pose_id),
                    self.document.pose(transition.target_pose_id),
                ).profile_name_stem
                if matches_metadata or (
                    not transition.evidence_status
                    and path.stem
                    in {name_stem, name_stem.replace("_Transition_", "_Uebergang_", 1)}
                ):
                    found.setdefault(transition.edge_id, []).append(path)
        return {edge_id: tuple(paths) for edge_id, paths in found.items()}

    def _pose_clicked(self, pose_id: int) -> None:
        if self.start_pose_id is None or self.end_pose_id is not None:
            self.start_pose_id, self.end_pose_id = pose_id, None
        elif pose_id == self.start_pose_id:
            if any(
                e.member_context and e.source_pose_id == pose_id and e.target_pose_id == pose_id
                for e in self.document.transitions
            ):
                self.end_pose_id = pose_id
            else:
                self.pose_selection_label.setText(
                    f"Start: Pose {pose_id}    End: choose a different pose"
                )
                return
        else:
            self.end_pose_id = pose_id
        self._refresh_paths()

    def _clear_pose_selection(self) -> None:
        self.start_pose_id = None
        self.end_pose_id = None
        self._refresh_paths()

    def _set_pose_selection(self, start_pose_id: int, end_pose_id: int) -> None:
        """Small test/programmatic hook using the same filter as map clicks."""
        self.start_pose_id = start_pose_id
        self.end_pose_id = end_pose_id
        self._refresh_paths()

    def _refresh_paths(self) -> None:
        self.map_widget.start_pose_id = self.start_pose_id
        self.map_widget.end_pose_id = self.end_pose_id
        self.map_widget.update()
        self.transition_table.setRowCount(0)
        self.use_button.setEnabled(False)
        self.selection_label.setText("No path selected")
        self.command_preview.setVisible(False)
        self.command_slider.setVisible(False)
        if self.start_pose_id is None:
            self.pose_selection_label.setText("Start: —    End: —")
            self.path_hint.setText("Select a start and end pose on the map.")
            return
        if self.end_pose_id is None:
            self.pose_selection_label.setText(f"Start: Pose {self.start_pose_id}    End: —")
            self.path_hint.setText("Now click the desired end pose.")
            return
        self.pose_selection_label.setText(
            f"Start: Pose {self.start_pose_id}    End: Pose {self.end_pose_id}"
        )
        for route in self.document.route_options(self.start_pose_id, self.end_pose_id):
            self._transitions_by_id.setdefault(route.edge_id, route)
        self._saved_profiles_by_edge = self._find_saved_profiles()
        options = sorted(
            (
                item
                for item in self._transitions_by_id.values()
                if item.calibratable
                and (self.document.consumer_export_version != 2 or item.source_group_key)
                and item.source_pose_id == self.start_pose_id
                and item.target_pose_id == self.end_pose_id
            ),
            key=lambda item: (
                (item.preference_cost, item.edge_id)
                if self.document.consumer_export_version == 2
                else (item.flip_count, item.edge_id)
            ),
        )
        if not options:
            self.path_hint.setText("No calibratable path was found for these two poses.")
            return
        self.path_hint.setText(
            f"{len(options)} path option(s), ordered by required reorientations."
        )
        for transition in options:
            self._add_transition_row(transition)

    def _step_summary(self, transition: RoadmapTransition) -> str:
        if transition.source_group_key:
            return transition.display_label
        member = transition.member_context
        prefix = ""
        if member:
            prefix = (
                "From "
                + ", ".join(member["source_member_ids"])
                + " → possible "
                + ", ".join(member["possible_target_member_ids"])
                + " · "
            )
            if member.get("capture_interval_deg") is not None:
                prefix += f"capture {member['capture_interval_deg']}° · "
        if transition.commands:
            prefix += (
                "Geometry only · calibration required · " if transition.evidence_status else ""
            )
            return prefix + " → ".join(
                f"{c['axis'].upper()} {c['signed_angle_deg']:+.1f}°" for c in transition.commands
            )
        if not transition.is_multi_reorientation:
            angle = (
                "angle not specified"
                if transition.signed_angle_deg is None
                else f"{transition.signed_angle_deg:+.1f}°"
            )
            return f"{action_display_label(transition.actuation)} ({angle})"
        steps: list[str] = []
        for edge_id in transition.component_edge_ids:
            edge = self._transitions_by_id.get(edge_id)
            if edge is None:
                steps.append(edge_id)
                continue
            angle = "?" if edge.signed_angle_deg is None else f"{edge.signed_angle_deg:+.1f}°"
            steps.append(f"{action_display_label(edge.actuation)} ({angle})")
        return "  →  ".join(steps) or "component flips not available"

    def _add_transition_row(self, transition: RoadmapTransition) -> None:
        row = self.transition_table.rowCount()
        self.transition_table.insertRow(row)
        pose_ids = (
            transition.source_pose_id,
            *transition.via_pose_ids,
            transition.target_pose_id,
        )
        saved = self._saved_profiles_by_edge.get(transition.edge_id, ())
        values = (
            " → ".join(
                f"{'Group' if transition.source_group_key else 'Pose'} {item}" for item in pose_ids
            ),
            str(transition.flip_count),
            self._step_summary(transition),
            "Saved: " + ", ".join(path.name for path in saved) if saved else "No saved profile",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, transition.edge_id)
            if transition.flip_count == 1:
                item.setBackground(QColor("#dcefff"))
                item.setForeground(QColor("#123a58"))
            else:
                item.setBackground(QColor("#fff3cd"))
                item.setForeground(QColor("#664d03"))
                item.setToolTip("Experimental direct profile for this complete multi-flip path.")
            if transition.source_group_key:
                item.setToolTip(group_evidence_details(transition.member_context))
            self.transition_table.setItem(row, column, item)
        self.transition_table.setRowHeight(row, 34)

    def _selected_table_transition(self) -> RoadmapTransition | None:
        rows = self.transition_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.transition_table.item(rows[0].row(), 0)
        return self._transitions_by_id.get(str(item.data(Qt.ItemDataRole.UserRole)))

    def _on_transition_selection_changed(self) -> None:
        transition = self._selected_table_transition()
        self.use_button.setEnabled(transition is not None)
        if transition is None:
            self.selection_label.setText("No path selected")
        else:
            self.selection_label.setText(
                f"Selected: {transition.flip_count} flip(s) · {self._step_summary(transition)}"
            )
        show = bool(transition and transition.commands)
        self.coverage_edit.setText(
            ", ".join(transition.covered_source_member_ids) if transition else ""
        )
        if transition and transition.source_group_key:
            self.selection_label.setToolTip(group_evidence_details(transition.member_context))
            self.group_coverage_checkbox.setChecked(
                transition.calibrated_source_group_key == transition.source_group_key
            )
            self.group_coverage_checkbox.setToolTip(transition.source_group_key)
        if transition and transition.member_context:
            self.coverage_edit.setToolTip(
                "Source group members: "
                + ", ".join(transition.member_context.get("source_group_member_ids", []))
            )
        self.command_slider.setVisible(show)
        self.command_preview.setVisible(show)
        if show:
            self._preview_command(self.command_slider.value())

    def _preview_command(self, value):
        transition = self._selected_table_transition()
        if transition is None or not transition.commands:
            return
        q = (
            preview_frame(transition.preview_segments, value / 1000)
            if transition.preview_segments
            else None
        ) or command_frame(transition.commands, value / 1000)
        if self.document.mesh_path and self.document.mesh_path.is_file():
            try:
                pixmap = render_mesh_preview(
                    self.document.mesh_path,
                    500,
                    220,
                    quaternion_xyzw=q,
                    caption="Transient orientation · geometry only"
                    if transition.evidence_status
                    else "Route orientation",
                )
                self.command_preview.setPixmap(pixmap)
                return
            except (ImportError, OSError, ValueError):
                pass
        self.command_preview.setText(
            "Transient orientation xyzw: " + ", ".join(f"{v:.4f}" for v in q)
        )

    def _accept_selected_transition(self) -> None:
        transition = self._selected_table_transition()
        if transition is None:
            return
        if transition.source_group_key:
            transition = replace(
                transition,
                calibrated_source_group_key=(
                    transition.source_group_key if self.group_coverage_checkbox.isChecked() else ""
                ),
            )
        elif self.document.member_states:
            allowed = {
                s["member_id"]
                for s in self.document.member_states
                if s["retained"] and s["group_id"] == transition.source_pose_id
            }
            try:
                declared = (
                    list(allowed)
                    if self.coverage_edit.text().strip() == "*"
                    else [s.strip() for s in self.coverage_edit.text().split(",") if s.strip()]
                )
                coverage = source_coverage(declared, allowed)
            except ValueError as error:
                QMessageBox.warning(self, "Source member coverage", str(error))
                return
            transition = replace(transition, covered_source_member_ids=coverage)
        source_pose = self.document.pose(transition.source_pose_id)
        target_pose = self.document.pose(transition.target_pose_id)
        if transition.member_context:
            states = {s["member_id"]: s for s in self.document.member_states}
            a = transition.member_context.get(
                "preview_source_member_id",
                transition.member_context["source_member_ids"][0],
            )
            b = transition.member_context.get(
                "preview_target_member_id",
                transition.member_context["possible_target_member_ids"][0],
            )
            source_pose = replace(
                source_pose,
                quaternion_xyzw=tuple(states[a]["quaternion_xyzw"]),
                thumbnail_png=None,
            )
            target_pose = replace(
                target_pose,
                quaternion_xyzw=tuple(states[b]["quaternion_xyzw"]),
                thumbnail_png=None,
            )
        self.selected_transition = SelectedRoadmapTransition(
            roadmap_path=self.document.path,
            part_name=self.document.part_name,
            transition=transition,
            source_pose=source_pose,
            target_pose=target_pose,
            component_flips=tuple(
                self._transitions_by_id[edge_id] for edge_id in transition.component_edge_ids
            ),
        )
        self.accept()
