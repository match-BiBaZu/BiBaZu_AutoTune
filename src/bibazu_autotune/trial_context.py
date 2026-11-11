"""Portable trial identities; geometry and material analysis are session-only."""

from __future__ import annotations

import copy
import math
import re
from typing import NotRequired, TypedDict

from .orientation_animation import rotate

Quaternion = tuple[float, float, float, float]


class ManualContext(TypedDict):
    kind: str
    part_name: str
    mesh_sha256: str
    source_quaternion_xyzw: list[float]
    target_quaternion_xyzw: list[float]
    title: str
    rotation_axis: NotRequired[str]
    signed_angle_deg: NotRequired[float]


def normalize_quaternion(value) -> Quaternion:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Quaternion benötigt vier Zahlen in der Reihenfolge x, y, z, w.")
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) for v in value):
        raise ValueError("Quaternion muss endliche Zahlen enthalten.")
    if not all(math.isfinite(v) for v in value):
        raise ValueError("Quaternion muss endliche Zahlen enthalten.")
    # Scale first to avoid overflow/underflow for otherwise valid inputs.
    scale = max(abs(v) for v in value)
    if scale == 0:
        raise ValueError("Quaternion darf nicht der Nullvektor sein.")
    scaled = tuple(v / scale for v in value)
    norm = math.sqrt(sum(v * v for v in scaled))
    result = tuple(v / norm for v in scaled)
    # q and -q describe the same orientation and must share an identity.
    sign = next((v for v in reversed(result) if v != 0), 1)
    return tuple(-v if sign < 0 else v for v in result)


def manual_context(
    part_name, mesh_sha256, source, target, *, rotation_axis=None, signed_angle_deg=None
) -> ManualContext:
    if not isinstance(part_name, str) or not part_name.strip():
        raise ValueError("Bauteilname fehlt.")
    if not isinstance(mesh_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", mesh_sha256):
        raise ValueError("Manueller Versuch benötigt einen gültigen STL-Inhaltshash.")
    result = ManualContext(
        kind="manual",
        part_name=part_name.strip(),
        mesh_sha256=mesh_sha256.lower(),
        source_quaternion_xyzw=list(normalize_quaternion(source)),
        target_quaternion_xyzw=list(normalize_quaternion(target)),
        title="Manueller Übergang",
    )
    if rotation_axis is not None or signed_angle_deg is not None:
        if rotation_axis not in ("x", "y", "z") or (
            isinstance(signed_angle_deg, bool)
            or not isinstance(signed_angle_deg, (int, float))
            or not math.isfinite(signed_angle_deg)
            or not 0 < abs(signed_angle_deg) <= 360
        ):
            raise ValueError("Manuelle Kippachse und Drehwinkel müssen gemeinsam gültig sein.")
        expected = normalize_quaternion(
            rotate(result["source_quaternion_xyzw"], rotation_axis, signed_angle_deg)
        )
        if (
            abs(sum(a * b for a, b in zip(expected, result["target_quaternion_xyzw"], strict=True)))
            < 1 - 1e-8
        ):
            raise ValueError("Manuelle Kippachse und Drehwinkel passen nicht zur Zielorientierung.")
        result.update(rotation_axis=rotation_axis, signed_angle_deg=float(signed_angle_deg))
    return result


def validate_context(context):
    if not isinstance(context, dict):
        raise ValueError("Versuchskontext fehlt.")
    if context.get("kind") == "manual":
        if ("rotation_axis" in context) != ("signed_angle_deg" in context) or (
            "rotation_axis" in context
            and (context["rotation_axis"] is None or context["signed_angle_deg"] is None)
        ):
            raise ValueError("Manuelle Kippachse und Drehwinkel müssen gemeinsam gültig sein.")
        return manual_context(
            context.get("part_name"),
            context.get("mesh_sha256"),
            context.get("source_quaternion_xyzw"),
            context.get("target_quaternion_xyzw"),
            rotation_axis=context.get("rotation_axis"),
            signed_angle_deg=context.get("signed_angle_deg"),
        )
    if not {"part_name", "source_pose_id", "target_pose_id"} <= context.keys():
        raise ValueError("Roadmap-Transition fehlt oder ist unvollständig.")
    return copy.deepcopy(context)


def context_fields(context):
    context = validate_context(context)
    key = "trial_context" if context.get("kind") == "manual" else "roadmap_transition"
    return {key: context}


def read_context(document):
    manual = document.get("trial_context")
    roadmap = document.get("roadmap_transition")
    if manual is not None and roadmap is not None:
        raise ValueError("Datei enthält widersprüchliche Versuchskontexte.")
    if manual is not None:
        if not isinstance(manual, dict) or manual.get("kind") != "manual":
            raise ValueError("trial_context muss einen manuellen Übergang enthalten.")
        return validate_context(manual)
    if isinstance(roadmap, dict) and roadmap.get("kind") == "manual":
        raise ValueError("Manuelle Übergänge gehören in trial_context.")
    return validate_context(roadmap)


def manual_identity(context):
    context = validate_context(context)
    identity = [
        "manual",
        context["part_name"],
        context["mesh_sha256"],
        *[round(v, 14) for v in context["source_quaternion_xyzw"]],
        *[round(v, 14) for v in context["target_quaternion_xyzw"]],
    ]
    if "rotation_axis" in context:
        identity += [context["rotation_axis"], context["signed_angle_deg"]]
    return identity
