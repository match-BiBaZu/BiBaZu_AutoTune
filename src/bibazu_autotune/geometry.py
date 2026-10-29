"""STL analysis in SI units, independent of Qt and machine connections."""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh

Vector = tuple[float, float, float]
Tensor = tuple[Vector, Vector, Vector]
Triangle = tuple[Vector, Vector, Vector]
UNIT_SCALE = {"mm": 0.001, "cm": 0.01, "m": 1.0}


@dataclass(frozen=True)
class GeometryProperties:
    dimensions_m: Vector
    area_m2: float
    volume_m3: float | None
    center_m: Vector | None
    inertia_m5: Tensor | None
    triangle_count: int
    valid_volume: bool


@dataclass(frozen=True)
class MaterialProperties:
    density_kg_m3: float | None = None
    mass_kg: float | None = None
    inertia_kg_m2: Tensor | None = None


@dataclass(frozen=True)
class MeshAnalysis:
    path: Path
    sha256: str
    unit: str
    geometry: GeometryProperties
    material: MaterialProperties
    preview_triangles: tuple[Triangle, ...]
    warning: str = ""
    vertices_m: np.ndarray = field(
        default_factory=lambda: np.empty((0, 3)), repr=False, compare=False
    )
    faces: np.ndarray = field(
        default_factory=lambda: np.empty((0, 3), dtype=int), repr=False, compare=False
    )


def material_properties(geometry: GeometryProperties, density=None) -> MaterialProperties:
    if density is None:
        return MaterialProperties()
    if isinstance(density, bool) or not isinstance(density, (int, float)):
        raise ValueError("Dichte muss eine positive, endliche Zahl in kg/m³ sein.")
    if not math.isfinite(density) or density <= 0:
        raise ValueError("Dichte muss eine positive, endliche Zahl in kg/m³ sein.")
    if not geometry.valid_volume:
        return MaterialProperties(density_kg_m3=density)
    mass = geometry.volume_m3 * density
    inertia = np.asarray(geometry.inertia_m5) * density
    if not math.isfinite(mass) or not np.isfinite(inertia).all():
        raise ValueError("Materialberechnung liegt außerhalb des endlichen Zahlenbereichs.")
    return MaterialProperties(density, mass, _tensor(inertia))


def _vector(array) -> Vector:
    return tuple(float(v) for v in array)


def _tensor(array) -> Tensor:
    return tuple(_vector(row) for row in array)


def analyze_stl(path: str | Path, unit="mm", density=None) -> MeshAnalysis:
    if unit not in UNIT_SCALE:
        raise ValueError("STL-Einheit muss mm, cm oder m sein.")
    source = Path(path).resolve()
    if source.suffix.lower() != ".stl":
        raise ValueError("Für Autotune bitte eine STL-Datei auswählen.")
    data = source.read_bytes()
    try:
        mesh = trimesh.load_mesh(io.BytesIO(data), file_type="stl", process=False)
    except Exception as exc:
        raise ValueError(f"STL konnte nicht gelesen werden: {source.name}") from exc
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("STL enthält keine Dreiecke.")
    if not np.isfinite(mesh.vertices).all():
        raise ValueError("STL enthält nicht endliche Koordinaten.")
    # Weld STL's duplicated vertices, without repairing holes or changing winding.
    mesh.process(validate=False)
    mesh.apply_scale(UNIT_SCALE[unit])
    dimensions = mesh.extents.copy()
    origin = mesh.bounds.mean(axis=0)
    mesh.apply_translation(-origin)
    area = float(mesh.area)
    if not np.isfinite(dimensions).all() or not math.isfinite(area) or area <= 0:
        raise ValueError("STL enthält keine endliche, nicht entartete Oberfläche.")
    valid = bool(mesh.is_volume)
    volume = float(mesh.volume) if valid else None
    center = mesh.center_mass + origin if valid else None
    inertia = mesh.moment_inertia if valid else None
    if valid and (
        not math.isfinite(volume) or not np.isfinite(center).all() or not np.isfinite(inertia).all()
    ):
        raise ValueError("STL-Masseneigenschaften sind nicht endlich.")
    geometry = GeometryProperties(
        _vector(dimensions),
        area,
        volume,
        _vector(center) if valid else None,
        _tensor(inertia) if valid else None,
        len(mesh.faces),
        valid,
    )
    # Bound preview work while keeping the full mesh for every numerical integral.
    stride = max(1, math.ceil(len(mesh.faces) / 1500))
    triangles = tuple(_tensor(t) for t in mesh.triangles[::stride])
    vertices = np.asarray(mesh.vertices + origin).copy()
    faces = np.asarray(mesh.faces).copy()
    vertices.setflags(write=False)
    faces.setflags(write=False)
    warning = (
        ""
        if valid
        else (
            "Mesh ist offen oder inkonsistent orientiert: Volumen, Volumenschwerpunkt "
            "und Trägheit sind nicht verfügbar."
        )
    )
    return MeshAnalysis(
        source,
        hashlib.sha256(data).hexdigest(),
        unit,
        geometry,
        material_properties(geometry, density),
        triangles,
        warning,
        vertices,
        faces,
    )
