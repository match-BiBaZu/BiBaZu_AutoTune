"""UI-independent analytical first-guess pipeline; suggestions never write parameters."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar

from .analytical_model import (
    AnalyticalModel,
    CandidateKey,
    CandidateResult,
    ModelConfiguration,
    OffsetEstimator,
    PressureModel,
)
from .geometry import GeometryProperties, MaterialProperties, MeshAnalysis
from .recipes import validate_parameters
from .trial_context import normalize_quaternion, validate_context

T = TypeVar("T")


@dataclass(frozen=True)
class StageResult(Generic[T]):
    status: Literal["ready", "not_implemented", "blocked"]
    value: T | None = None
    message: str = ""
    source: str = ""


@dataclass(frozen=True)
class ArrayInput:
    index: int
    nozzles_enabled: tuple[bool, ...]
    pulse_duration_ms: int
    offset_mm: float
    pressure_mbar: int
    delay_ms: int
    conveyor_velocity_delay: bool


@dataclass(frozen=True)
class AutotuneInput:
    geometry: GeometryProperties
    material: MaterialProperties
    context: dict
    arrays: tuple[ArrayInput, ...]
    conveyor_speed_mm_per_sec: float


class PulseEstimator(Protocol):
    def estimate(self, request: AutotuneInput) -> StageResult[dict[int, int]]: ...


class PressureRefiner(Protocol):
    def refine(
        self, request: AutotuneInput, pressures_mbar: dict[CandidateKey, float]
    ) -> StageResult[dict[CandidateKey, float]]: ...


class ManualPulseEstimator:
    def estimate(self, request):
        return StageResult(
            "ready",
            {a.index: a.pulse_duration_ms for a in request.arrays},
            "Impulsdauer manuell; automatische Schätzung noch nicht implementiert.",
            "manual",
        )


class PendingPressureRefiner:
    def refine(self, request, pressures_mbar):
        return StageResult("not_implemented", message="ML-Korrektur noch nicht implementiert.")


@dataclass(frozen=True)
class PreparationResult:
    missing: tuple[str, ...]
    pulse: StageResult[dict[int, int]]
    offset: StageResult[dict[CandidateKey, float]]
    pressure: StageResult[dict[CandidateKey, float]]
    sps_pressure: StageResult[dict[CandidateKey, float]]
    ml: StageResult[dict[CandidateKey, float]]
    candidates: tuple[CandidateResult, ...] = ()


class AutotunePipeline:
    def __init__(
        self,
        pulse: PulseEstimator | None = None,
        offset: OffsetEstimator | None = None,
        pressure: PressureModel | None = None,
        ml: PressureRefiner | None = None,
    ):
        self.pulse = pulse or ManualPulseEstimator()
        self.model = AnalyticalModel(offset, pressure)
        self.ml = ml or PendingPressureRefiner()

    def prepare(
        self,
        analysis: MeshAnalysis | None,
        context,
        parameters,
        configuration: ModelConfiguration | None = None,
        *,
        calculate=True,
    ) -> PreparationResult:
        parameters = validate_parameters(parameters)
        configuration = configuration or ModelConfiguration()
        configuration.validate()
        missing = []
        if analysis is None:
            missing.append("STL auswerten")
        elif not analysis.geometry.valid_volume:
            missing.append("Gültiges geschlossenes Volumen erforderlich")
        if context is None:
            missing.append("Roadmap-Transition oder manuelle Posen bestätigen")
        else:
            context = validate_context(context)
            try:
                for key in ("source_quaternion_xyzw", "target_quaternion_xyzw"):
                    normalize_quaternion(context.get(key))
            except ValueError:
                missing.append("Räumliche Ausgangs- und Zielorientierung erforderlich")
            if (
                analysis
                and context.get("kind") == "manual"
                and context["mesh_sha256"] != analysis.sha256
            ):
                missing.append("STL passt nicht zum bestätigten manuellen Pose-Kontext")
        if analysis and analysis.material.density_kg_m3 is None:
            missing.append("Dichte für physikalische Masse und Trägheit eingeben")
        arrays = parameters["arrays"]
        pulse = StageResult(
            "ready",
            {a["index"]: a["pulse_duration_ms"] for a in arrays},
            "Impulsdauer manuell; automatische Schätzung noch nicht implementiert.",
            "manual",
        )
        candidates = ()
        if context is not None:
            try:
                candidates = self.model.evaluate(
                    analysis,
                    context,
                    parameters,
                    configuration,
                    calculate=calculate and not missing,
                )
            except ValueError as exc:
                missing.append(str(exc))
        offsets = {c.key: c.impact.offset_m * 1000 for c in candidates if c.impact is not None}
        pressures = {c.key: c.pressure.gauge_pa / 100 for c in candidates if c.pressure is not None}
        offset = StageResult(
            "ready" if offsets else "blocked",
            offsets or None,
            "Automatischer Offset: berechnet"
            if offsets
            else "Automatischer Offset: Voraussetzungen prüfen oder Berechnung starten.",
        )
        pressure = StageResult(
            "ready" if pressures else "blocked",
            pressures or None,
            "Analytisches Druckmodell: berechnet"
            if pressures
            else "Analytisches Druckmodell: Voraussetzungen prüfen oder Berechnung starten.",
        )
        scaled = {
            c.key: c.sps_pressure.gauge_pa / 100 for c in candidates if c.sps_pressure is not None
        }
        sps_pressure = StageResult(
            "ready" if scaled else "blocked",
            scaled or None,
            "SPS-Druckskalierung: berechnet"
            if scaled
            else "SPS-Druckskalierung: analytisches Ergebnis und gültige "
            "Koeffizienten erforderlich.",
        )
        ml = StageResult(
            "not_implemented",
            message="ML-Korrektur noch nicht implementiert; wartet auf analytische Werte.",
        )
        analytical_ready = {
            c.key: c.pressure.gauge_pa / 100
            for c in candidates
            if c.pressure is not None and not c.pressure.warnings
        }
        if not missing and analytical_ready:
            request = AutotuneInput(
                analysis.geometry,
                analysis.material,
                copy.deepcopy(context),
                tuple(
                    ArrayInput(
                        a["index"],
                        tuple(a["nozzles_enabled"]),
                        a["pulse_duration_ms"],
                        a["offset_mm"],
                        a["pressure_mbar"],
                        a["delay_ms"],
                        a["conveyor_velocity_delay"],
                    )
                    for a in arrays
                ),
                parameters["conveyor_speed_mm_per_sec"],
            )
            pulse = self.pulse.estimate(request)
            ml = self.ml.refine(request, analytical_ready)
        return PreparationResult(
            tuple(missing), pulse, offset, pressure, sps_pressure, ml, candidates
        )
