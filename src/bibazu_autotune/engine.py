"""UI-independent trial orchestration; every conveyor start is explicitly requested."""

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
from .profiles import make_profile
from .recipes import transition_identity, validate_parameters


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
        self.save_path = None
        self.profile_template = None
        self.conveyor_operation = None
        self.conveyor_started = 0
        self.last_pressure_poll = -1
        self.camera_armed = False
        self.vision_connected = False

    def set_context(self, context):
        if self.state not in {"disconnected", "ready"}:
            raise ApiFailure("Transition kann während eines Versuchs nicht geändert werden.")
        if transition_identity(context) == transition_identity(self.context):
            self.context = copy.deepcopy(context)
            return
        self.context = copy.deepcopy(context)
        self.run_id = None
        self.record = None
        if self.state == "ready":
            self.baseline = self.pressure.state()["parameters"]
            self.new_run()

    def new_run(self):
        if not self.context:
            return
        self.run_id = uuid.uuid4().hex
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

    def change(self, state):
        self.state = state
        self.emit("state", state)

    def connect(self):
        if self.state != "disconnected":
            raise ApiFailure("Sitzung zuerst beenden.")
        try:
            session = self.pressure.acquire()
            self.last_heartbeat = self.clock()
            self.operation_id = session["snapshot_operation_id"]
            self.operation_started = self.clock()
            self.change("connecting")
        except Exception:
            self.release()
            raise

    def connect_camera(self):
        if self.state != "ready":
            raise ApiFailure("SPS zuerst verbinden und Versuch abschließen.")
        try:
            if not self.vision.session_id:
                self.vision.acquire()
            self.cursor = self.vision.measurements()["latest_cursor"]
        except ApiFailure as exc:
            self.disconnect_camera(str(exc))
            return False
        self.vision_connected = True
        self.emit("camera_connection", {"connected": True})
        return True

    def disconnect_camera(self, error=None):
        try:
            self.vision.release()
        except ApiFailure:
            pass
        self.vision_connected = False
        self.camera_armed = False
        self.emit("camera_armed", False)
        self.emit("camera_connection", {"connected": False, "error": error})

    def start(self, parameters):
        if self.state != "ready":
            raise ApiFailure("Vorherigen Versuch zuerst abschließen.")
        if not self.vision_connected:
            raise ApiFailure("Kamera zuerst über Kamera verbinden anschließen.")
        if not self.context or not self.run_id:
            raise ApiFailure("Zuerst eine Roadmap-Transition in AutoTune auswählen.")
        parameters = validate_parameters(parameters)
        self.camera_armed = False
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
        if self.state != "waiting" or not self.camera_armed:
            raise ApiFailure("Kamera ist noch nicht scharfgeschaltet.")
        return self.vision.trigger(self.current)

    def apply_parameters(self, parameters):
        if self.state != "ready" or self.conveyor_operation:
            raise ApiFailure("Laufende Operation zuerst abschließen.")
        parameters = validate_parameters(parameters)
        self.operation_id = self.pressure.apply(parameters)["operation_id"]
        self.operation_started = self.clock()
        self.change("updating")

    def save(self, parameters, path, template=None):
        if self.state != "ready":
            raise ApiFailure("Versuch zuerst abschließen.")
        if not path:
            return
        if not self.context:
            raise ApiFailure("Roadmap-Transition fehlt.")
        self.save_parameters = validate_parameters(parameters)
        self.save_path = Path(path)
        self.profile_template = copy.deepcopy(template)
        self.operation_id = self.pressure.apply(parameters)["operation_id"]
        self.operation_started = self.clock()
        self.change("selecting")

    def conveyor(self, start):
        if start:
            allowed = self.state in {"ready", "recording"} or (
                self.state == "waiting" and self.camera_armed
            )
            if not allowed or self.conveyor_operation:
                raise ApiFailure("Bandstart erst nach Abschluss der laufenden Operation.")
            result = self.pressure.start_conveyor()
        else:
            result = self.pressure.stop_conveyor()
        self.conveyor_operation = result["operation_id"]
        self.conveyor_started = self.clock()
        self.emit("conveyor_pending", True)

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
            if self.vision_connected:
                try:
                    self.vision.heartbeat()
                except ApiFailure as exc:
                    if self.state not in {"connecting", "ready", "selecting", "updating"}:
                        raise
                    self.disconnect_camera(str(exc))
            self.last_heartbeat = self.clock()
        if self.clock() - self.last_pressure_poll >= 0.5:
            self.emit("pressure_state", self.pressure.state())
            self.last_pressure_poll = self.clock()
        if self.conveyor_operation:
            motor = self.pressure.operation(self.conveyor_operation)
            if motor["status"] != "pending":
                self.conveyor_operation = None
                self.emit("conveyor_pending", False)
                if motor["status"] != "succeeded":
                    raise ApiFailure(motor.get("error", "Bandoperation fehlgeschlagen"))
            elif self.clock() - self.conveyor_started > 30:
                raise ApiFailure("Bandoperation antwortet nicht.")
        if self.state in {"connecting", "applying", "selecting", "updating"}:
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
                self.baseline = state["parameters"]
                if not self.baseline:
                    raise ApiFailure("Parametersatz fehlt.")
                self.new_run()
                self.emit("connected", {"parameters": self.baseline, "context": self.context})
                self.change("ready")
                self.connect_camera()
            elif self.state == "updating":
                snapshot = result.get("snapshot") or self.pressure.state()
                self.emit("parameters_applied", snapshot["parameters"])
                self.emit("pressure_state", self.pressure.state())
                self.change("ready")
            elif self.state == "applying":
                camera_state = self.vision.state()
                if camera_state["stale_seconds"] > 1:
                    raise ApiFailure("Keine aktuellen Kamerabilder.")
                trial = self.vision.start(
                    self.run_id,
                    self.current,
                    {
                        "roadmap_transition": self.context,
                        "parameters": self.record["parameters"],
                    },
                )
                self.record.update(status="waiting", camera_state=camera_state)
                self.emit("trial_mode", camera_state["capture_config"]["start_mode"])
                self.camera_armed = trial.get("status") == "armed"
                self.emit("camera_armed", self.camera_armed)
                self.persist()
                self.change("waiting")
            elif self.state == "selecting":
                actual = self.pressure.state()
                try:
                    atomic_json(
                        self.save_path,
                        make_profile(
                            actual["parameters"],
                            self.context,
                            actual.get("machine_metadata"),
                            self.profile_template,
                            actual.get("conveyor"),
                        ),
                    )
                except (OSError, ValueError) as exc:
                    self.emit(
                        "error", "Parameter übernommen, Profil NICHT gespeichert: " + str(exc)
                    )
                else:
                    self.emit("saved", {"status": "succeeded", "profile_path": str(self.save_path)})
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
            self.camera_armed = trial["status"] == "armed" and self.state != "aborting"
            self.emit("camera_armed", self.camera_armed)
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
        self.camera_armed = False
        self.vision_connected = False
        self.conveyor_operation = None
        self.emit("camera_armed", False)
        self.emit("camera_connection", {"connected": False})
        self.emit("conveyor_pending", False)
        self.change("disconnected")
