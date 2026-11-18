"""Reviewed tabular exports and future task definitions; no training dependencies."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .storage import CampaignStore

MODEL_FAMILIES = ("Neural Network", "Gaussian Process", "Random Forest")
TASKS = {
    "classifier": {"target": "label", "description": "Flipklasse aus Geometrie und Parametern"},
    "regressor": {
        "target": "suitable_pressure_mbar",
        "description": "Geeigneter Druck / analytische Korrektur bei fester Dauer",
        "status": "Zielwertbildung aus Wiederholungen folgt mit dem Training",
    },
}


def flatten_scalars(value, prefix="feature"):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(flatten_scalars(item, prefix + "_" + key))
    elif isinstance(value, (tuple, list)) and len(value) <= 9:
        for index, item in enumerate(value):
            result.update(flatten_scalars(item, prefix + "_" + str(index)))
    elif value is None or isinstance(value, (str, bool, int, float)):
        result[prefix] = value
    return result


class DatasetReader:
    def __init__(self, campaigns):
        self.stores = [
            item if isinstance(item, CampaignStore) else CampaignStore(item) for item in campaigns
        ]

    def rows(self, *, include_excluded=False):
        for store in self.stores:
            plan = store.plan
            snapshot = plan.snapshot
            features = flatten_scalars(
                {
                    key: snapshot.get(key)
                    for key in (
                        "geometry",
                        "material",
                        "configuration",
                        "candidate_mount",
                    )
                }
            )
            candidate = snapshot.get("candidate", {})
            features.update(
                flatten_scalars(
                    {
                        "mechanics": candidate.get("mechanics"),
                        "impact": candidate.get("impact"),
                        "oriented_center_m": candidate.get("center_m"),
                        "oriented_inertia_kg_m2": candidate.get("inertia_cm_kg_m2"),
                    }
                )
            )
            for record in store.trials():
                annotation = store.annotation(record["trial_id"])
                evaluation = record.get("evaluation", {})
                label = annotation["label"] if annotation else evaluation.get("label")
                excluded = annotation["excluded"] if annotation else False
                if excluded and not include_excluded:
                    continue
                array = record["requested_parameters"]["arrays"][plan.array_index - 1]
                readback = record.get("readback_parameters", {}).get("arrays", [])
                actual = next((a for a in readback if a["index"] == plan.array_index), {})
                prediction = record.get("analytical", {}).get("pressure", {}).get("gauge_pa")
                yield {
                    **features,
                    "campaign_id": store.campaign_id,
                    "trial_id": record["trial_id"],
                    "part_group": snapshot.get("mesh_sha256", plan.context.get("part_name")),
                    "transition_group": json.dumps(plan.context, sort_keys=True),
                    "point_index": record["point_index"],
                    "repetition": record["repetition"],
                    "status": record["status"],
                    "resolution": record.get("resolution"),
                    "array_index": plan.array_index,
                    "nozzles": "+".join(
                        str(i + 1) for i, on in enumerate(array["nozzles_enabled"]) if on
                    ),
                    "input_pressure_mbar": array["pressure_mbar"],
                    "input_pulse_duration_ms": array["pulse_duration_ms"],
                    "input_offset_mm": array["offset_mm"],
                    "input_delay_ms": array["delay_ms"],
                    "input_conveyor_speed_mm_per_sec": record["requested_parameters"][
                        "conveyor_speed_mm_per_sec"
                    ],
                    "input_target_angle_deg": plan.classification.target_angle_deg,
                    "input_rotation_axis": candidate.get("turn", {}).get("axis"),
                    "readback_pressure_mbar": actual.get("pressure_mbar"),
                    "analytical_pressure_mbar": prediction / 100
                    if prediction is not None
                    else None,
                    "label": label,
                    "automatic_label": evaluation.get("label"),
                    "quality": evaluation.get("quality", "review_required"),
                    "quality_reasons": ";".join(evaluation.get("reasons", [])),
                    "reviewed": annotation is not None,
                    "excluded": excluded,
                    "review_reason": annotation["reason"] if annotation else "",
                    "outcome_rotation_deg": evaluation.get("rotation_deg"),
                    "outcome_max_rotation_deg": evaluation.get("max_rotation_deg"),
                }

    def classifier_rows(self):
        return [
            row
            for row in self.rows()
            if row["label"] is not None and (row["quality"] == "valid" or row["reviewed"])
        ]

    def export_csv(self, path, *, include_excluded=True):
        path = Path(path).resolve()
        if path.suffix.lower() != ".csv" or any(
            path.is_relative_to(store.directory / folder)
            for store in self.stores
            for folder in ("assets", "trials")
        ):
            raise ValueError("CSV außerhalb der Originaldaten-/Assetordner speichern.")
        rows = list(self.rows(include_excluded=include_excluded))
        fields = sorted({key for row in rows for key in row}) or ["campaign_id", "trial_id"]
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)
