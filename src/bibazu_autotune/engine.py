"""UI-independent, explicitly stepped trial state machine. No conveyor commands."""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .clients import ApiFailure


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
    os.replace(temporary, path)


class Strategy(Protocol):
    def suggest(self, baseline: dict) -> dict | None: ...
    def observe(self, result: dict) -> None: ...


class RecipeStrategy:
    def __init__(self, variants):
        self.variants = iter(copy.deepcopy(variants))
        self.results = []

    def suggest(self, baseline):
        return next(self.variants, None)

    def observe(self, result):
        self.results.append(copy.deepcopy(result))


class Engine:
    def __init__(self, pressure, vision, output_dir, emit=lambda *args: None, clock=time.monotonic):
        self.pressure, self.vision = pressure, vision
        self.output_dir, self.emit, self.clock = Path(output_dir), emit, clock
        self.state = "disconnected"
        self.operation_id = None
        self.operation_started = 0
        self.current = None
        self.context = None
        self.baseline = None
        self.run_id = None
        self.cursor = 0
        self.last_heartbeat = 0
        self.record = None
        self.save_parameters = None

    def change(self, state):
        self.state = state
        self.emit("state", state)

    def connect(self):
        if self.state != "disconnected":
            raise ApiFailure("Sitzung zuerst beenden.")
        try:
            session = self.pressure.acquire()
            self.vision.acquire()
            self.last_heartbeat = self.clock()
            self.operation_id = session["snapshot_operation_id"]
            self.operation_started = self.clock()
            self.change("connecting")
        except Exception:
            self.release()
            raise

    def start(self, parameters):
        if self.state != "ready":
            raise ApiFailure("Vorherigen Versuch zuerst abschließen.")
        self.record = {
            "run_id": self.run_id,
            "trial_id": uuid.uuid4().hex,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "parameters": copy.deepcopy(parameters),
            "roadmap_transition": self.context,
            "status": "applying",
            "live_overflow": False,
        }
        self.current = self.record["trial_id"]
        self.persist()
        self.operation_id = self.pressure.apply(parameters)["operation_id"]
        self.operation_started = self.clock()
        self.change("applying")

    def trigger(self):
        if self.state != "waiting":
            raise ApiFailure("Kamera ist noch nicht scharfgeschaltet.")
        return self.vision.trigger(self.current)

    def save(self, parameters):
        if self.state != "ready":
            raise ApiFailure("Versuch zuerst abschließen.")
        self.save_parameters = copy.deepcopy(parameters)
        self.operation_id = self.pressure.apply(parameters)["operation_id"]
        self.operation_started = self.clock()
        self.change("selecting")

    def persist(self):
        if self.record:
            atomic_json(
                self.output_dir / self.run_id / f"{self.record['trial_id']}.json", self.record
            )

    def tick(self):
        if self.state == "disconnected":
            return
        if self.clock() - self.last_heartbeat >= 5:
            self.pressure.heartbeat()
            self.vision.heartbeat()
            self.last_heartbeat = self.clock()
        if self.state in {"connecting", "applying", "selecting", "save_dialog"}:
            result = self.pressure.operation(self.operation_id)
            status = result["status"]
            if status in {"pending", "awaiting_user"}:
                if status == "pending" and self.clock() - self.operation_started > 15:
                    raise ApiFailure(
                        "SPS-Operation antwortet nicht; keine automatische Wiederholung."
                    )
                return
            if status not in {"succeeded", "cancelled"}:
                raise ApiFailure(result.get("error", "SPS-Operation fehlgeschlagen"))
            if self.state == "connecting":
                state = self.pressure.state()
                self.context = state["roadmap_transition"]
                self.baseline = state["parameters"]
                if not self.context or not self.baseline:
                    raise ApiFailure("Transition oder Parametersatz fehlt.")
                self.run_id = uuid.uuid4().hex
                self.cursor = self.vision.measurements()["latest_cursor"]
                atomic_json(
                    self.output_dir / self.run_id / "run.json",
                    {
                        "schema_version": 1,
                        "run_id": self.run_id,
                        "created_utc": datetime.now(timezone.utc).isoformat(),
                        "roadmap_transition": self.context,
                        "baseline": self.baseline,
                    },
                )
                self.emit("connected", {"parameters": self.baseline, "context": self.context})
                self.change("ready")
            elif self.state == "applying":
                camera_state = self.vision.state()
                if camera_state["stale_seconds"] > 1:
                    raise ApiFailure("Keine aktuellen Kamerabilder.")
                self.vision.start(
                    self.run_id,
                    self.current,
                    {
                        "roadmap_transition": self.context,
                        "parameters": self.record["parameters"],
                    },
                )
                self.record.update(status="waiting", camera_state=camera_state)
                self.emit("trial_mode", camera_state["capture_config"]["start_mode"])
                self.persist()
                self.change("waiting")
            elif self.state == "selecting":
                self.operation_id = self.pressure.save_dialog()["operation_id"]
                self.operation_started = self.clock()
                self.change("save_dialog")
            else:
                self.emit("saved", result)
                self.change("ready")
            return
        if self.state in {"waiting", "recording", "saving", "aborting"}:
            batch = self.vision.measurements(self.cursor)
            self.cursor = batch["next_cursor"]
            self.record["live_overflow"] |= batch["overflow"]
            self.emit(
                "measurements", [s for s in batch["measurements"] if s["trial_id"] == self.current]
            )
            trial = self.vision.trial(self.current)
            if trial["status"] in {"completed", "failed", "aborted"}:
                self.record.update(status=trial["status"], result=trial)
                self.persist()
                self.emit("result", copy.deepcopy(self.record))
                self.current = None
                self.change("ready")
            elif trial["status"] in {"recording", "saving"} and self.state != "aborting":
                if self.state != trial["status"]:
                    self.change(trial["status"])

    def abort(self):
        if self.current and self.state in {"waiting", "recording", "saving"}:
            self.vision.abort(self.current)
            self.change("aborting")
        else:
            # Pending writes cannot be recalled; invalidate the session so a
            # command still queued in Qt/ADS cannot execute later.
            self.release()

    def fail(self, error):
        if self.record and self.current:
            self.record.update(status="failed", error=str(error))
            try:
                self.persist()
            except OSError:
                pass
            self.emit("result", copy.deepcopy(self.record))
        self.emit("error", str(error))
        self.release()

    def release(self):
        if self.current:
            try:
                self.vision.abort(self.current)
            except Exception:
                pass
            if self.record and self.record["status"] not in {"completed", "failed", "aborted"}:
                self.record.update(status="aborted", error="Sitzung beendet")
                try:
                    self.persist()
                except OSError:
                    pass
        for client in (self.vision, self.pressure):
            try:
                client.release()
            except Exception:
                pass
        self.current = None
        self.change("disconnected")
