"""Build an editor proposal atomically; no machine or UI side effects."""

import math
from decimal import ROUND_HALF_UP, Decimal

from .analytical_model import CandidateResult
from .recipes import validate_parameters


def check_candidate(candidate: CandidateResult):
    if (
        candidate.status not in {"ready", "outside_model"}
        or candidate.pressure is None
        or candidate.impact is None
    ):
        raise ValueError("Der ausgewählte Kandidat besitzt kein übernehmbares Ergebnis.")
    pressure = candidate.pressure.gauge_pa / 100
    offset = candidate.impact.offset_m * 1000
    if not math.isfinite(pressure) or not 0 <= pressure <= 6000:
        raise ValueError("Berechneter Druck liegt außerhalb der SPS-Grenzen 0–6000 mbar.")
    if not math.isfinite(offset) or not 0 <= offset <= 5000:
        raise ValueError("Berechneter Offset liegt außerhalb der SPS-Grenzen 0–5000 mm.")
    if candidate.mount.array_index not in range(1, 5) or candidate.mount.nozzle_index not in (1, 4):
        raise ValueError("Ungültige Array-/Düsenkombination.")


def candidate_parameters(parameters: dict, candidate: CandidateResult) -> dict:
    check_candidate(candidate)
    result = validate_parameters(parameters)
    if result["conveyor_speed_mm_per_sec"] <= 0:
        raise ValueError("Vor der Übernahme eine Bandgeschwindigkeit größer als 0 mm/s eintragen.")
    array = next(a for a in result["arrays"] if a["index"] == candidate.mount.array_index)
    if array["pulse_duration_ms"] != candidate.pulse_ms:
        raise ValueError("Impulsdauer wurde geändert; Berechnung erneut durchführen.")
    array["pressure_mbar"] = int(
        (Decimal(str(candidate.pressure.gauge_pa)) / 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    array["offset_mm"] = float(
        (Decimal(str(candidate.impact.offset_m)) * 1000).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    )
    array["enabled"] = True
    array["nozzles_enabled"] = [i == candidate.mount.nozzle_index for i in range(1, 7)]
    array["conveyor_velocity_delay"] = True
    return validate_parameters(result)
