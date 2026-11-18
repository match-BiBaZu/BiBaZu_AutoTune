"""Portable campaign folders, atomic records and auditable review annotations."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..engine import atomic_json
from . import SCHEMA_VERSION
from .classification import evaluate
from .models import ExperimentPlan, ReviewAnnotation


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Dateipfad verlässt den Datenordner.")
    return path


def write_samples(path, samples):
    fields = [
        "timestamp",
        "frame_id",
        "angle_deg",
        "continuous_angle_deg",
        "segment",
        "gap",
        "valid",
        "present",
        "touching_border",
        "confidence",
        "axis_deg",
        "area",
        "processing_ms",
    ]
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(samples)
    temporary.replace(path)


class CampaignStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.manifest = read_json(self.directory / "campaign.json")
        if self.manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Nicht unterstützte Kampagnenversion.")
        self.campaign_id = self.manifest["campaign_id"]
        self.plan = ExperimentPlan.from_dict(self.manifest["plan"])

    @classmethod
    def create(cls, root, plan, *, assets=None, metadata=None):
        plan.validate()
        campaign_id = uuid.uuid4().hex
        directory = Path(root).resolve() / campaign_id
        directory.mkdir(parents=True, exist_ok=False)
        records = []
        for name, source in (assets or {}).items():
            source = Path(source).resolve()
            if not re.fullmatch(r"[a-z_]+", name):
                raise ValueError("Ungültiger Assetname.")
            content = source.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if name == "mesh" and digest != plan.snapshot.get("mesh_sha256"):
                raise ValueError("STL wurde seit der Auswertung geändert; erneut auswerten.")
            target = directory / "assets" / (name + source.suffix.lower())
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(content)
            records.append(
                {
                    "kind": name,
                    "path": target.relative_to(directory).as_posix(),
                    "sha256": digest,
                    "source_path": str(source),
                }
            )
        if not any(item["kind"] == "mesh" for item in records):
            raise ValueError("Die Kampagne benötigt eine archivierte STL.")
        atomic_json(
            directory / "campaign.json",
            {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": campaign_id,
                "created_utc": utc_now(),
                "plan": plan.to_dict(),
                "assets": records,
                "metadata": metadata or {},
                "units": {
                    "geometry": "SI",
                    "pressure_mbar": "mbar gauge (PLC setpoint)",
                    "pulse_duration_ms": "ms",
                    "angle_deg": "degree",
                    "timestamp": "camera monotonic host seconds; not PLC synchronized",
                },
            },
        )
        store = cls(directory)
        store.checkpoint(0, "gate")
        return store

    def verify_assets(self):
        for asset in self.manifest["assets"]:
            path = inside(self.directory, asset["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise ValueError("Archiviertes Asset fehlt oder wurde verändert.")

    def trial_directory(self, trial_id):
        if not re.fullmatch(r"[0-9a-f]{32}", trial_id):
            raise ValueError("Ungültige Versuchskennung.")
        return self.directory / "trials" / trial_id

    def save_trial(self, record):
        atomic_json(self.trial_directory(record["trial_id"]) / "trial.json", record)

    def trials(self):
        return sorted(
            (read_json(path) for path in self.directory.glob("trials/*/trial.json")),
            key=lambda row: (row["created_utc"], row["trial_id"]),
        )

    def next_slot(self):
        # Trial records are authoritative: a crash between record and checkpoint is harmless.
        resolved = {
            row["slot"] for row in self.trials() if row.get("resolution") in {"accepted", "skipped"}
        }
        for slot in range(len(self.plan.points()) * self.plan.repetitions):
            if slot not in resolved:
                return slot
        return len(self.plan.points()) * self.plan.repetitions

    def checkpoint(self, slot, state):
        atomic_json(
            self.directory / "progress.json",
            {
                "schema_version": SCHEMA_VERSION,
                "slot": slot,
                "state": state,
                "updated_utc": utc_now(),
            },
        )

    def interrupt_unfinished(self):
        for record in self.trials():
            if record["status"] not in {"completed", "failed", "aborted", "interrupted"}:
                record.update(status="interrupted", error="Anwendung vor Abschluss beendet.")
                self.save_trial(record)

    def finish(self, record, result):
        """Archive a completed camera result on an IO worker; never alter source clips."""
        record = copy.deepcopy(record)
        record.update(
            status=result["status"],
            finished_utc=utc_now(),
            camera_status=result["status"],
            error=result.get("error"),
            stop_reason=result.get("stop_reason"),
        )
        rows = result.get("measurements") or []
        manifest = None
        source_name = result.get("clip_path")
        if source_name:
            source = Path(source_name).resolve()
            manifest = read_json(source / "manifest.json")
            if manifest.get("schema_version") != 1:
                raise ValueError("Unbekanntes Kamera-Aufnahmeformat.")
            metadata = manifest.get("metadata", {})
            if metadata.get("trial_id") != record["trial_id"] or (
                metadata.get("run_id") != self.campaign_id
            ):
                raise ValueError("Kameraaufnahme gehört nicht zu diesem Versuch.")
            frames = manifest["frames"]
            if len(frames) != manifest["frame_count"] or not frames:
                raise ValueError("Kameraaufnahme ist unvollständig.")
            target = self.trial_directory(record["trial_id"]) / "clip"
            staging = target.with_name("clip-" + uuid.uuid4().hex + ".tmp")
            staging.mkdir(parents=True, exist_ok=False)
            for frame in frames:
                source_file = inside(source, frame["file"])
                destination = inside(staging, frame["file"])
                if source_file.suffix.lower() != ".npy":
                    raise ValueError("Die Kameraaufnahme enthält ein unbekanntes Bildformat.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, destination)
            atomic_json(staging / "manifest.json", manifest)
            staging.rename(target)
            record.update(clip="clip", original_clip_path=str(source), camera_manifest=manifest)
            rows = [
                {
                    "timestamp": frame["timestamp"],
                    "frame_id": frame.get("frame_id"),
                    **frame["measurement"],
                }
                for frame in frames
            ]
        completed = (
            bool(manifest)
            and result["status"] == "completed"
            and (not manifest.get("aborted") and manifest.get("stop_reason") == "part_left")
        )
        dropped = (manifest or {}).get("dropped_frames", 0)
        evaluation, samples = evaluate(
            rows, self.plan.classification, completed=completed, dropped_frames=dropped
        )
        record["evaluation"] = evaluation
        record["resolution"] = None
        write_samples(self.trial_directory(record["trial_id"]) / "measurements.csv", samples)
        self.save_trial(record)
        return record

    def annotate(self, trial_id, label, excluded, reason):
        annotation = ReviewAnnotation(label, excluded, reason, utc_now())
        annotation.validate()
        directory = self.trial_directory(trial_id)
        read_json(directory / "trial.json")  # Cannot annotate a nonexistent attempt.
        path = directory / "annotations.json"
        document = read_json(path) if path.exists() else {"schema_version": 1, "history": []}
        document["history"].append(asdict(annotation))
        atomic_json(path, document)

    def annotation(self, trial_id):
        path = self.trial_directory(trial_id) / "annotations.json"
        return read_json(path)["history"][-1] if path.exists() else None
