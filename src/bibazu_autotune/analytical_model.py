"""Static tipping impulse and additive jets. All physical calculations use SI units.

The supplied drag equation assumes independent jets at common stagnation pressure.
Neither motion during the impulse nor contact/friction losses are simulated here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal, Protocol

import numpy as np
from shapely import LineString, Point, polygons, union_all

from .geometry import MeshAnalysis, Tensor, Vector
from .orientation_animation import multiply
from .trial_context import normalize_quaternion

Axis = Literal["x", "y", "z"]
EPS = 1e-10


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


def default_mounts() -> tuple[NozzleMount, ...]:
    return tuple(
        NozzleMount(i, n, "x" if n == 4 else "z" if i % 2 else "y", "y" if i % 2 else "z")
        for i in range(1, 5)
        for n in (1, 4)
    )


@dataclass(frozen=True)
class ModelConfiguration:
    opening_count: int = 8
    diameter_m: float = 0.0007
    pitch_m: float = (0.024 - 0.0007) / 7
    distance_m: float = 0.025
    spread_angle_deg: float = 20.0
    margin_m: float = 0.001
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
    mounts: tuple[NozzleMount, ...] = field(default_factory=default_mounts)
    manual_axis: Axis | None = None
    manual_angle_deg: float = 90.0

    @property
    def outer_width_m(self) -> float:
        return self.diameter_m + (self.opening_count - 1) * self.pitch_m

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
            key = mount.array_index, mount.nozzle_index
            if (
                key in keys
                or mount.array_index not in range(1, 5)
                or mount.nozzle_index not in (1, 4)
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
    axis_index, normal_index = "xyz".index(turn.axis), "xyz".index(mount.normal_axis)
    transverse = next(i for i in range(3) if i not in (axis_index, normal_index))
    normal = np.eye(3)[normal_index]
    axis = turn.direction
    contact = body.vertices[np.abs(body.vertices[:, normal_index]) <= config.contact_tolerance_m]
    coefficient = float(np.dot(np.cross(np.eye(3)[transverse], normal), axis))
    bound = contact[:, transverse].min() if coefficient > 0 else contact[:, transverse].max()
    edge = contact[np.abs(contact[:, transverse] - bound) <= config.contact_tolerance_m]
    lo, hi = edge[:, axis_index].min(), edge[:, axis_index].max()
    if hi - lo <= config.contact_tolerance_m:
        raise ValueError("Keine eindeutige geradlinige Kontakt-/Kippkante entlang der Kippachse.")
    pivot = np.zeros(3)
    pivot[transverse], pivot[axis_index] = bound, lo
    end = pivot.copy()
    end[axis_index] = hi
    r = body.center - pivot
    perpendicular = r - axis * np.dot(axis, r)
    tangent = np.cross(axis, r)
    up = -config.gravity / config.gravity_m_s2
    a, b = float(np.dot(up, perpendicular)), float(np.dot(up, tangent))
    if b <= EPS:
        raise ValueError("Keine positive anfängliche Energiebarriere in dieser Kipprichtung.")
    theta = math.atan2(b, a) % (2 * math.pi)
    if theta > math.radians(abs(turn.signed_angle_deg)) + 1e-7:
        raise ValueError("Die vorgesehene Drehung erreicht den Kipppunkt an dieser Kante nicht.")
    delta_h = a * (math.cos(theta) - 1) + b * math.sin(theta)
    if delta_h <= EPS:
        raise ValueError("Keine positive potentielle Energiebarriere vorhanden.")
    # Check exact minima of every vertex's sinusoidal Y/Z trajectory up to the barrier.
    relative = body.vertices - pivot
    parallel = np.outer(relative @ axis, axis)
    cosine = relative - parallel
    sine = np.cross(axis, relative)
    constant = pivot + parallel
    for coord in (1, 2):
        minima = np.minimum(
            body.vertices[:, coord],
            constant[:, coord]
            + cosine[:, coord] * math.cos(theta)
            + sine[:, coord] * math.sin(theta),
        )
        extrema = (np.arctan2(sine[:, coord], cosine[:, coord]) + math.pi) % (2 * math.pi)
        inside = extrema <= theta
        minima[inside] = np.minimum(
            minima[inside],
            constant[inside, coord] - np.hypot(cosine[inside, coord], sine[inside, coord]),
        )
        if np.min(minima) < -config.contact_tolerance_m:
            raise ValueError("Kippbewegung durchdringt vor dem Kipppunkt eine Auflagefläche.")
    inertia = float(
        axis @ body.inertia @ axis + body.mass_kg * np.dot(perpendicular, perpendicular)
    )
    if not math.isfinite(inertia) or inertia <= 0:
        raise ValueError("Ungültiges Trägheitsmoment um die Kippkante.")
    return TippingMechanics(
        _vector(pivot), (_vector(pivot), _vector(end)), inertia, delta_h, math.degrees(theta)
    )


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
    return tuple(0.0 if f < lower else 0.5 if f < upper else 1.0 for f in fractions)


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
        if mount.row_center_m is None:
            raise ValueError("Montageposition der Reihenmitte relativ zur Rinnenkante fehlt.")
        radius = config.jet_radius_m
        clearance = radius + config.margin_m
        shape = body.silhouette(mount.normal_axis)
        # A tiny conservative allowance also bounds GEOS's polygonal arc approximation.
        safe = shape.buffer(-clearance / math.cos(math.pi / 512), quad_segs=128)
        if safe.is_empty:
            raise ValueError(
                "Kein vollständiger Strahltreffer mit dem eingestellten Randabstand möglich."
            )
        row = (np.arange(config.opening_count) - (config.opening_count - 1) / 2) * config.pitch_m
        along_x = turn.axis == "x"
        transverse = 2 if mount.normal_axis == "y" else 1
        locations = (
            np.full(config.opening_count, mount.row_center_m)
            if along_x
            else mount.row_center_m + row
        )
        front = float(body.vertices[:, 0].max())
        bases = front + radius + (row - row.min() if along_x else np.zeros(config.opening_count))
        domains = []
        xmin, _, xmax, _ = shape.bounds
        for base, location in zip(bases, locations, strict=True):
            cuts = safe.intersection(LineString([(xmin - 1, location), (xmax + 1, location)]))
            domains.append(
                [(max(0.0, base - hi), base - lo) for lo, hi in _intervals(cuts) if base - lo >= 0]
            )
        boundaries = sorted(
            {v for intervals in domains for interval in intervals for v in interval}
        )
        if not boundaries:
            raise ValueError(
                "Keine Öffnung trifft bei dieser Reihenlage vollständig auf das Bauteil."
            )
        axis, normal = turn.direction, np.eye(3)["xyz".index(mount.normal_axis)]
        pivot = np.array(mechanics.pivot_m)
        boundary = shape.boundary

        @lru_cache(maxsize=None)
        def evaluate(offset):
            points = np.zeros((config.opening_count, 3))
            points[:, 0], points[:, transverse] = bases - offset, locations
            centers = tuple(zip(points[:, 0], locations, strict=True))
            coverage = jet_overlap_fractions(shape, centers, radius)
            weights = coverage_weights(coverage, config.half_hit_lower, config.half_hit_upper)
            full_weights = tuple(
                float(
                    shape.covers(point := Point(x, y))
                    and boundary.distance(point) >= clearance - EPS
                )
                for x, y in centers
            )
            lever = float(np.dot(np.cross(points[0] - pivot, normal), axis))
            return points, weights, lever, full_weights, coverage

        spans = [(lo, hi) for lo, hi in zip(boundaries[:-1], boundaries[1:], strict=True)]
        if along_x:
            regions = [(lo, hi, sum(evaluate((lo + hi) / 2)[3])) for lo, hi in spans]
            regions += [(v, v, sum(evaluate(v)[3])) for v in boundaries]
            best = max(n for _, _, n in regions)
            if best == 0:
                raise ValueError("Keine vollständigen Strahltreffer vorhanden.")
            merged = []
            for lo, hi, _ in sorted(r for r in regions if r[2] == best):
                if merged and lo <= merged[-1][1] + EPS:
                    merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
                else:
                    merged.append((lo, hi))
            lo, hi = max(merged, key=lambda span: (round(span[1] - span[0], 12), -span[0]))
            offset = (lo + hi) / 2
        else:
            choices = boundaries + [(lo + hi) / 2 for lo, hi in spans]
            viable = [
                (evaluate(v)[2], sum(evaluate(v)[1]), -v, v)
                for v in choices
                if sum(evaluate(v)[3]) > 0 and evaluate(v)[2] > EPS
            ]
            if not viable:
                raise ValueError(
                    "Kein vollständiger Treffer mit positivem Hebelarm in Zielrichtung."
                )
            offset = max(viable)[-1]
        points, weights, lever, full_weights, coverage = evaluate(offset)
        if lever <= EPS or not any(weights):
            raise ValueError("Strahlen erzeugen kein positives Kippmoment in Zielrichtung.")
        center = (body.vertices.min(axis=0) + body.vertices.max(axis=0)) / 2
        return ImpactSolution(
            float(offset),
            weights,
            tuple(_vector(point) for point in points),
            lever,
            sum(weights) * math.pi * radius**2,
            radius,
            tuple(_vector(point) for point in points - center),
            tuple(_vector(point) for point in points - body.center),
            _rings(shape),
            full_weights,
            coverage,
        )


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
        force = math.sqrt(
            2
            * mechanics.inertia_axis_kg_m2
            * body.mass_kg
            * config.gravity_m_s2
            * mechanics.delta_h_m
        ) / (pulse_ms / 1000 * impact.lever_arm_m)
        factor = drag_factor(impact.area_eff_m2, config)
        try:
            gauge = config.ambient_pa * math.expm1(
                config.kappa / (config.kappa - 1) * math.log1p(force / factor)
            )
            critical = config.ambient_pa * math.expm1(
                config.kappa / (config.kappa - 1) * math.log((config.kappa + 1) / 2)
            )
        except OverflowError as exc:
            raise ValueError(
                "Berechneter Druck liegt außerhalb des endlichen Zahlenbereichs."
            ) from exc
        if not all(math.isfinite(v) for v in (force, gauge, critical)):
            raise ValueError("Druckberechnung liefert keine endlichen Werte.")
        warnings = []
        if not 5000 <= gauge <= 80000:
            warnings.append(
                "Außerhalb des Referenzbereichs des Ausgangsmodells (0,05–0,80 bar Überdruck)"
            )
        if gauge >= critical:
            warnings.append(
                "Nicht abgeschnürte Strömung nicht erfüllt; mathematische Extrapolation"
            )
        if not 0 <= gauge / 100 <= 6000:
            warnings.append("Außerhalb der SPS-Druckgrenzen 0–6000 mbar")
        if not 0 <= impact.offset_m * 1000 <= 5000:
            warnings.append("Außerhalb der SPS-Offsetgrenzen 0–5000 mm")
        return PressurePrediction(
            force, config.ambient_pa + gauge, gauge, critical, tuple(warnings)
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


class AnalyticalModel:
    def __init__(
        self, offset: OffsetEstimator | None = None, pressure: PressureModel | None = None
    ):
        self.offset = offset or AutomaticOffsetEstimator()
        self.pressure = pressure or AnalyticalPressureModel()

    def evaluate(self, analysis, context, parameters, config, *, calculate=True):
        config.validate()
        turn = resolve_turn(context, config)
        pulses = {a["index"]: a["pulse_duration_ms"] for a in parameters["arrays"]}
        mounts = [m for m in config.mounts if m.rotation_axis == turn.axis]
        if not mounts:
            raise ValueError(
                "Keine passende Array-/Düsenkombination für diese Kippachse konfiguriert."
            )
        body = None
        results = []
        for mount in mounts:
            try:
                if mount.row_center_m is None:
                    raise ValueError(
                        "Montageposition der Reihenmitte relativ zur Rinnenkante fehlt."
                    )
                if not calculate:
                    results.append(
                        CandidateResult(
                            mount, turn, pulses[mount.array_index], "pending", "Berechnung starten"
                        )
                    )
                    continue
                if body is None:
                    body = orient_body(analysis, context)
                mechanics = tipping_mechanics(body, turn, mount, config)
                impact = self.offset.estimate(body, turn, mount, mechanics, config)
                pressure = self.pressure.predict(
                    body, mechanics, impact, pulses[mount.array_index], config
                )
                results.append(
                    CandidateResult(
                        mount,
                        turn,
                        pulses[mount.array_index],
                        "outside_model" if pressure.warnings else "ready",
                        "; ".join(pressure.warnings) or "Berechnet innerhalb des Modellbereichs",
                        mechanics,
                        impact,
                        pressure,
                        _vector(body.center),
                        _tensor(body.inertia),
                    )
                )
            except ValueError as exc:
                results.append(
                    CandidateResult(mount, turn, pulses[mount.array_index], "blocked", str(exc))
                )
        return tuple(results)
