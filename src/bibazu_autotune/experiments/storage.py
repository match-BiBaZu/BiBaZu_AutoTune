"""Portable campaign folders, atomic records and auditable review annotations."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..engine import atomic_json
from . import SCHEMA_VERSION
from .classification import evaluate
from .models import ExperimentPlan, ReviewAnnotation, current_review_reason


class InsufficientSpaceError(OSError):
    pass


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
    def storage_bytes(self):
        return sum(path.stat().st_size for path in self.directory.rglob("*") if path.is_file())

    def reviewed_videos(self):
        result = []
        for record in self.trials():
            if self.annotation(record["trial_id"]) is None:
                continue
            directory = self.trial_directory(record["trial_id"])
            clip = inside(directory, record.get("clip") or "clip")
            manifest_path = clip / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest = read_json(manifest_path)
            media = manifest.get("media", {})
            if manifest.get("schema_version") != 2 or media.get("file") != "review.mp4":
                continue
            literal = clip / "review.mp4"
            if literal.is_symlink() or getattr(literal, "is_junction", lambda: False)():
                continue
            video = inside(clip, "review.mp4")
            if video != literal:
                continue
            if video.is_file() and media.get("state") in {"available", "deleting", "failed"}:
                result.append(
                    {"trial_id": record["trial_id"], "path": video, "bytes": video.stat().st_size}
                )
        return result

    def delete_review_video(self, trial_id):
        candidates = {v["trial_id"]: v for v in self.reviewed_videos()}
        if trial_id not in candidates:
            raise ValueError("Nur vorhandene Prüfvideos mit gespeicherter Prüfung löschen.")
        video = candidates[trial_id]["path"]
        if video.name != "review.mp4" or video.is_symlink() or video.resolve() != video:
            raise ValueError("Ungültiges Prüfvideo-Löschziel.")
        manifest_path = video.parent / "manifest.json"
        manifest = read_json(manifest_path)
        if manifest.get("metadata", {}).get("trial_id") != trial_id or (
            manifest.get("metadata", {}).get("run_id") != self.campaign_id
        ):
            raise ValueError("Prüfvideo gehört nicht zu dieser Kampagne.")
        # Journal intent first. Recovery distinguishes missing media from missing measurements.
        previous_state = manifest["media"]["state"]
        manifest["media"].update(state="deleting", deletion_requested_utc=utc_now())
        atomic_json(manifest_path, manifest)
        try:
            video.unlink()
        except OSError:
            manifest["media"]["state"] = previous_state
            atomic_json(manifest_path, manifest)
            raise
        manifest["media"].update(state="deleted", deleted_utc=utc_now())
        atomic_json(manifest_path, manifest)
        return candidates[trial_id]["bytes"]

    def delete(self):
        """Delete exactly this identified campaign; never follow linked directories."""
        target = self.directory.resolve(strict=True)
        if not re.fullmatch(r"[0-9a-f]{32}", self.campaign_id) or target.name != self.campaign_id:
            raise ValueError("Nur ein eindeutig benannter Kampagnenordner darf gelöscht werden.")
        manifest = read_json(target / "campaign.json")
        if manifest.get("campaign_id") != self.campaign_id or manifest.get("schema_version") != 1:
            raise ValueError("Kampagnenkennung stimmt nicht mit dem Löschziel überein.")
        for directory, folders, files in os.walk(target, followlinks=False):
            for name in (*folders, *files):
                entry = Path(directory) / name
                if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                    raise ValueError("Verknüpfungen im Kampagnenordner verhindern das Löschen.")
        shutil.rmtree(target)

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

    def reserve_capture(self, record):
        directory = self.trial_directory(record["trial_id"])
        self.check_space()
        if self.plan.storage_profile == "review_video_v1":
            atomic_json(
                directory / "capture-request.json",
                {
                    "format": "review_video_v1",
                    "run_id": self.campaign_id,
                    "trial_id": record["trial_id"],
                },
            )
        return directory

    def check_space(self):
        if shutil.disk_usage(self.directory).free < 1024**3:
            raise InsufficientSpaceError("Weniger als 1 GiB freier Speicher: Aufnahme pausiert.")

    def trials(self):
        return sorted(
            (read_json(path) for path in self.directory.glob("trials/*/trial.json")),
            key=lambda row: (row["created_utc"], row["trial_id"]),
        )

    def next_slot(self):
        # Trial records are authoritative: a crash between record and checkpoint is harmless.
        resolved = {
            row["slot"]
            for row in self.trials()
            if row.get("resolution") in {"accepted", "skipped", "uncertain"}
        }
        for slot in range(len(self.plan.points()) * self.plan.repetitions):
            if slot not in resolved:
                return slot
        return len(self.plan.points()) * self.plan.repetitions

    def pending_repeats(self, point_index=None):
        records = self.trials()
        resolved = {r["slot"] for r in records if r.get("resolution") in {"accepted", "skipped"}}
        latest = {}
        for record in records:
            if record["slot"] not in resolved and record.get("resolution") == "uncertain":
                if point_index is None or record["point_index"] == point_index:
                    latest[record["slot"]] = record
        pending = []
        for slot in sorted(latest):
            record = latest[slot]
            annotation = self.annotation(record["trial_id"])
            if annotation and (annotation["excluded"] or annotation["label"] is not None):
                # Resolve the attempt without changing its original automatic result.
                continue
            pending.append(record)
        return pending

    def checkpoint(self, slot, state, *, scheduler=None):
        atomic_json(
            self.directory / "progress.json",
            {
                "schema_version": SCHEMA_VERSION,
                "slot": slot,
                "state": state,
                "updated_utc": utc_now(),
                **({"scheduler": scheduler} if scheduler is not None else {}),
            },
        )

    def interrupt_unfinished(self):
        for record in self.trials():
            if record.get("storage_profile") == "review_video_v1" and not record.get("clip"):
                from .video import recover_pending

                recovered = recover_pending(self.trial_directory(record["trial_id"]))
                if recovered:
                    self.finish(
                        record,
                        {
                            "status": "aborted",
                            "clip_path": str(recovered),
                            "error": "Sitzung vor Abschluss unterbrochen",
                        },
                    )
                    continue
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
            if manifest.get("schema_version") not in (1, 2):
                raise ValueError("Unbekanntes Kamera-Aufnahmeformat.")
            metadata = manifest.get("metadata", {})
            if metadata.get("trial_id") != record["trial_id"] or (
                metadata.get("run_id") != self.campaign_id
            ):
                raise ValueError("Kameraaufnahme gehört nicht zu diesem Versuch.")
            frames = manifest["frames"]
            if len(frames) != manifest["frame_count"] or (
                not frames and manifest["schema_version"] == 1
            ):
                raise ValueError("Kameraaufnahme ist unvollständig.")
            target = self.trial_directory(record["trial_id"]) / "clip"
            if manifest["schema_version"] == 2:
                if source != target and source != target.with_name("clip.tmp"):
                    raise ValueError("Prüfvideo liegt nicht im reservierten Versuchsordner.")
                if source.name == "clip.tmp":
                    from .video import finalize_pending

                    source = finalize_pending(source)
                media = manifest["media"]
                if media.get("file") != "review.mp4":
                    raise ValueError("Ungültiger Prüfvideo-Dateiname.")
                record.update(clip=source.name, storage_profile="review_video_v1", media=media)
            else:
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
                record.update(clip="clip", storage_profile="legacy_npy")
            record.update(original_clip_path=str(source))
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
        evaluation_stop_reason = (manifest or {}).get("stop_reason", result.get("stop_reason"))
        if result["status"] == "aborted" or result.get("error"):
            # External cancellation or IO errors must never become accepted memory cutoffs.
            evaluation_stop_reason = None
        evaluation, samples = evaluate(
            rows,
            self.plan.classification,
            completed=completed,
            dropped_frames=dropped,
            stop_reason=evaluation_stop_reason,
        )
        record["evaluation"] = evaluation
        record["resolution"] = None
        write_samples(self.trial_directory(record["trial_id"]) / "measurements.csv", samples)
        self.save_trial(record)
        return record

    def annotate(self, trial_id, label, excluded, reason):
        annotation = ReviewAnnotation(
            label, excluded, current_review_reason(reason, excluded), utc_now()
        )
        annotation.validate()
        directory = self.trial_directory(trial_id)
        read_json(directory / "trial.json")  # Cannot annotate a nonexistent attempt.
        path = directory / "annotations.json"
        document = read_json(path) if path.exists() else {"schema_version": 1, "history": []}
        if document["history"] and all(
            document["history"][-1][key] == getattr(annotation, key)
            for key in ("label", "excluded", "reason")
        ):
            return
        document["history"].append(asdict(annotation))
        atomic_json(path, document)

    def annotation(self, trial_id):
        path = self.trial_directory(trial_id) / "annotations.json"
        if not path.exists():
            return None
        annotation = read_json(path)["history"][-1]
        annotation["reason"] = current_review_reason(annotation["reason"], annotation["excluded"])
        return annotation
