"""Validated, JSON-serializable experiment plans; no hardware or Qt dependencies."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field

from ..recipes import validate_parameters
from ..trial_context import validate_context

LABELS = ("not_flipped", "flipped", "overflipped")


def finite(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: Zahl erforderlich.")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: zulässig {low}–{high}.")


@dataclass(frozen=True)
class SweepRange:
    minimum: int
    maximum: int
    step: int

    def values(self, low, high):
        for value in (self.minimum, self.maximum, self.step):
            if type(value) is not int:
                raise ValueError("Rasterwerte müssen Ganzzahlen sein.")
        if not low <= self.minimum <= self.maximum <= high or self.step <= 0:
            raise ValueError(f"Ungültiger Bereich: {low}–{high}; Schrittweite muss positiv sein.")
        if (self.maximum - self.minimum) % self.step:
            raise ValueError("Der Endwert muss mit der Schrittweite genau erreichbar sein.")
        return tuple(range(self.minimum, self.maximum + 1, self.step))


@dataclass(frozen=True)
class ClassificationConfig:
    target_angle_deg: float
    camera_direction: int = 1
    tolerance_deg: float = 10.0
    stable_seconds: float = 0.05
    stable_span_deg: float = 5.0
    max_gap_seconds: float = 0.1
    observable_confirmed: bool = False

    def validate(self):
        finite(self.target_angle_deg, "Zieldrehung", 0.001, 360)
        finite(self.tolerance_deg, "Zieltoleranz", 0.001, 90)
        finite(self.stable_seconds, "Stabilitätsdauer", 0.001, 10)
        finite(self.stable_span_deg, "Stabilitätsspanne", 0.001, 90)
        finite(self.max_gap_seconds, "Maximale Zeitlücke", 0.001, 1)
        if type(self.camera_direction) is not int or self.camera_direction not in (-1, 1):
            raise ValueError("Kameradrehsinn muss +1 oder −1 sein.")
        if type(self.observable_confirmed) is not bool:
            raise ValueError("Beobachtbarkeit muss ausdrücklich bestätigt werden.")


@dataclass(frozen=True)
class SweepPoint:
    index: int
    pressure_mbar: int
    pulse_duration_ms: int


@dataclass(frozen=True)
class ExperimentPlan:
    context: dict
    parameters: dict
    array_index: int
    pressure: SweepRange
    pulse: SweepRange
    classification: ClassificationConfig
    repetitions: int = 5
    settling_seconds: float = 5.0
    trial_timeout_seconds: float = 60.0
    snapshot: dict = field(default_factory=dict)

    def validate(self):
        validate_context(self.context)
        parameters = validate_parameters(self.parameters)
        if type(self.array_index) is not int or self.array_index not in range(1, 5):
            raise ValueError("Genau ein physisches Array 1–4 auswählen.")
        selected = parameters["arrays"][self.array_index - 1]
        if not selected["enabled"] or not any(selected["nozzles_enabled"]):
            raise ValueError("Der Kandidat benötigt aktive Düsen.")
        if any(a["enabled"] for a in parameters["arrays"] if a["index"] != self.array_index):
            raise ValueError("Nur das ausgewählte Versuchsarray darf aktiv sein.")
        if parameters["conveyor_speed_mm_per_sec"] <= 0:
            raise ValueError("Bandgeschwindigkeit größer als 0 mm/s festlegen.")
        if type(self.repetitions) is not int or not 1 <= self.repetitions <= 10000:
            raise ValueError("Wiederholungszahl muss zwischen 1 und 10000 liegen.")
        finite(self.settling_seconds, "Einschwingzeit", 0, 3600)
        finite(self.trial_timeout_seconds, "Versuchszeitlimit", 1, 3600)
        self.classification.validate()
        pressures = self.pressure.values(0, 6000)
        durations = self.pulse.values(1, 500)
        if len(pressures) * len(durations) * self.repetitions > 1000000:
            raise ValueError("Eine Kampagne darf höchstens eine Million Versuche enthalten.")

    def points(self):
        pressures = self.pressure.values(0, 6000)
        result = []
        for row, duration in enumerate(self.pulse.values(1, 500)):
            for pressure in pressures if row % 2 == 0 else reversed(pressures):
                result.append(SweepPoint(len(result), pressure, duration))
        return tuple(result)

    def parameters_at(self, point, *, enabled=True):
        result = copy.deepcopy(self.parameters)
        for array in result["arrays"]:
            array["enabled"] = enabled and array["index"] == self.array_index
            if array["index"] != self.array_index:
                array["nozzles_enabled"] = [False] * 6
        array = result["arrays"][self.array_index - 1]
        array.update(pressure_mbar=point.pressure_mbar, pulse_duration_ms=point.pulse_duration_ms)
        return validate_parameters(result)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        value = copy.deepcopy(value)
        value["pressure"] = SweepRange(**value["pressure"])
        value["pulse"] = SweepRange(**value["pulse"])
        value["classification"] = ClassificationConfig(**value["classification"])
        result = cls(**value)
        result.validate()
        return result


@dataclass
class TrialRecord:
    campaign_id: str
    trial_id: str
    point_index: int
    repetition: int
    created_utc: str
    requested_parameters: dict
    status: str = "applying"
    replaces_trial_id: str | None = None


@dataclass(frozen=True)
class ReviewAnnotation:
    label: str | None
    excluded: bool
    reason: str
    created_utc: str

    def validate(self):
        if self.label is not None and self.label not in LABELS:
            raise ValueError("Unbekannte Klasse.")
        if type(self.excluded) is not bool or not self.reason.strip():
            raise ValueError("Eine Begründung ist erforderlich.")
