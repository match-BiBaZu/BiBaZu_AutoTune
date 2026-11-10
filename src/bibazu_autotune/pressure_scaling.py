"""Empirical peak-pressure mapping, separate from the analytical force model.

All pressures, including the intercept, use Pa. The defaults fit the 100-ms
measurements of array 2/nozzle 1, repetitions 2–5, run 53f1095c8f3244b182ab08c3980a6dac.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_PRESSURE_SCALE_A = 0.6544389198933329
DEFAULT_PRESSURE_SCALE_B_PA = -28480.891486008954
PRESSURE_SCALING_SOURCE = (
    "Vorgaben aus Spitzendruck-Fit: 100 ms, Array 2/Düse 1, Wiederholungen 2–5. "
    "Das aktuelle a/b-Paar gilt vorläufig für alle Kandidaten; "
    "andere Impulsdauern und Düsenkombinationen sind damit nicht gesondert kalibriert."
)


def validate_pressure_scaling(a: float, b_pa: float):
    if isinstance(a, bool) or not isinstance(a, (int, float)) or not math.isfinite(a) or a <= 0:
        raise ValueError("Druckskalierung a muss positiv und endlich sein.")
    if isinstance(b_pa, bool) or not isinstance(b_pa, (int, float)) or not math.isfinite(b_pa):
        raise ValueError("Druckskalierung b muss endlich sein (beide Vorzeichen erlaubt).")


@dataclass(frozen=True)
class SpsPressurePrediction:
    gauge_pa: float
    scale_a: float
    scale_b_pa: float
    warnings: tuple[str, ...] = ()
    source: str = PRESSURE_SCALING_SOURCE


def scale_pressure(analytical_gauge_pa: float, a: float, b_pa: float) -> SpsPressurePrediction:
    validate_pressure_scaling(a, b_pa)
    if not math.isfinite(analytical_gauge_pa) or analytical_gauge_pa < 0:
        raise ValueError("Analytischer Überdruck muss endlich und nichtnegativ sein.")
    gauge_pa = (analytical_gauge_pa - b_pa) / a
    if not math.isfinite(gauge_pa):
        raise ValueError("Skalierter SPS-Druck liegt außerhalb des endlichen Zahlenbereichs.")
    warnings = (
        ("Skalierter SPS-Druck außerhalb der SPS-Druckgrenzen 0–6000 mbar",)
        if not 0 <= gauge_pa <= 600000
        else ()
    )
    return SpsPressurePrediction(gauge_pa, a, b_pa, warnings)
