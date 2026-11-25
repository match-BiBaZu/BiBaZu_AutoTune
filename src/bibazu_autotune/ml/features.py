"""Versioned inputs that are available before a flip is performed."""

from dataclasses import dataclass
from math import isfinite
from types import SimpleNamespace

from ..analytical_model import AnalyticalPressureModel, ModelConfiguration, NozzleMount
from ..candidate_transfer import candidate_setup_parameters
from ..recipes import LIMITS

FEATURE_VERSION = "physics-v1"
FEATURES = (
    ("pressure_mbar", "mbar SPS"),
    ("pulse_duration_ms", "ms"),
    ("analytical_gauge_mbar", "mbar Überdruck"),
    ("mass_kg", "kg"),
    ("inertia_axis_kg_m2", "kg m²"),
    ("delta_h_m", "m"),
    ("lever_arm_m", "m"),
    ("area_eff_m2", "m²"),
    ("target_angle_deg", "°"),
    ("offset_mm", "mm"),
    ("conveyor_speed_mm_per_sec", "mm/s"),
    ("delay_ms", "ms"),
)


def finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"Ungültiges oder fehlendes Merkmal: {name}.")
    return float(value)


def setpoint(value, name):
    low, high, _ = LIMITS[name]
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name}: ganzzahliger SPS-Wert von {low} bis {high} erforderlich.")
    return value


@dataclass(frozen=True)
class FeatureContext:
    configuration: ModelConfiguration
    mass_kg: float
    inertia_axis_kg_m2: float
    delta_h_m: float
    lever_arm_m: float
    area_eff_m2: float
    target_angle_deg: float
    offset_mm: float
    conveyor_speed_mm_per_sec: float
    delay_ms: float

    def __post_init__(self):
        for name, _ in FEATURES[3:]:
            finite_number(getattr(self, name), name)

    def analytical_gauge(self, duration_ms):
        """Re-evaluate the reference for this pulse using the common pressure model."""
        return (
            AnalyticalPressureModel()
            .predict(
                SimpleNamespace(mass_kg=self.mass_kg),
                SimpleNamespace(
                    inertia_axis_kg_m2=self.inertia_axis_kg_m2, delta_h_m=self.delta_h_m
                ),
                SimpleNamespace(
                    lever_arm_m=self.lever_arm_m,
                    area_eff_m2=self.area_eff_m2,
                    offset_m=self.offset_mm / 1000,
                ),
                setpoint(duration_ms, "pulse_duration_ms"),
                self.configuration,
            )
            .gauge_pa
            / 100
        )

    def row(self, pressure_mbar, duration_ms, analytical_gauge_mbar):
        return (
            setpoint(pressure_mbar, "pressure_mbar"),
            setpoint(duration_ms, "pulse_duration_ms"),
            finite_number(analytical_gauge_mbar, "analytical_gauge_mbar"),
            *(getattr(self, name) for name, _ in FEATURES[3:]),
        )


def context_for_candidate(analysis, candidate, configuration, parameters):
    """Use the same rounded offset and selected setup as a campaign."""
    if candidate.mechanics is None:
        raise ValueError("Dem Kandidaten fehlen die analytischen Kippdaten.")
    proposal = candidate_setup_parameters(parameters, candidate)
    array = proposal["arrays"][candidate.mount.array_index - 1]
    return FeatureContext(
        configuration,
        analysis.material.mass_kg,
        candidate.mechanics.inertia_axis_kg_m2,
        candidate.mechanics.delta_h_m,
        candidate.impact.lever_arm_m,
        candidate.impact.area_eff_m2,
        abs(candidate.turn.signed_angle_deg),
        array["offset_mm"],
        proposal["conveyor_speed_mm_per_sec"],
        array["delay_ms"],
    )


def context_from_snapshot(snapshot, parameters, array_index):
    raw_config = dict(snapshot["configuration"])
    # These fields affect the pressure formula. An absent value cannot be inferred
    # from the current defaults, which may differ from the recorded setup.
    for name in (
        "drag_coefficient",
        "concentricity_coefficient",
        "mesh_coefficient",
        "ambient_pa",
        "kappa",
        "diameter_m",
        "distance_m",
        "spread_angle_deg",
        "gravity_m_s2",
    ):
        finite_number(raw_config.get(name), name)
    raw_config["mounts"] = tuple(NozzleMount(**item) for item in raw_config["mounts"])
    candidate = snapshot["candidate"]
    mechanics, impact = candidate["mechanics"], candidate["impact"]
    array = parameters["arrays"][array_index - 1]
    return FeatureContext(
        ModelConfiguration(**raw_config),
        snapshot["material"]["mass_kg"],
        mechanics["inertia_axis_kg_m2"],
        mechanics["delta_h_m"],
        impact["lever_arm_m"],
        impact["area_eff_m2"],
        abs(candidate["turn"]["signed_angle_deg"]),
        array["offset_mm"],
        parameters["conveyor_speed_mm_per_sec"],
        array["delay_ms"],
    )
