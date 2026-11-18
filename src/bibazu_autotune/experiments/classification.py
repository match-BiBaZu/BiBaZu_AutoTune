"""Conservative endpoint classification from complete recorded 2D axis measurements."""

from __future__ import annotations

import math
from statistics import median

from .models import ClassificationConfig


def evaluate(
    rows, config: ClassificationConfig, *, completed=True, dropped_frames=0, stop_reason=None
):
    config.validate()
    if config.algorithm_version == 2:
        from .classification_v2 import evaluate_targeted

        return evaluate_targeted(
            rows,
            config,
            completed=completed,
            dropped_frames=dropped_frames,
            stop_reason=stop_reason,
        )
    samples = []
    previous = None
    segment = 0
    for row in rows:
        raw, stamp = row.get("angle_deg"), row.get("timestamp")
        reason = None
        valid = (
            isinstance(raw, (float, int))
            and not isinstance(raw, bool)
            and math.isfinite(raw)
            and row.get("valid", True)
            and row.get("present", True)
            and not row.get("touching_border", False)
            and isinstance(stamp, (float, int))
            and math.isfinite(stamp)
        )
        continuous = None
        if not valid:
            reason = "invalid_measurement"
        elif previous is not None:
            old_stamp = previous.get("timestamp")
            dt = stamp - old_stamp if isinstance(old_stamp, (int, float)) else -1
            frame, old_frame = row.get("frame_id"), previous.get("frame_id")
            if dt <= 0 or dt > config.max_gap_seconds + 1e-9:
                reason = "time_gap"
            elif isinstance(frame, int) and isinstance(old_frame, int) and frame != old_frame + 1:
                reason = "frame_gap"
            elif previous["continuous_angle_deg"] is None:
                reason = "after_invalid"
            else:
                delta = (raw - previous["angle_deg"] + 90) % 180 - 90
                if math.isclose(abs(delta), 90, abs_tol=1e-8):
                    reason = "ambiguous_step"
                else:
                    continuous = previous["continuous_angle_deg"] + delta
        if valid and continuous is None:
            continuous = float(raw)
        if reason:
            segment += 1
        sample = {**row, "continuous_angle_deg": continuous, "segment": segment, "gap": reason}
        samples.append(sample)
        previous = sample

    result = {
        "algorithm_version": 1,
        "label": None,
        "quality": "review_required",
        "reasons": [],
        "initial_angle_deg": None,
        "final_angle_deg": None,
        "rotation_deg": None,
        "max_rotation_deg": None,
        "overshoot_deg": None,
    }
    if not completed:
        result["reasons"].append("capture_incomplete")
    if dropped_frames:
        result["reasons"].append("dropped_frames")
    if not config.observable_confirmed:
        result["reasons"].append("observability_not_confirmed")
    valid_indices = [i for i, row in enumerate(samples) if row["continuous_angle_deg"] is not None]
    if not valid_indices:
        result["reasons"].append("no_valid_angles")
        return result, samples

    first, last = valid_indices[0], valid_indices[-1]

    def window(start, direction):
        chosen = []
        anchor = samples[start]
        for i in range(start, len(samples) if direction == 1 else -1, direction):
            row = samples[i]
            if row["continuous_angle_deg"] is None or row["segment"] != anchor["segment"]:
                return None
            chosen.append(row["continuous_angle_deg"])
            if max(chosen) - min(chosen) > config.stable_span_deg:
                return None
            if len(chosen) >= 3 and abs(row["timestamp"] - anchor["timestamp"]) >= (
                config.stable_seconds - 1e-9
            ):
                return median(chosen), i
        return None

    initial, final = window(first, 1), window(last, -1)
    if initial is None:
        result["reasons"].append("no_stable_initial_pose")
    if final is None:
        result["reasons"].append("no_stable_final_pose")
    if samples[first]["segment"] != samples[last]["segment"]:
        result["reasons"].append("interrupted_rotation")
    if initial and final:
        if initial[1] >= final[1]:
            result["reasons"].append("insufficient_observation")
        result["initial_angle_deg"], result["final_angle_deg"] = initial[0], final[0]
        if samples[first]["segment"] == samples[last]["segment"]:
            rotation = config.camera_direction * (final[0] - initial[0])
            peak = max(
                config.camera_direction * (row["continuous_angle_deg"] - initial[0])
                for row in samples[first : last + 1]
            )
            result.update(
                rotation_deg=rotation,
                max_rotation_deg=peak,
                overshoot_deg=max(0.0, peak - config.target_angle_deg),
            )
            if not result["reasons"]:
                low = config.target_angle_deg - config.tolerance_deg
                high = config.target_angle_deg + config.tolerance_deg
                result.update(
                    quality="valid",
                    label=(
                        "not_flipped"
                        if rotation < low - 1e-8
                        else "overflipped"
                        if rotation > high + 1e-8
                        else "flipped"
                    ),
                )
    return result, samples
