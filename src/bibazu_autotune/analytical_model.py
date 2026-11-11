"""Static tipping impulse and additive jets. All physical calculations use SI units.

The supplied drag equation assumes independent jets at common stagnation pressure.
Neither motion during the impulse nor contact/friction losses are simulated here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise
from typing import Literal, Protocol

import numpy as np
from shapely import LineString, Point, polygons, union_all

from .geometry import MeshAnalysis, Tensor, Vector
from .orientation_animation import multiply
from .pressure_scaling import (
    DEFAULT_PRESSURE_SCALE_A,
    DEFAULT_PRESSURE_SCALE_B_PA,
    SpsPressurePrediction,
    scale_pressure,
    validate_pressure_scaling,
)
from .trial_context import normalize_quaternion

Axis = Literal["x", "y", "z"]
CandidateKey = tuple[int, int]  # Array and reference nozzle (1 or 4).
EPS = 1e-10
DEFAULT_ROW_CENTER_M = 0.01244
DEFAULT_NOZZLE_SPACING_M = 0.02488


def _vector(values) -> Vector:
    return tuple(float(v) for v in values)


def _tensor(values) -> Tensor:
    return tuple(_vector(row) for row in values)


@dataclass(frozen=True)
class NozzleMount:
    array_index: int
    nozzle_index: int
    rotation_axis: Axis
    normal_axis: Axis
    row_center_m: float | None = None
    longitudinal_offset_m: float | None = 0.0


def default_mounts() -> tuple[NozzleMount, ...]:
    mounts = []
    for array_index in range(1, 5):
        normal_axis = "y" if array_index % 2 else "z"
        tipping_axis = "z" if array_index % 2 else "y"
        for nozzle_index in range(1, 7):
            mounts.append(
                NozzleMount(
                    array_index,
                    nozzle_index,
                    tipping_axis if nozzle_index <= 3 else "x",
                    normal_axis,
                    row_center_m=DEFAULT_ROW_CENTER_M
                    + (nozzle_index - 1) * DEFAULT_NOZZLE_SPACING_M
                    if nozzle_index <= 3
                    else DEFAULT_ROW_CENTER_M,
                    longitudinal_offset_m=(nozzle_index - 4) * DEFAULT_NOZZLE_SPACING_M
                    if nozzle_index >= 4
                    else 0.0,
                )
            )
    return tuple(mounts)


@dataclass(frozen=True)
class ModelConfiguration:
    opening_count: int = 8
    diameter_m: float = 0.0008
    pitch_m: float = 0.0028
    distance_m: float = 0.025
    spread_angle_deg: float = 20.0
    margin_m: float = 0.001
    offset_safety_m: float = 0.005
    drag_coefficient: float = 1.0
    mesh_coefficient: float = 0.47
    concentricity_coefficient: float = 1.0
    kappa: float = 1.4
    ambient_pa: float = 101300.0
    gravity_m_s2: float = 9.81
    uphill_deg: float = 2.2
    cross_slope_deg: float = 45.0
    contact_tolerance_m: float = 1e-6
    half_hit_lower: float = 0.25
    half_hit_upper: float = 0.75
    pressure_scale_a: float = DEFAULT_PRESSURE_SCALE_A
    pressure_scale_b_pa: float = DEFAULT_PRESSURE_SCALE_B_PA
    mounts: tuple[NozzleMount, ...] = field(default_factory=default_mounts)
    manual_axis: Axis | None = None
    manual_angle_deg: float = 90.0

    @property
    def center_span_m(self) -> float:
        """Distance from the first opening center to the last opening center."""
        return (self.opening_count - 1) * self.pitch_m

    @property
    def outer_width_m(self) -> float:
        return self.diameter_m + self.center_span_m

    @property
    def jet_radius_m(self) -> float:
        return (
            self.distance_m * math.tan(math.radians(self.spread_angle_deg) / 2)
            + self.diameter_m / 2
        )

    @property
    def gravity(self) -> np.ndarray:
        alpha, beta = math.radians(self.uphill_deg), math.radians(self.cross_slope_deg)
        return -self.gravity_m_s2 * np.array(
            [math.sin(alpha), math.cos(alpha) * math.sin(beta), math.cos(alpha) * math.cos(beta)]
        )

    def validate(self):
        validate_pressure_scaling(self.pressure_scale_a, self.pressure_scale_b_pa)
        positive = (
            self.diameter_m,
            self.distance_m,
            self.drag_coefficient,
            self.mesh_coefficient,
            self.concentricity_coefficient,
            self.ambient_pa,
            self.gravity_m_s2,
            self.contact_tolerance_m,
        )
        if any(not math.isfinite(v) or v <= 0 for v in positive):
            raise ValueError(
                "Durchmesser, Abstand, Koeffizienten, Druck und g müssen positiv sein."
            )
        if type(self.opening_count) is not int or not 1 <= self.opening_count <= 256:
            raise ValueError("Öffnungszahl muss zwischen 1 und 256 liegen.")
        if (
            not math.isfinite(self.pitch_m)
            or self.pitch_m < 0
            or self.opening_count > 1
            and self.pitch_m < self.diameter_m
        ):
            raise ValueError("Mittenabstand darf nicht kleiner als der Öffnungsdurchmesser sein.")
        if not math.isfinite(self.margin_m) or self.margin_m < 0:
            raise ValueError("Randabstand muss endlich und nichtnegativ sein.")
        if not math.isfinite(self.offset_safety_m) or self.offset_safety_m < 0:
            raise ValueError("Offsetreserve muss endlich und nichtnegativ sein.")
        validate_hit_thresholds(self.half_hit_lower, self.half_hit_upper)
        if not math.isfinite(self.spread_angle_deg) or not 0 <= self.spread_angle_deg < 180:
            raise ValueError("Strahlöffnungswinkel muss zwischen 0 und weniger als 180° liegen.")
        if not math.isfinite(self.kappa) or self.kappa <= 1:
            raise ValueError("Isentropenexponent κ muss größer als 1 sein.")
        if (
            not math.isfinite(self.uphill_deg)
            or abs(self.uphill_deg) >= 90
            or not math.isfinite(self.cross_slope_deg)
            or not 0 <= self.cross_slope_deg <= 90
        ):
            raise ValueError("Ungültige Anlagenneigung.")
        if self.manual_axis is not None and self.manual_axis not in ("x", "y", "z"):
            raise ValueError("Manuelle Kippachse muss X, Y oder Z sein.")
        if not math.isfinite(self.manual_angle_deg) or not 0 < abs(self.manual_angle_deg) <= 360:
            raise ValueError("Drehwinkel muss ungleich null und höchstens 360° sein.")
        keys = set()
        for mount in self.mounts:
            if type(mount.array_index) is not int or type(mount.nozzle_index) is not int:
                raise ValueError("Array- und Düsenindex müssen Ganzzahlen sein.")
            key = mount.array_index, mount.nozzle_index
            if (
                key in keys
                or mount.array_index not in range(1, 5)
                or mount.nozzle_index not in range(1, 7)
            ):
                raise ValueError("Doppelte oder ungültige Array-/Düsenkonfiguration.")
            keys.add(key)
            if mount.normal_axis not in ("y", "z") or mount.rotation_axis not in ("x", "y", "z"):
                raise ValueError("Ungültige Montageachse.")
            if mount.rotation_axis == mount.normal_axis:
                raise ValueError("Lochreihe/Kippachse muss quer zur Strahlrichtung liegen.")
            if mount.row_center_m is not None and (
                not math.isfinite(mount.row_center_m) or mount.row_center_m < 0
            ):
                raise ValueError("Reihenmitte muss endlich und nichtnegativ sein.")
            if mount.longitudinal_offset_m is not None and not math.isfinite(
                mount.longitudinal_offset_m
            ):
                raise ValueError("Längsversatz muss endlich sein.")
            if mount.nozzle_index in (1, 4) and mount.longitudinal_offset_m != 0:
                raise ValueError("Die Referenzdüse 1 beziehungsweise 4 hat Längsversatz 0.")


def nozzle_group(reference: NozzleMount, config: ModelConfiguration) -> tuple[NozzleMount, ...]:
    """Require the complete measured group before enabling automatic selection."""
    mounts = {m.nozzle_index: m for m in config.mounts if m.array_index == reference.array_index}
    mounts[reference.nozzle_index] = reference
    group = []
    missing = []
    for index in range(reference.nozzle_index, reference.nozzle_index + 3):
        mount = mounts.get(index)
        if mount is None:
            missing.append(f"Düse {index}: Montageposition fehlt")
            continue
        if (mount.rotation_axis, mount.normal_axis) != (
            reference.rotation_axis,
            reference.normal_axis,
        ):
            raise ValueError(
                f"Düse {index}: Achsen und Strahlrichtung müssen "
                "innerhalb der Gruppe übereinstimmen."
            )
        if mount.row_center_m is None:
            missing.append(f"Düse {index}: Reihenmitte fehlt")
        if reference.rotation_axis == "x" and mount.longitudinal_offset_m is None:
            missing.append(f"Düse {index}: Längsversatz fehlt")
        group.append(mount)
    if missing:
        raise ValueError("Montageposition der Dreiergruppe unvollständig: " + "; ".join(missing))
    return tuple(group)


@dataclass(frozen=True)
class Turn:
    axis: Axis
    signed_angle_deg: float

    @property
    def direction(self) -> np.ndarray:
        return np.eye(3)["xyz".index(self.axis)] * math.copysign(1, self.signed_angle_deg)


def resolve_turn(context: dict, config: ModelConfiguration) -> Turn:
    if (
        context.get("flip_count", 1) != 1
        or len(context.get("commands", [])) > 1
        or len(context.get("component_flips", [])) > 1
    ):
        raise ValueError("Mehrstufige Bewegungsfolgen werden noch nicht analytisch berechnet.")
    if context.get("transition_kind") == "passive":
        raise ValueError("Passiver Übergang benötigt keinen aktiven Druckvorschlag.")
    if context.get("kind") == "manual" and "rotation_axis" in context:
        from .trial_context import validate_context

        manual = validate_context(context)
        return Turn(manual["rotation_axis"], manual["signed_angle_deg"])
    if context.get("kind") == "manual" and config.manual_axis is not None:
        source = normalize_quaternion(context.get("source_quaternion_xyzw"))
        target = normalize_quaternion(context.get("target_quaternion_xyzw"))
        half = math.radians(config.manual_angle_deg) / 2
        command = [0.0, 0.0, 0.0, math.cos(half)]
        command["xyz".index(config.manual_axis)] = math.sin(half)
        expected = normalize_quaternion(multiply(command, source))
        if abs(np.dot(expected, target)) < 1 - 1e-6:
            raise ValueError("Manuelle Achse/Drehwinkel passen nicht zur bestätigten Zielpose.")
        return Turn(config.manual_axis, config.manual_angle_deg)
    commands = context.get("commands", [])
    if commands:
        axis, angle = commands[0].get("axis"), commands[0].get("signed_angle_deg")
    elif context.get("actuation") in ("free_x", "free_y", "free_z"):
        axis, angle = context["actuation"][-1], context.get("signed_angle_deg")
    else:
        axis, angle = None, None
    if axis is not None and angle is not None:
        if (
            axis not in ("x", "y", "z")
            or isinstance(angle, bool)
            or not isinstance(angle, (int, float))
            or not math.isfinite(angle)
            or not 0 < abs(angle) <= 360
        ):
            raise ValueError("Ungültige Kippachse oder Drehwinkel der Transition.")
        return Turn(axis, float(angle))
    source = normalize_quaternion(context.get("source_quaternion_xyzw"))
    target = normalize_quaternion(context.get("target_quaternion_xyzw"))
    relative = normalize_quaternion(
        multiply(target, (-source[0], -source[1], -source[2], source[3]))
    )
    vector = np.array(relative[:3])
    norm = float(np.linalg.norm(vector))
    if norm < 1e-8:
        raise ValueError("Keine eindeutige Drehung: manuelle Achse und Drehsinn bestätigen.")
    axis_index = int(np.argmax(np.abs(vector)))
    if np.linalg.norm(np.delete(vector / norm, axis_index)) > 1e-5 or abs(relative[3]) < 1e-7:
        raise ValueError("Achse/Drehsinn mehrdeutig: manuelle Achse und Drehwinkel bestätigen.")
    angle = math.degrees(2 * math.atan2(norm, relative[3])) * math.copysign(1, vector[axis_index])
    return Turn("xyz"[axis_index], angle)


def quaternion_matrix(quaternion) -> np.ndarray:
    x, y, z, w = normalize_quaternion(quaternion)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


@dataclass(frozen=True)
class OrientedBody:
    vertices: np.ndarray
    faces: np.ndarray
    center: np.ndarray
    inertia: np.ndarray
    mass_kg: float
    _silhouettes: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def silhouette(self, normal_axis: Axis):
        if normal_axis in self._silhouettes:
            return self._silhouettes[normal_axis]
        columns = [i for i in range(3) if i != "xyz".index(normal_axis)]
        triangles = self.vertices[self.faces][:, :, columns]
        first, second = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
        projected_area = first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]
        triangles = triangles[np.abs(projected_area) > 1e-18]
        if not len(triangles):
            raise ValueError("Keine projizierte Bauteilfläche verfügbar.")
        shape = union_all(polygons(triangles))
        if shape.is_empty or not shape.is_valid:
            raise ValueError("Projizierte Bauteilfläche ist ungültig.")
        self._silhouettes[normal_axis] = shape
        return shape


def orient_body(analysis: MeshAnalysis, context: dict) -> OrientedBody:
    if not analysis.geometry.valid_volume or analysis.material.mass_kg is None:
        raise ValueError("Geschlossenes Volumen und Dichte sind erforderlich.")
    if not len(analysis.vertices_m) or not len(analysis.faces):
        raise ValueError("Vollständige Mesh-Geometrie fehlt; STL erneut auswerten.")
    matrix = quaternion_matrix(context.get("source_quaternion_xyzw"))
    vertices = analysis.vertices_m @ matrix.T
    shift = np.array([0, -vertices[:, 1].min(), -vertices[:, 2].min()])
    vertices += shift
    center = matrix @ np.asarray(analysis.geometry.center_m) + shift
    inertia = matrix @ np.asarray(analysis.material.inertia_kg_m2) @ matrix.T
    return OrientedBody(vertices, analysis.faces, center, inertia, analysis.material.mass_kg)


@dataclass(frozen=True)
class TippingMechanics:
    pivot_m: Vector
    edge_m: tuple[Vector, Vector]
    inertia_axis_kg_m2: float
    delta_h_m: float
    tipping_angle_deg: float


def tipping_mechanics(
    body: OrientedBody, turn: Turn, mount: NozzleMount, config: ModelConfiguration
) -> TippingMechanics:
    """Find the pivot, energy barrier and inertia for the requested rotation."""
    pivot, edge_end = _contact_edge(body, turn, mount, config.contact_tolerance_m)
    axis = turn.direction
    center_offset = body.center - pivot
    perpendicular = center_offset - axis * np.dot(axis, center_offset)
    tangent = np.cross(axis, center_offset)
    up = -config.gravity / config.gravity_m_s2
    vertical_component = float(np.dot(up, perpendicular))
    tangential_component = float(np.dot(up, tangent))
    if tangential_component <= EPS:
        raise ValueError("Keine positive anfängliche Energiebarriere in dieser Kipprichtung.")

    tipping_angle = math.atan2(tangential_component, vertical_component) % (2 * math.pi)
    if tipping_angle > math.radians(abs(turn.signed_angle_deg)) + 1e-7:
        raise ValueError("Die vorgesehene Drehung erreicht den Kipppunkt an dieser Kante nicht.")
    height_gain = vertical_component * (
        math.cos(tipping_angle) - 1
    ) + tangential_component * math.sin(tipping_angle)
    if height_gain <= EPS:
        raise ValueError("Keine positive potentielle Energiebarriere vorhanden.")
    _check_support_clearance(body.vertices, pivot, axis, tipping_angle, config.contact_tolerance_m)

    inertia = float(
        axis @ body.inertia @ axis + body.mass_kg * np.dot(perpendicular, perpendicular)
    )
    if not math.isfinite(inertia) or inertia <= 0:
        raise ValueError("Ungültiges Trägheitsmoment um die Kippkante.")
    return TippingMechanics(
        pivot_m=_vector(pivot),
        edge_m=(_vector(pivot), _vector(edge_end)),
        inertia_axis_kg_m2=inertia,
        delta_h_m=height_gain,
        tipping_angle_deg=math.degrees(tipping_angle),
    )


def _contact_edge(body, turn, mount, tolerance):
    """Select the straight contact edge on the side required by the turn."""
    axis_index, normal_index = "xyz".index(turn.axis), "xyz".index(mount.normal_axis)
    transverse = next(i for i in range(3) if i not in (axis_index, normal_index))
    normal = np.eye(3)[normal_index]
    axis = turn.direction
    contact = body.vertices[np.abs(body.vertices[:, normal_index]) <= tolerance]
    coefficient = float(np.dot(np.cross(np.eye(3)[transverse], normal), axis))
    bound = contact[:, transverse].min() if coefficient > 0 else contact[:, transverse].max()
    edge = contact[np.abs(contact[:, transverse] - bound) <= tolerance]
    lo, hi = edge[:, axis_index].min(), edge[:, axis_index].max()
    if hi - lo <= tolerance:
        raise ValueError("Keine eindeutige geradlinige Kontakt-/Kippkante entlang der Kippachse.")
    pivot = np.zeros(3)
    pivot[transverse], pivot[axis_index] = bound, lo
    end = pivot.copy()
    end[axis_index] = hi
    return pivot, end


def _check_support_clearance(vertices, pivot, axis, tipping_angle, tolerance):
    """Check each vertex's exact lowest Y/Z position up to the energy barrier."""
    relative = vertices - pivot
    parallel = np.outer(relative @ axis, axis)
    cosine = relative - parallel
    sine = np.cross(axis, relative)
    constant = pivot + parallel
    for coord in (1, 2):
        minima = np.minimum(
            vertices[:, coord],
            constant[:, coord]
            + cosine[:, coord] * math.cos(tipping_angle)
            + sine[:, coord] * math.sin(tipping_angle),
        )
        extrema = (np.arctan2(sine[:, coord], cosine[:, coord]) + math.pi) % (2 * math.pi)
        inside = extrema <= tipping_angle
        minima[inside] = np.minimum(
            minima[inside],
            constant[inside, coord] - np.hypot(cosine[inside, coord], sine[inside, coord]),
        )
        if np.min(minima) < -tolerance:
            raise ValueError("Kippbewegung durchdringt vor dem Kipppunkt eine Auflagefläche.")


@dataclass(frozen=True)
class ImpactSolution:
    offset_m: float
    weights: tuple[float, ...]
    points_m: tuple[Vector, ...]
    lever_arm_m: float
    area_eff_m2: float
    jet_radius_m: float
    center_offsets_m: tuple[Vector, ...]
    com_offsets_m: tuple[Vector, ...]
    silhouette_rings: tuple[tuple[tuple[float, float], ...], ...]
    full_weights: tuple[float, ...] = ()
    coverage_fractions: tuple[float, ...] = ()
    active_nozzles: tuple[int, ...] = ()
    opening_ids: tuple[tuple[int, int], ...] = ()
    geometric_weights: tuple[float, ...] = ()
    lever_arms_m: tuple[float, ...] = ()
    offset_safety_m: float = 0.0


def jet_overlap_fractions(shape, centers, radius_m: float) -> tuple[float, ...]:
    """Area coverage of each circular jet, including concavities and holes.

    Partial intersections use a 512-sided circle. Dividing by that same polygon's
    area preserves exact half coverage at a straight edge through the jet center.
    """
    if not math.isfinite(radius_m) or radius_m <= 0:
        raise ValueError("Strahlradius muss positiv und endlich sein.")
    boundary = shape.boundary
    fractions = []
    for x, y in centers:
        point = Point(x, y)
        if shape.is_empty or shape.distance(point) >= radius_m:
            fractions.append(0.0)
        elif shape.covers(point) and boundary.distance(point) >= radius_m:
            fractions.append(1.0)
        else:
            circle = point.buffer(radius_m, quad_segs=128)
            fractions.append(min(1.0, max(0.0, shape.intersection(circle).area / circle.area)))
    return tuple(fractions)


def validate_hit_thresholds(lower, upper):
    if (
        not math.isfinite(lower)
        or not math.isfinite(upper)
        or not 0 < lower <= 0.5 <= upper < 1
        or lower >= upper
    ):
        raise ValueError(
            "Treffergrenzen müssen 0 < Untergrenze ≤ 50 % ≤ Obergrenze < 100 % erfüllen."
        )


def coverage_weights(fractions, lower=0.25, upper=0.75) -> tuple[float, ...]:
    validate_hit_thresholds(lower, upper)
    weights = []
    for fraction in fractions:
        if fraction < lower:
            weights.append(0.0)
        elif fraction < upper:
            weights.append(0.5)
        else:
            weights.append(1.0)
    return tuple(weights)


def jet_hit_weights(
    shape, centers, radius_m: float, *, half_lower=0.25, half_upper=0.75
) -> tuple[float, ...]:
    return coverage_weights(jet_overlap_fractions(shape, centers, radius_m), half_lower, half_upper)


def _intervals(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [(geometry.x, geometry.x)]
    if geometry.geom_type in ("LineString", "LinearRing"):
        xs = [p[0] for p in geometry.coords]
        return [(min(xs), max(xs))]
    return [interval for g in geometry.geoms for interval in _intervals(g)]


def _rings(shape):
    polygons = [shape] if shape.geom_type == "Polygon" else list(shape.geoms)
    return tuple(
        tuple((float(x), float(y)) for x, y in ring.coords)
        for polygon in polygons
        for ring in (polygon.exterior, *polygon.interiors)
    )


class OffsetEstimator(Protocol):
    def estimate(
        self,
        body: OrientedBody,
        turn: Turn,
        mount: NozzleMount,
        mechanics: TippingMechanics,
        config: ModelConfiguration,
    ) -> ImpactSolution: ...


class AutomaticOffsetEstimator:
    def estimate(self, body, turn, mount, mechanics, config):
        """Place the nozzle row, select an offset and report its jet contributions."""
        group = nozzle_group(mount, config)
        radius = config.jet_radius_m
        clearance = radius + config.margin_m
        shape = body.silhouette(mount.normal_axis)
        # A tiny conservative allowance also bounds GEOS's polygonal arc approximation.
        safe_area = shape.buffer(-clearance / math.cos(math.pi / 512), quad_segs=128)
        if safe_area.is_empty:
            raise ValueError(
                "Kein vollständiger Strahltreffer mit dem eingestellten Randabstand möglich."
            )
        row = (np.arange(config.opening_count) - (config.opening_count - 1) / 2) * config.pitch_m
        along_x = turn.axis == "x"
        transverse = 2 if mount.normal_axis == "y" else 1
        locations = np.concatenate(
            [
                np.full(config.opening_count, m.row_center_m) if along_x else m.row_center_m + row
                for m in group
            ]
        )
        front = float(body.vertices[:, 0].max())
        # Never rebase to the first active nozzle: nozzle 1/4 remains the timing reference.
        bases = np.concatenate(
            [
                front
                + radius
                + (
                    m.longitudinal_offset_m + row - row.min()
                    if along_x
                    else np.zeros(config.opening_count)
                )
                for m in group
            ]
        )
        opening_ids = tuple(
            (m.nozzle_index, opening + 1) for m in group for opening in range(config.opening_count)
        )
        intervals = _safe_x_intervals(safe_area, locations, shape.bounds, config.offset_safety_m)
        boundaries = _offset_boundaries(bases, intervals)
        if not boundaries:
            raise ValueError(
                "Keine Öffnung trifft bei dieser Reihenlage vollständig mit Randabstand "
                "und Offsetreserve auf das Bauteil."
            )
        axis, normal = turn.direction, np.eye(3)["xyz".index(mount.normal_axis)]
        pivot = np.array(mechanics.pivot_m)
        boundary = shape.boundary
        center = (body.vertices.min(axis=0) + body.vertices.max(axis=0)) / 2
        rings = _rings(shape)

        @lru_cache(maxsize=None)
        def evaluate(offset):
            points = np.zeros((len(opening_ids), 3))
            points[:, 0], points[:, transverse] = bases - offset, locations
            centers = tuple(zip(points[:, 0], locations, strict=True))
            coverage = jet_overlap_fractions(shape, centers, radius)
            geometric_weights = coverage_weights(
                coverage, config.half_hit_lower, config.half_hit_upper
            )
            full_weights = []
            for (x, y), allowed in zip(centers, intervals, strict=True):
                point = Point(x, y)
                full_hit = shape.covers(point) and boundary.distance(point) >= clearance - EPS
                if config.offset_safety_m > 0:
                    full_hit = full_hit and any(lo - EPS <= x <= hi + EPS for lo, hi in allowed)
                full_weights.append(float(full_hit))
            active_nozzles = tuple(
                m.nozzle_index
                for i, m in enumerate(group)
                if any(full_weights[i * config.opening_count : (i + 1) * config.opening_count])
            )
            weights = tuple(
                weight if nozzle in active_nozzles else 0.0
                for (nozzle, _), weight in zip(opening_ids, geometric_weights, strict=True)
            )
            levers = np.cross(points - pivot, normal) @ axis
            weight_sum = sum(weights)
            contributing_levers = levers[np.asarray(weights) > 0]
            if not weight_sum:
                lever = 0.0
            elif np.all(contributing_levers == contributing_levers[0]):
                lever = float(contributing_levers[0])
            else:
                lever = float(np.dot(weights, levers) / weight_sum)
            return ImpactSolution(
                offset_m=float(offset),
                weights=weights,
                points_m=tuple(_vector(point) for point in points),
                lever_arm_m=lever,
                area_eff_m2=sum(weights) * math.pi * radius**2,
                jet_radius_m=radius,
                center_offsets_m=tuple(_vector(point) for point in points - center),
                com_offsets_m=tuple(_vector(point) for point in points - body.center),
                silhouette_rings=rings,
                full_weights=tuple(full_weights),
                coverage_fractions=coverage,
                active_nozzles=active_nozzles,
                opening_ids=opening_ids,
                geometric_weights=geometric_weights,
                lever_arms_m=tuple(map(float, levers)),
                offset_safety_m=config.offset_safety_m,
            )

        if along_x:
            offset = _centered_offset(boundaries, evaluate)
        else:
            offset = _outermost_offset(boundaries, evaluate)
        impact = evaluate(offset)
        if impact.lever_arm_m <= EPS or not any(impact.weights):
            raise ValueError("Strahlen erzeugen kein positives Kippmoment in Zielrichtung.")
        return impact


def _safe_x_intervals(safe_area, locations, bounds, reserve):
    """Keep each full jet inside its own surface interval for +/- X uncertainty."""
    xmin, _, xmax, _ = bounds
    intervals = []
    for location in locations:
        row_line = LineString([(xmin - 1, location), (xmax + 1, location)])
        intervals.append(
            tuple(
                (start + reserve, end - reserve)
                for start, end in _intervals(safe_area.intersection(row_line))
                if end - start >= 2 * reserve
            )
        )
    return intervals


def _offset_boundaries(bases, intervals):
    """Convert each opening's allowed X intervals to nonnegative belt offsets."""
    boundaries = set()
    for base, allowed in zip(bases, intervals, strict=True):
        for x_start, x_end in allowed:
            offset_start = max(0.0, base - x_end)
            offset_end = base - x_start
            if offset_end >= 0:
                boundaries.update((offset_start, offset_end))
    return sorted(boundaries)


def _centered_offset(boundaries, evaluate):
    """For X turns, center the longest region with the most full hits."""
    regions = []
    for start, end in pairwise(boundaries):
        hits = sum(evaluate((start + end) / 2).full_weights)
        regions.append((start, end, hits))
    for offset in boundaries:
        regions.append((offset, offset, sum(evaluate(offset).full_weights)))
    best_hits = max(hits for _, _, hits in regions)
    if best_hits == 0:
        raise ValueError("Keine vollständigen Strahltreffer vorhanden.")

    # Adjacent optimal regions belong to the same continuous offset interval.
    merged = []
    for start, end, hits in sorted(regions):
        if hits != best_hits:
            continue
        if merged and start <= merged[-1][1] + EPS:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    start, end = max(merged, key=lambda span: (round(span[1] - span[0], 12), -span[0]))
    return (start + end) / 2


def _outermost_offset(boundaries, evaluate):
    """For Y/Z turns, enable all usable rows before maximizing their leverage."""
    choices = boundaries + [(start + end) / 2 for start, end in pairwise(boundaries)]
    best_offset, best_score = None, None
    for offset in choices:
        impact = evaluate(offset)
        if sum(impact.full_weights) == 0 or impact.lever_arm_m <= EPS:
            continue
        # A slightly slanted edge must not sacrifice two usable nozzle rows for
        # a microscopic gain in lever arm. The full-hit requirement stays intact.
        score = (len(impact.active_nozzles), impact.lever_arm_m, sum(impact.weights), -offset)
        if best_score is None or score > best_score:
            best_offset, best_score = offset, score
    if best_offset is None:
        raise ValueError("Kein vollständiger Treffer mit positivem Hebelarm in Zielrichtung.")
    return best_offset


@dataclass(frozen=True)
class PressurePrediction:
    force_n: float
    absolute_pa: float
    gauge_pa: float
    critical_gauge_pa: float
    warnings: tuple[str, ...]


def drag_factor(area_m2: float, config: ModelConfiguration) -> float:
    config.validate()
    factor = (
        config.drag_coefficient
        * config.mesh_coefficient
        * config.concentricity_coefficient
        * area_m2
        * config.kappa
        / (config.kappa - 1)
        * config.ambient_pa
        * (config.diameter_m / (2 * config.jet_radius_m)) ** 2
    )
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError(
            "Wirksame Strahlfläche und Strömungsfaktor müssen positiv und endlich sein."
        )
    return factor


def drag_force(gauge_pa: float, area_m2: float, config: ModelConfiguration) -> float:
    if not math.isfinite(gauge_pa) or gauge_pa < 0:
        raise ValueError("Überdruck muss endlich und nichtnegativ sein.")
    return drag_factor(area_m2, config) * math.expm1(
        (config.kappa - 1) / config.kappa * math.log1p(gauge_pa / config.ambient_pa)
    )


class PressureModel(Protocol):
    def predict(
        self,
        body: OrientedBody,
        mechanics: TippingMechanics,
        impact: ImpactSolution,
        pulse_ms: int,
        config: ModelConfiguration,
    ) -> PressurePrediction: ...


class AnalyticalPressureModel:
    def predict(self, body, mechanics, impact, pulse_ms, config):
        """Compute tipping force, invert the drag equation and report model limits."""
        config.validate()
        if any(
            not math.isfinite(v) or v <= 0
            for v in (body.mass_kg, mechanics.inertia_axis_kg_m2, mechanics.delta_h_m)
        ):
            raise ValueError(
                "Masse, Trägheitsmoment und Energiebarriere müssen positiv und endlich sein."
            )
        if not math.isfinite(pulse_ms) or pulse_ms <= 0 or impact.lever_arm_m <= 0:
            raise ValueError("Impulsdauer und Hebelarm müssen positiv sein.")
        pulse_seconds = pulse_ms / 1000
        force_n = math.sqrt(
            2
            * mechanics.inertia_axis_kg_m2
            * body.mass_kg
            * config.gravity_m_s2
            * mechanics.delta_h_m
        ) / (pulse_seconds * impact.lever_arm_m)
        factor = drag_factor(impact.area_eff_m2, config)
        pressure_exponent = config.kappa / (config.kappa - 1)
        try:
            # log1p/expm1 keep equation (7) accurate for small pressure differences.
            gauge_pa = config.ambient_pa * math.expm1(
                pressure_exponent * math.log1p(force_n / factor)
            )
            critical_pa = config.ambient_pa * math.expm1(
                pressure_exponent * math.log((config.kappa + 1) / 2)
            )
        except OverflowError as exc:
            raise ValueError(
                "Berechneter Druck liegt außerhalb des endlichen Zahlenbereichs."
            ) from exc
        if not all(math.isfinite(v) for v in (force_n, gauge_pa, critical_pa)):
            raise ValueError("Druckberechnung liefert keine endlichen Werte.")
        warnings = []
        if not 5000 <= gauge_pa <= 80000:
            warnings.append(
                "Außerhalb des Referenzbereichs des Ausgangsmodells (0,05–0,80 bar Überdruck)"
            )
        if gauge_pa >= critical_pa:
            warnings.append(
                "Nicht abgeschnürte Strömung nicht erfüllt; mathematische Extrapolation"
            )
        if not 0 <= impact.offset_m * 1000 <= 5000:
            warnings.append("Außerhalb der SPS-Offsetgrenzen 0–5000 mm")
        return PressurePrediction(
            force_n=force_n,
            absolute_pa=config.ambient_pa + gauge_pa,
            gauge_pa=gauge_pa,
            critical_gauge_pa=critical_pa,
            warnings=tuple(warnings),
        )


@dataclass(frozen=True)
class CandidateResult:
    mount: NozzleMount
    turn: Turn
    pulse_ms: int
    status: Literal["ready", "outside_model", "blocked", "pending"]
    message: str
    mechanics: TippingMechanics | None = None
    impact: ImpactSolution | None = None
    pressure: PressurePrediction | None = None
    center_m: Vector | None = None
    inertia_cm_kg_m2: Tensor | None = None
    sps_pressure: SpsPressurePrediction | None = None

    @property
    def key(self) -> CandidateKey:
        return self.mount.array_index, self.mount.nozzle_index

    @property
    def nozzle_label(self) -> str:
        active = self.impact.active_nozzles if self.impact else ()
        if not active:
            return f"Gruppe {self.mount.nozzle_index}–{self.mount.nozzle_index + 2}"
        return ("Düse " if len(active) == 1 else "Düsen ") + "+".join(map(str, active))


class AnalyticalModel:
    def __init__(
        self, offset: OffsetEstimator | None = None, pressure: PressureModel | None = None
    ):
        self.offset = offset or AutomaticOffsetEstimator()
        self.pressure = pressure or AnalyticalPressureModel()

    def evaluate(self, analysis, context, parameters, config, *, calculate=True):
        """For each matching mount: mechanics → jet placement → pressure proposal."""
        config.validate()
        turn = resolve_turn(context, config)
        pulses = {a["index"]: a["pulse_duration_ms"] for a in parameters["arrays"]}
        mounts = [
            m for m in config.mounts if m.nozzle_index in (1, 4) and m.rotation_axis == turn.axis
        ]
        if not mounts:
            raise ValueError(
                "Keine passende Array-/Düsenkombination für diese Kippachse konfiguriert."
            )
        body = None
        results = []
        for mount in mounts:
            pulse_ms = pulses[mount.array_index]
            try:
                nozzle_group(mount, config)
                if not calculate:
                    results.append(
                        CandidateResult(
                            mount=mount,
                            turn=turn,
                            pulse_ms=pulse_ms,
                            status="pending",
                            message="Berechnung starten",
                        )
                    )
                    continue
                if body is None:
                    body = orient_body(analysis, context)
                mechanics = tipping_mechanics(body, turn, mount, config)
                impact = self.offset.estimate(body, turn, mount, mechanics, config)
                pressure = self.pressure.predict(body, mechanics, impact, pulse_ms, config)
                sps_pressure = None
                warnings = pressure.warnings
                try:
                    sps_pressure = scale_pressure(
                        pressure.gauge_pa, config.pressure_scale_a, config.pressure_scale_b_pa
                    )
                    warnings += sps_pressure.warnings
                except ValueError as exc:
                    # Keep the completed mechanics/analytical result visible on scaling failure.
                    warnings += (str(exc),)
                results.append(
                    CandidateResult(
                        mount=mount,
                        turn=turn,
                        pulse_ms=pulse_ms,
                        status="blocked"
                        if sps_pressure is None
                        else "outside_model"
                        if warnings
                        else "ready",
                        message="; ".join(warnings) or "Berechnet innerhalb des Modellbereichs",
                        mechanics=mechanics,
                        impact=impact,
                        pressure=pressure,
                        sps_pressure=sps_pressure,
                        center_m=_vector(body.center),
                        inertia_cm_kg_m2=_tensor(body.inertia),
                    )
                )
            except ValueError as exc:
                results.append(
                    CandidateResult(
                        mount=mount,
                        turn=turn,
                        pulse_ms=pulse_ms,
                        status="blocked",
                        message=str(exc),
                    )
                )
        return tuple(results)
