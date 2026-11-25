"""Empirical peak-pressure mapping, separate from the analytical force model.

The coefficients a and b are user inputs, not fitted automatically.
All pressures, including the intercept b, use Pa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_PRESSURE_SCALE_A = 1.5485
DEFAULT_PRESSURE_SCALE_B_PA = 43_216.7  # b = 432.167 mbar
PRESSURE_SCALING_SOURCE = (
    "Manuell vorgegebene Koeffizienten a und b; keine automatische Anpassung. "
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
    """Apply p_sps = a * p_analytisch + b, with gauge pressures and b in Pa."""
    validate_pressure_scaling(a, b_pa)
    if not math.isfinite(analytical_gauge_pa) or analytical_gauge_pa < 0:
        raise ValueError("Analytischer Überdruck muss endlich und nichtnegativ sein.")
    # p_sps = a * p_analytisch + b
    p_sps_pa = a * analytical_gauge_pa + b_pa
    if not math.isfinite(p_sps_pa):
        raise ValueError("Skalierter SPS-Druck liegt außerhalb des endlichen Zahlenbereichs.")
    warnings = (
        ("Skalierter SPS-Druck außerhalb der SPS-Druckgrenzen 0–6000 mbar",)
        if not 0 <= p_sps_pa <= 600000
        else ()
    )
    return SpsPressurePrediction(p_sps_pa, a, b_pa, warnings)
