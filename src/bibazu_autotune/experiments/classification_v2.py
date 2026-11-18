"""Phase-aware evaluation; raw measurements and missing samples are never synthesized."""

from __future__ import annotations

import math
from statistics import median


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid(row):
    return (
        number(row.get("timestamp"))
        and number(row.get("angle_deg"))
        and row.get("valid", True)
        and row.get("present", True)
        and not row.get("touching_border", False)
    )


def delta(left, right):
    return (right - left + 90) % 180 - 90


def samples_from(rows, config, bridges):
    samples, generation = [], 0
    for index, row in enumerate(rows):
        continuous, reason = None, None
        segment = samples[-1]["segment"] if samples else 0
        if not valid(row):
            reason = "invalid_measurement"
        elif index:
            previous = samples[bridges.get(index, index - 1)]
            dt = (
                row["timestamp"] - previous["timestamp"]
                if number(previous.get("timestamp"))
                else -1
            )
            frame, old_frame = row.get("frame_id"), previous.get("frame_id")
            if dt <= 0 or dt > config.max_gap_seconds + 1e-9:
                reason = "time_gap"
            elif (
                index not in bridges
                and type(frame) is int
                and type(old_frame) is int
                and frame != old_frame + 1
            ):
                reason = "frame_gap"
            elif previous["continuous_angle_deg"] is None:
                reason = "after_invalid"
            else:
                step = delta(previous["angle_deg"], row["angle_deg"])
                if math.isclose(abs(step), 90, abs_tol=1e-8):
                    reason = "ambiguous_step"
                else:
                    continuous = previous["continuous_angle_deg"] + step
                    segment = previous["segment"]
        if reason:
            generation += 1
            segment = generation
        if valid(row) and continuous is None:
            continuous = float(row["angle_deg"])
        samples.append(
            {**row, "continuous_angle_deg": continuous, "segment": segment, "gap": reason}
        )
        if index in bridges:
            samples[-1]["gap"] = "bridged_rest_gap"
    return samples


def stable_window(samples, start, direction, seconds, span):
    anchor = samples[start]
    if anchor["continuous_angle_deg"] is None:
        return None
    values, indices = [], []
    previous = start
    for index in range(start, len(samples) if direction == 1 else -1, direction):
        row = samples[index]
        # A stability window itself must be observed without even a tolerated gap.
        if row["continuous_angle_deg"] is None or row["segment"] != anchor["segment"]:
            return None
        if index != start and samples[max(index, previous)].get("gap"):
            return None
        values.append(row["continuous_angle_deg"])
        indices.append(index)
        if max(values) - min(values) > span + 1e-9:
            return None
        if len(values) >= 3 and abs(row["timestamp"] - anchor["timestamp"]) >= seconds - 1e-9:
            return {"angle": median(values), "start": min(indices), "end": max(indices)}
        previous = index
    return None


def rest_bridges(rows, baseline, config):
    bridges, details = {}, []
    indices = [i for i, row in enumerate(rows) if valid(row)]
    for left, right in zip(indices, indices[1:], strict=False):
        if baseline[left]["segment"] == baseline[right]["segment"]:
            continue
        elapsed = rows[right]["timestamp"] - rows[left]["timestamp"]
        if not 0 < elapsed <= min(0.02, config.max_gap_seconds) + 1e-9:
            continue
        frame, old_frame = rows[right].get("frame_id"), rows[left].get("frame_id")
        missing = right - left - 1
        if type(frame) is int and type(old_frame) is int:
            if frame <= old_frame:
                continue
            missing = max(missing, frame - old_frame - 1)
        if missing > 2:
            continue
        stamps = [row.get("timestamp") for row in rows[left : right + 1]]
        if not all(number(t) for t in stamps) or any(
            b <= a for a, b in zip(stamps, stamps[1:], strict=False)
        ):
            continue
        before = stable_window(
            baseline, left, -1, max(0.05, config.stable_seconds), config.stable_span_deg
        )
        after = stable_window(
            baseline, right, 1, max(0.05, config.stable_seconds), config.stable_span_deg
        )
        if before is None or after is None:
            continue
        values = [
            delta(rows[left]["angle_deg"], rows[i]["angle_deg"])
            for a, b in ((before["start"], left), (right, after["end"]))
            for i in range(a, b + 1)
        ]
        if max(values) - min(values) > min(5.0, config.stable_span_deg) + 1e-9:
            continue
        bridges[right] = left
        details.append(
            {
                "before_timestamp": stamps[0],
                "after_timestamp": stamps[-1],
                "missing_frames": missing,
            }
        )
    return bridges, details


def evaluate_targeted(rows, config, *, completed, dropped_frames, stop_reason):
    rows = list(rows)
    baseline = samples_from(rows, config, {})
    bridges, details = rest_bridges(rows, baseline, config)
    samples = samples_from(rows, config, bridges) if bridges else baseline
    result = {
        "algorithm_version": 2,
        "label": None,
        "quality": "review_required",
        "reasons": [],
        "warnings": ["rest_gap_bridged"] if bridges else [],
        "bridged_gaps": details,
        "initial_angle_deg": None,
        "final_angle_deg": None,
        "rotation_deg": None,
        "max_rotation_deg": None,
        "overshoot_deg": None,
    }
    reasons, warnings = result["reasons"], result["warnings"]
    if not config.observable_confirmed:
        reasons.append("observability_not_confirmed")
    if not completed:
        reasons.append("capture_incomplete")
    if dropped_frames:
        reasons.append("dropped_frames")
    if not any(valid(row) for row in rows):
        reasons.append("no_valid_angles")
        return result, samples
    initial = next(
        (
            window
            for i in range(len(samples))
            if (
                window := stable_window(
                    samples, i, 1, config.stable_seconds, config.stable_span_deg
                )
            )
        ),
        None,
    )
    final = next(
        (
            window
            for i in range(len(samples) - 1, -1, -1)
            if (
                window := stable_window(
                    samples, i, -1, config.stable_seconds, config.stable_span_deg
                )
            )
        ),
        None,
    )
    if initial is None:
        reasons.append("no_stable_initial_pose")
    if final is None:
        reasons.append("no_stable_final_pose")
    if initial is None or final is None:
        return result, samples
    first, last = initial["start"], final["end"]
    result.update(
        initial_angle_deg=initial["angle"],
        final_angle_deg=final["angle"],
        analysis_start_timestamp=samples[first]["timestamp"],
        analysis_end_timestamp=samples[last]["timestamp"],
    )
    if first:
        if any(valid(row) for row in rows[:first]):
            reasons.append("no_stable_initial_pose")
        else:
            warnings.append("unusable_prefix_ignored")
    if last != len(samples) - 1:
        if any(valid(row) for row in rows[last + 1 :]):
            reasons.append("no_stable_final_pose")
        else:
            warnings.append("unusable_suffix_ignored")
    if initial["end"] >= final["start"]:
        reasons.append("insufficient_observation")
    observed = [row for row in samples[first : last + 1] if row["continuous_angle_deg"] is not None]
    if len({row["segment"] for row in observed}) != 1:
        reasons.append("interrupted_rotation")
        return result, samples
    rotation = config.camera_direction * (final["angle"] - initial["angle"])
    peak = max(
        config.camera_direction * (row["continuous_angle_deg"] - initial["angle"])
        for row in observed
    )
    result.update(
        rotation_deg=rotation,
        max_rotation_deg=peak,
        overshoot_deg=max(0.0, peak - config.target_angle_deg),
    )
    if (
        not completed
        and stop_reason == "memory_limit"
        and dropped_frames in (0, 1)
        and last == len(samples) - 1
    ):
        end_window = stable_window(
            samples, last, -1, max(0.2, config.stable_seconds), config.stable_span_deg
        )
        if (
            end_window
            and end_window["start"] > initial["end"]
            and any(
                abs(row["continuous_angle_deg"] - initial["angle"]) > 5.0 + 1e-9
                for row in samples[initial["end"] + 1 : end_window["start"]]
                if row["continuous_angle_deg"] is not None
            )
        ):
            reasons.remove("capture_incomplete")
            if "dropped_frames" in reasons:
                reasons.remove("dropped_frames")
            warnings.append("memory_limit_after_stable_final_pose")
    if not reasons:
        low, high = (
            config.target_angle_deg - config.tolerance_deg,
            config.target_angle_deg + config.tolerance_deg,
        )
        result.update(
            quality="valid",
            label="not_flipped"
            if rotation < low - 1e-8
            else "overflipped"
            if rotation > high + 1e-8
            else "flipped",
        )
    return result, samples
