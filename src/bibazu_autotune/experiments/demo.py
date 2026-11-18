"""Deterministic local acquisition simulator. Never imports or contacts ADS hardware."""

from __future__ import annotations

import copy
import tempfile
import time
from pathlib import Path

import numpy as np

from ..engine import atomic_json
from ..profiles import empty_parameters


class DemoPressure:
    simulated = True

    def __init__(self):
        self.session_id = None
        self.parameters = empty_parameters()
        self.moving = False
        self.operations = {}

    def operation_result(self):
        key = str(len(self.operations))
        self.operations[key] = {"status": "succeeded", "snapshot": self.state()}
        return {"operation_id": key}

    def acquire(self):
        self.session_id = "demo-pressure"
        return {
            "session_id": self.session_id,
            "snapshot_operation_id": self.operation_result()["operation_id"],
        }

    def operation(self, key):
        return copy.deepcopy(self.operations[key])

    def apply(self, parameters):
        self.parameters = copy.deepcopy(parameters)
        return self.operation_result()

    def state(self):
        return {
            "connected": bool(self.session_id),
            "parameters": copy.deepcopy(self.parameters),
            "conveyor": {"moving": self.moving},
            "machine_metadata": {"simulated": True},
        }

    def heartbeat(self):
        return {}

    def start_conveyor(self):
        self.moving = True
        return self.operation_result()

    def stop_conveyor(self):
        self.moving = False
        return self.operation_result()

    def release(self):
        self.moving, self.session_id = False, None

    def close(self):
        pass


class DemoVision:
    def __init__(self, pressure, clock=time.monotonic):
        self.pressure, self.clock = pressure, clock
        self.session_id = None
        self.trials = {}
        self.temporary = tempfile.TemporaryDirectory(prefix="bibazu-experiment-demo-")

    def acquire(self):
        self.session_id = "demo-vision"
        return {"session_id": self.session_id}

    def state(self):
        return {
            "stale_seconds": 0,
            "demo": True,
            "processing": {},
            "has_background": True,
            "camera_geometry": {"width": 256, "height": 192},
            "capture_config": {"start_mode": "motion"},
        }

    def measurements(self, after=0):
        return {"measurements": [], "latest_cursor": 0, "next_cursor": 0, "overflow": False}

    def capabilities(self):
        return {
            "trial_start_mode_override": True,
            "review_video_v1": True,
            "direct_trial_output": True,
            "qualified_presence_start": True,
        }

    def start(
        self,
        run_id,
        trial_id,
        context,
        timeout_seconds=60,
        *,
        start_mode=None,
        storage_profile=None,
        output_directory=None,
        presence_confirm_seconds=None,
    ):
        self.trials[trial_id] = {
            "status": "armed",
            "run_id": run_id,
            "trial_id": trial_id,
            "context": context,
            "started": self.clock(),
            "storage_profile": storage_profile,
            "output_directory": output_directory,
            "presence_confirm_seconds": presence_confirm_seconds,
        }
        return self.trials[trial_id]

    def trial(self, trial_id):
        record = self.trials[trial_id]
        if (
            record["status"] == "armed"
            and self.pressure.moving
            and (self.clock() - record["started"] > 1)
        ):
            video_mode = record.get("storage_profile") == "review_video_v1"
            folder = (
                Path(record["output_directory"]) / "clip"
                if video_mode
                else Path(self.temporary.name) / trial_id
            )
            folder.mkdir()
            writer = None
            if video_mode:
                import cv2

                writer = cv2.VideoWriter(
                    str(folder / "review.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 50, (256, 192)
                )
                if not writer.isOpened():
                    raise RuntimeError("Demo-Videoencoder nicht verfügbar")
            selected = next(a for a in record["context"]["parameters"]["arrays"] if a["enabled"])
            target = record["context"].get("target_angle_deg", 90)
            direction = record["context"].get("camera_direction", 1)
            final = direction * target * selected["pressure_mbar"] / 2000
            angles = [0.0] * 6 + np.linspace(0, final, 17).tolist() + [final] * 6
            frames = []
            for index, angle in enumerate(angles):
                yy, xx = np.mgrid[:192, :256]
                radians = np.deg2rad(angle)
                along = (xx - 128) * np.cos(radians) + (yy - 96) * np.sin(radians)
                across = -(xx - 128) * np.sin(radians) + (yy - 96) * np.cos(radians)
                image = np.full((192, 256), 225, dtype=np.uint8)
                image[(abs(along) < 55) & (abs(across) < 17)] = 40
                filename = f"frame_{index:06d}.npy"
                if video_mode:
                    writer.write(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR))
                else:
                    np.save(folder / filename, image, allow_pickle=False)
                frames.append(
                    {
                        **({"video_frame_index": index} if video_mode else {"file": filename}),
                        "timestamp": index * 0.02,
                        "frame_id": index,
                        "measurement": {
                            "angle_deg": (angle + 90) % 180 - 90,
                            "present": True,
                            "touching_border": False,
                            "confidence": 1.0,
                        },
                    }
                )
            if writer is not None:
                writer.release()
            manifest = {
                "schema_version": 2 if video_mode else 1,
                "frame_count": len(frames),
                "frames": frames,
                "metadata": {"trial_id": trial_id, "run_id": record["run_id"]},
                "stop_reason": "part_left",
                "aborted": False,
                "dropped_frames": 0,
                **(
                    {
                        "media": {
                            "format": "mp4v",
                            "state": "available",
                            "file": "review.mp4",
                            "fps": 50,
                            "crop_xywh": [0, 0, 256, 192],
                            "image_size": [256, 192],
                            "scale_xy": [1, 1],
                        }
                    }
                    if video_mode
                    else {}
                ),
            }
            atomic_json(folder / "manifest.json", manifest)
            record.update(status="completed", stop_reason="part_left", clip_path=str(folder))
        return copy.deepcopy(record)

    def trigger(self, trial_id):
        pass

    def abort(self, trial_id):
        self.trials[trial_id].update(status="aborted", error="demo_abort")

    def heartbeat(self):
        return {}

    def release(self):
        self.session_id = None

    def close(self):
        self.temporary.cleanup()
