"""Reader for camera manifest v2; no import of the sibling camera repository."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .storage import inside


def finalize_pending(path):
    path = Path(path)
    if path.name != "clip.tmp":
        return path
    final = path.with_name("clip")
    if not path.exists() and final.is_dir():
        return final
    for delay in (0, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0):
        if delay:
            time.sleep(delay)
        if final.exists():
            raise FileExistsError("Aufnahmeziel bereits vorhanden: " + str(final))
        try:
            path.rename(final)
            return final
        except PermissionError:
            pass
    return path


def recover_pending(directory):
    """Camera journal v2 contract; old image recordings are never modified."""
    root = Path(directory).resolve()
    pending = root / "clip.tmp"
    try:
        marker = json.loads((root / "capture-request.json").read_text("utf-8"))
        # A crash may occur after publication but before the client saves trial.json.
        published = root / "clip"
        complete = pending if pending.is_dir() else published
        if (complete / "manifest.json").is_file():
            manifest = json.loads((complete / "manifest.json").read_text("utf-8"))
            metadata = manifest["metadata"]
            if manifest.get("schema_version") != 2 or marker != {
                "format": "review_video_v1",
                "run_id": metadata["run_id"],
                "trial_id": metadata["trial_id"],
            }:
                return None
            return finalize_pending(complete)
        if not pending.is_dir():
            return None
        manifest = json.loads((pending / "capture.json").read_text("utf-8"))
        metadata = manifest["metadata"]
        if marker != {
            "format": "review_video_v1",
            "run_id": metadata["run_id"],
            "trial_id": metadata["trial_id"],
        }:
            return None
        rows = []
        with (pending / "measurements.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    break
                if row.get("video_frame_index") != len(rows):
                    break
                rows.append(row)
        manifest.update(
            schema_version=2,
            stop_reason="write_error",
            aborted=True,
            end_timestamp=rows[-1]["timestamp"] if rows else manifest["trigger_timestamp"],
            frames=rows,
            frame_count=len(rows),
            dropped_frames=0,
            media={
                "state": "failed",
                "file": "review.mp4",
                "format": "mp4v",
                "error": "Unterbrochene Aufnahme aus Messjournal wiederhergestellt",
            },
        )
        from ..engine import atomic_json

        atomic_json(pending / "manifest.json", manifest)
        return finalize_pending(pending)
    except (OSError, ValueError, KeyError):
        return None


class VideoFrames:
    """Decode at most eight frames, then close the file so videos can be deleted."""

    def __init__(self, directory, manifest):
        self.directory = Path(directory)
        self.media = manifest["media"]
        self.count = (
            manifest["frame_count"]
            if self.media.get("state") in {"available", "deleting"}
            and (inside(self.directory, self.media["file"]).is_file())
            else 0
        )
        self.cache = {}

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        import cv2

        if not isinstance(index, int) or not 0 <= index < self.count:
            raise IndexError(index)
        if index not in self.cache:
            capture = cv2.VideoCapture(str(inside(self.directory, self.media["file"])))
            try:
                if not capture.isOpened() or not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                    raise ValueError("Prüfvideo kann nicht geöffnet werden.")
                self.cache.clear()
                for position in range(index, min(index + 8, self.count)):
                    ok, pixels = capture.read()
                    if not ok:
                        raise ValueError("Prüfvideo enthält fehlende Bilder.")
                    self.cache[position] = pixels
            finally:
                capture.release()
        return self.cache[index]

    def close(self):
        self.cache.clear()
