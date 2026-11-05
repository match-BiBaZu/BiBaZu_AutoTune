"""Fixed pressure sweep, timestamped ADS traces and durable measurement exports."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

FILETIME_MS = 10_000
SIGNALS = ("raw1", "raw2", "bar1", "bar2", "valve")
SCALING = "SPS: Rohwert * 10 / 32767 bar; Sensortyp/Skalierung noch nicht bestätigt"


@dataclass(frozen=True)
class PressurePulse:
    number: int
    pulse_ms: int
    pressure_mbar: int
    repetition: int


def pressure_matrix() -> tuple[PressurePulse, ...]:
    combinations = (
        (duration, pressure, repeat)
        for duration in (20, 50, 100)
        for pressure in range(1000, 6001, 1000)
        for repeat in range(1, 6)
    )
    return tuple(PressurePulse(i, *values) for i, values in enumerate(combinations, 1))


def atomic_text(path: Path, text: str):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def csv_text(columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def analyze_pressure_trace(pulse: PressurePulse, events, *, error=None):
    """Never infer missing samples or a valve edge from host-clock timing."""
    groups = {signal: {} for signal in SIGNALS}
    for signal, timestamp, value in events:
        if signal in groups:
            groups[signal][int(timestamp)] = value
    valve = sorted(groups["valve"].items())
    opened = next((t for t, value in valve if value), None)
    closed = next(
        (t for t, value in valve if opened is not None and t > opened and not value), None
    )
    reasons = [error] if error else []
    if opened is None or closed is None:
        reasons.append("Öffnungs- oder Schließkommando fehlt in den ADS-Notifications.")
    anchor = opened if opened is not None else (valve[0][0] if valve else 0)
    end = (closed if closed is not None else anchor) + 200 * FILETIME_MS
    start = anchor - 100 * FILETIME_MS
    # The ADS server timestamps all cyclic values in the same PLC task. Missing
    # timestamps remain missing instead of being interpolated into complete data.
    ticks = sorted(t for t in groups["valve"] if start <= t <= end)
    rows = []
    for timestamp in ticks:
        row = {"timestamp_filetime": timestamp, "time_ms": (timestamp - anchor) / FILETIME_MS}
        row.update({signal: groups[signal].get(timestamp) for signal in SIGNALS})
        rows.append(row)
    for signal, samples in groups.items():
        times = sorted(t for t in samples if start <= t <= end)
        if not times or times[0] > start or times[-1] < end:
            reasons.append(f"{signal}: Vor-/Nachlauf unvollständig.")
        elif any(b - a != FILETIME_MS for a, b in zip(times, times[1:], strict=False)):
            reasons.append(f"{signal}: Lücke oder abweichender Abtastabstand.")
    if any(row[signal] is None for row in rows for signal in SIGNALS):
        reasons.append(
            "Die Signale sind nicht vollständig auf gemeinsame SPS-Zeitstempel abgebildet."
        )
    if any(
        not isinstance(value, (bool, int, float)) or not math.isfinite(value)
        for samples in groups.values()
        for value in samples.values()
    ):
        reasons.append("Nicht endliche oder ungültige Messwerte.")
    observed = (
        (closed - opened) / FILETIME_MS if opened is not None and closed is not None else None
    )
    if observed is not None and observed != pulse.pulse_ms:
        reasons.append(f"Ventilkommando dauerte {observed:g} statt {pulse.pulse_ms} ms.")
    result = {
        **asdict(pulse),
        "complete": not reasons,
        "error": "; ".join(dict.fromkeys(reasons)),
        "observed_pulse_ms": observed,
        "baseline_bar": None,
        "peak_bar": None,
        "mean_open_bar": None,
    }
    if not reasons:
        result["baseline_bar"] = statistics.mean(row["bar2"] for row in rows if row["time_ms"] < 0)
        result["peak_bar"] = max(row["bar2"] for row in rows if row["time_ms"] >= 0)
        result["mean_open_bar"] = statistics.mean(row["bar2"] for row in rows if row["valve"])
    return result, rows


class PressureRecording:
    def __init__(self, directory, run_id, baseline):
        self.path = Path(directory) / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.results = []
        self.manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "array": 2,
            "nozzle": 1,
            "valve_symbol": "MAIN.OpenValve5",
            "sensor_channel": 2,
            "notification_cycle_ms": 1,
            "pre_ms": 100,
            "post_ms": 200,
            "pause_ms": 3000,
            "settle_ms": 1000,
            "scaling": SCALING,
            "sampling_note": "ADS-Serverzeitstempel; Sensor-/EL3068-Auflösung separat begrenzt.",
            "probe_interface_version": 2,
            "timed_pulse_feature_version": 1,
            "signal_map": {
                "raw1": "MAIN.RawNozzlePressure",
                "raw2": "MAIN.RawNozzlePressure2",
                "bar1": "MAIN.NozzlePressure",
                "bar2": "MAIN.NozzlePressure2",
                "valve": "MAIN.OpenValve5",
            },
            "baseline_parameters": baseline,
            "matrix": [asdict(p) for p in pressure_matrix()],
            "results": self.results,
        }
        self.persist()

    def persist(self):
        atomic_text(self.path / "run.json", json.dumps(self.manifest, indent=2, ensure_ascii=False))

    def add(self, pulse, events, *, error=None):
        result, rows = analyze_pressure_trace(pulse, events, error=error)
        raw_rows = [
            {"signal": signal, "timestamp_filetime": timestamp, "value": value}
            for signal, timestamp, value in events
        ]
        stem = f"pulse_{pulse.number:03d}"
        atomic_text(
            self.path / f"{stem}_notifications.csv",
            csv_text(["signal", "timestamp_filetime", "value"], raw_rows),
        )
        atomic_text(
            self.path / f"{stem}.csv", csv_text(["timestamp_filetime", "time_ms", *SIGNALS], rows)
        )
        self.results.append(result)
        self.persist()
        atomic_text(self.path / "summary.csv", csv_text(list(result), self.results))
        grouped = []
        for duration in (20, 50, 100):
            for pressure in range(1000, 6001, 1000):
                valid = [
                    r
                    for r in self.results
                    if r["complete"]
                    and r["pulse_ms"] == duration
                    and r["pressure_mbar"] == pressure
                ]
                group = {"pulse_ms": duration, "pressure_mbar": pressure, "valid_count": len(valid)}
                for key in ("peak_bar", "mean_open_bar"):
                    values = [r[key] for r in valid]
                    group[key + "_mean"] = statistics.mean(values) if values else None
                    group[key + "_std"] = statistics.stdev(values) if len(values) > 1 else None
                grouped.append(group)
        atomic_text(self.path / "combinations.csv", csv_text(list(grouped[0]), grouped))
        return result, rows

    def finish(self, status, error=None):
        self.aggregate_samples()
        self.manifest.update(
            status=status, error=error, finished_utc=datetime.now(timezone.utc).isoformat()
        )
        self.persist()

    def aggregate_samples(self):
        columns = [
            "number",
            "pulse_ms",
            "pressure_mbar",
            "repetition",
            "complete",
            "timestamp_filetime",
            "time_ms",
            *SIGNALS,
        ]
        rows = []
        for result in self.results:
            metadata = {key: result[key] for key in columns[:5]}
            with (self.path / f"pulse_{result['number']:03d}.csv").open(
                encoding="utf-8", newline=""
            ) as stream:
                rows.extend({**metadata, **row} for row in csv.DictReader(stream))
        atomic_text(self.path / "samples.csv", csv_text(columns, rows))
