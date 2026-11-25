"""Evaluate a grid of SPS setpoints for a fixed analytical setup."""

from dataclasses import dataclass

import numpy as np

from .features import FEATURES, setpoint

MAX_POINTS = 10_000


@dataclass(frozen=True)
class GridRange:
    minimum: int
    maximum: int
    step: int

    def values(self, name):
        setpoint(self.minimum, name)
        setpoint(self.maximum, name)
        if type(self.step) is not int or self.step < 1 or self.minimum > self.maximum:
            raise ValueError("Ungültiges Raster. Untergrenze, Obergrenze und Schrittweite prüfen.")
        if (self.maximum - self.minimum) % self.step:
            raise ValueError("Die Obergrenze muss mit der Schrittweite erreichbar sein.")
        return range(self.minimum, self.maximum + 1, self.step)


@dataclass(frozen=True)
class GridPoint:
    pressure_mbar: int
    pulse_duration_ms: int
    analytical_mbar: float
    probabilities: dict[str, float]
    outside_features: tuple[str, ...]

    @property
    def flip_probability(self):
        return self.probabilities["flipped"]


def grid_axes(pressure, duration):
    pressures = pressure.values("pressure_mbar")
    durations = duration.values("pulse_duration_ms")
    if len(pressures) * len(durations) > MAX_POINTS:
        raise ValueError(f"Das Raster darf höchstens {MAX_POINTS} Parameterpaare enthalten.")
    return pressures, durations


def score_grid(model, context, pressure, duration):
    from threadpoolctl import threadpool_limits

    pressures, durations = grid_axes(pressure, duration)
    rows = []
    for duration_ms in durations:
        reference = context.analytical_gauge(duration_ms)
        rows.extend(context.row(value, duration_ms, reference) for value in pressures)

    matrix = np.asarray(rows, dtype=float)
    lower, upper = model.feature_bounds.T
    outside = ((matrix < lower) & ~np.isclose(matrix, lower, rtol=1e-8, atol=1e-12)) | (
        (matrix > upper) & ~np.isclose(matrix, upper, rtol=1e-8, atol=1e-12)
    )
    with threadpool_limits(limits=1):
        probabilities = np.concatenate(
            [model.probabilities(matrix[i : i + 256]) for i in range(0, len(rows), 256)],
        )
    if (
        probabilities.shape != (len(rows), len(model.classes))
        or not np.isfinite(probabilities).all()
        or (probabilities < 0).any()
        or not np.allclose(probabilities.sum(axis=1), 1)
    ):
        raise ValueError("Das Modell lieferte ungültige Wahrscheinlichkeiten.")
    points = [
        GridPoint(
            row[0],
            row[1],
            row[2],
            {label: float(p) for label, p in zip(model.classes, probability, strict=True)},
            tuple(name for (name, _), flag in zip(FEATURES, flags, strict=True) if flag),
        )
        for row, probability, flags in zip(rows, probabilities, outside, strict=True)
    ]
    return sorted(
        points, key=lambda row: (-row.flip_probability, row.pressure_mbar, row.pulse_duration_ms)
    )
