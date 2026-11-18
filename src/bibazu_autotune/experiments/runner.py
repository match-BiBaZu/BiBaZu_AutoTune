"""One-shot acquisition state machine. Only explicit conveyor commands start the belt."""

from __future__ import annotations

import copy
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from ..clients import ApiFailure
from ..recipes import validate_parameters
from .models import TrialRecord
from .storage import CampaignStore, utc_now

CAPTURE_STATES = {"arming", "waiting", "recording", "saving"}
TERMINAL = {"completed", "failed", "aborted"}


def parameters_match(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            parameters_match(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            parameters_match(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(left - right) <= 1e-4
    return left == right


def camera_signature(state):
    return {
        key: copy.deepcopy(state.get(key))
        for key in (
            "processing",
            "camera_geometry",
            "has_background",
            "capture_config",
            "demo",
        )
    }


class ExperimentRunner:
    def __init__(self, pressure, vision, emit=lambda *args: None, clock=time.monotonic):
        self.pressure, self.vision, self.emit, self.clock = pressure, vision, emit, clock
        self.state = "disconnected"
        self.store = None
        self.slot = 0
        self.record = None
        self.operation = self.motor_operation = None
        self.operation_started = self.motor_started = self.last_heartbeat = 0
        self.last_poll = -1
        self.deadline = 0
        self.pause_requested = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="experiment-archive")
        self.archive = None
        self.cursor = 0
        self.replaces = None

    def change(self, state):
        self.state = state
        self.emit("state", state)
        if self.store:
            try:
                self.store.checkpoint(self.slot, state)
            except OSError as exc:
                if state != "disconnected":
                    raise
                self.emit("error", "Fortschritt konnte nicht gespeichert werden: " + str(exc))
            total = len(self.store.plan.points()) * self.store.plan.repetitions
            point = self.store.plan.points()[
                min(self.slot // self.store.plan.repetitions, len(self.store.plan.points()) - 1)
            ]
            self.emit(
                "progress",
                {
                    "slot": self.slot,
                    "total": total,
                    "point": asdict(point),
                    "repetition": self.slot % self.store.plan.repetitions + 1,
                    "directory": str(self.store.directory),
                    "can_skip": bool(self.record and self.record["slot"] == self.slot),
                },
            )

    def connect(self):
        if self.state != "disconnected":
            raise ApiFailure("Sitzung ist bereits verbunden.")
        response = self.pressure.acquire()
        self.operation = response["snapshot_operation_id"]
        self.operation_started = self.last_heartbeat = self.clock()
        self.change("connecting")

    def check_camera(self):
        state = self.vision.state()
        if state.get("stale_seconds", 99) > 1:
            raise ApiFailure("Keine aktuellen Kamerabilder.")
        if state.get("demo") and not getattr(self.pressure, "simulated", False):
            raise ApiFailure("Demo-Kamera ist für eine reale Messkampagne nicht zugelassen.")
        if self.store and camera_signature(state) != self.store.manifest["metadata"]["camera"]:
            raise ApiFailure("Kamera-/Messkonfiguration geändert; neue Kampagne anlegen.")
        return state

    def create(self, root, plan, assets, metadata):
        if self.state not in {"ready", "complete"} or self.operation or self.motor_operation:
            raise ApiFailure("Zuerst verbinden und laufende Operationen abschließen.")
        camera = self.check_camera_without_campaign()
        self.store = CampaignStore.create(
            root,
            plan,
            assets=assets,
            metadata={**metadata, "camera": camera_signature(camera), "plc": self.pressure.state()},
        )
        self.slot, self.record, self.replaces = 0, None, None
        self.pause_requested = False
        self.emit("campaign", str(self.store.directory))
        self.disable("gate")

    def check_camera_without_campaign(self):
        previous, self.store = self.store, None
        try:
            return self.check_camera()
        finally:
            self.store = previous

    def resume(self, directory):
        if self.state not in {"ready", "complete"} or self.operation or self.motor_operation:
            raise ApiFailure("Zuerst verbinden und laufende Operationen abschließen.")
        self.store = CampaignStore(directory)
        self.store.verify_assets()
        self.check_camera()
        self.store.interrupt_unfinished()
        self.slot = self.store.next_slot()
        pending = [r for r in self.store.trials() if r["slot"] == self.slot]
        self.replaces = pending[-1]["trial_id"] if pending else None
        self.record = None
        self.pause_requested = False
        self.emit("campaign", str(self.store.directory))
        self.disable("complete" if self.finished() else "gate")

    def finished(self):
        return self.slot >= len(self.store.plan.points()) * self.store.plan.repetitions

    def point(self):
        plan = self.store.plan
        return plan.points()[min(self.slot // plan.repetitions, len(plan.points()) - 1)]

    def apply(self, parameters, next_state):
        if self.operation or self.motor_operation:
            raise ApiFailure("SPS-Operation läuft noch.")
        self.operation = self.pressure.apply(parameters)["operation_id"]
        self.expected_parameters = parameters
        self.operation_started = self.clock()
        self.after_operation = next_state
        self.change("applying" if next_state == "settling" else "disabling")

    def disable(self, next_state):
        parameters = self.store.plan.parameters_at(self.point(), enabled=False)
        self.apply(parameters, next_state)

    def confirm(self):
        if self.state != "gate" or self.operation or self.motor_operation or self.finished():
            raise ApiFailure("Keine neue Reihe zur Freigabe bereit.")
        self.check_camera()
        self.pause_requested = False
        self.begin_attempt(new_combination=True)

    def begin_attempt(self, *, new_combination):
        self.check_camera()
        point = self.point()
        parameters = self.store.plan.parameters_at(point)
        self.record = asdict(
            TrialRecord(
                self.store.campaign_id,
                uuid.uuid4().hex,
                point.index,
                self.slot % self.store.plan.repetitions + 1,
                utc_now(),
                parameters,
                replaces_trial_id=self.replaces,
            )
        )
        self.record.update(
            schema_version=1,
            slot=self.slot,
            analytical=self.store.plan.snapshot.get("analytical_by_duration", {}).get(
                str(point.pulse_duration_ms), {}
            ),
            settling_seconds=self.store.plan.settling_seconds if new_combination else 0,
        )
        self.store.save_trial(self.record)
        self.replaces = None
        if new_combination:
            self.apply(parameters, "settling")
        else:
            actual = self.pressure.state()["parameters"]
            if not parameters_match(actual, parameters):
                raise ApiFailure("SPS-Parameter wurden außerhalb der Messreihe geändert.")
            self.record["readback_parameters"] = copy.deepcopy(actual)
            self.arm()

    def arm(self):
        if self.pause_requested:
            self.pause()
            return
        state = self.check_camera()
        self.emit("capture_mode", state["capture_config"]["start_mode"])
        self.record.update(status="arming", camera_state=state)
        self.store.save_trial(self.record)
        self.change("arming")
        self.vision.start(
            self.store.campaign_id,
            self.record["trial_id"],
            {
                "experiment_schema_version": 1,
                "slot": self.slot,
                "transition": self.store.plan.context,
                "parameters": self.record["requested_parameters"],
                "target_angle_deg": self.store.plan.classification.target_angle_deg,
                "camera_direction": self.store.plan.classification.camera_direction,
            },
            self.store.plan.trial_timeout_seconds,
        )
        self.deadline = self.clock() + self.store.plan.trial_timeout_seconds + 30

    def trigger(self):
        if (
            self.state != "waiting"
            or self.record["camera_state"]["capture_config"]["start_mode"] != "manual"
        ):
            raise ApiFailure("Manueller Trigger nur bei scharfer Kamera im manuellen Modus.")
        self.vision.trigger(self.record["trial_id"])

    def pause(self):
        if self.state in {"disconnected", "connecting", "ready", "complete", "paused"}:
            return
        self.pause_requested = True
        if self.state in CAPTURE_STATES:
            try:
                self.vision.abort(self.record["trial_id"])
            except ApiFailure:
                # Completion can race the abort request. Archive that result normally.
                if self.vision.trial(self.record["trial_id"])["status"] not in TERMINAL:
                    raise
        elif self.archive or self.operation or self.motor_operation:
            return
        else:
            if self.record and self.record["status"] not in TERMINAL:
                self.record.update(status="interrupted", error="Manuell pausiert.")
                self.store.save_trial(self.record)
            self.disable("paused")

    def retry(self):
        if self.state != "paused":
            raise ApiFailure("Wiederholung erst nach abgeschlossener Fehlerpause.")
        self.replaces = self.record["trial_id"] if self.record else self.replaces
        self.pause_requested = False
        self.state = "gate"
        self.confirm()

    def skip(self):
        if self.state != "paused" or not self.record:
            raise ApiFailure("Kein pausierter Einzelversuch vorhanden.")
        self.record["resolution"] = "skipped"
        self.store.save_trial(self.record)
        self.slot += 1
        self.record, self.replaces = None, None
        self.pause_requested = False
        self.change("complete" if self.finished() else "gate")

    def conveyor(self, start):
        if self.state in {"disconnected", "connecting"} or self.motor_operation:
            raise ApiFailure("Keine freie SPS-Verbindung.")
        if start and (
            self.operation
            or self.state
            not in {
                "ready",
                "gate",
                "paused",
                "waiting",
                "recording",
                "complete",
            }
        ):
            raise ApiFailure("Bandstart erst nach Abschluss der laufenden Operation.")
        response = self.pressure.start_conveyor() if start else self.pressure.stop_conveyor()
        self.motor_operation = response["operation_id"]
        self.motor_started = self.clock()
        self.emit("motor_pending", True)

    def tick(self):
        if self.state == "disconnected":
            return
        now = self.clock()
        if now - self.last_heartbeat >= 5:
            self.pressure.heartbeat()
            if self.vision.session_id:
                self.vision.heartbeat()
            self.last_heartbeat = now
        if now - self.last_poll >= 0.5:
            self.emit("pressure_state", self.pressure.state())
            self.last_poll = now
        if self.motor_operation:
            result = self.pressure.operation(self.motor_operation)
            if result["status"] != "pending":
                self.motor_operation = None
                self.emit("motor_pending", False)
                if result["status"] != "succeeded":
                    raise ApiFailure(result.get("error", "Bandoperation fehlgeschlagen."))
            elif now - self.motor_started > 30:
                raise ApiFailure("Bandoperation antwortet nicht.")
        if self.operation:
            result = self.pressure.operation(self.operation)
            if result["status"] == "pending":
                if now - self.operation_started > 15:
                    raise ApiFailure("SPS-Operation antwortet nicht; keine Wiederholung.")
                return
            self.operation = None
            if result["status"] != "succeeded":
                raise ApiFailure(result.get("error", "SPS-Operation fehlgeschlagen."))
            actual = (result.get("snapshot") or self.pressure.state())["parameters"]
            validate_parameters(actual)
            if self.state == "connecting":
                self.vision.acquire()
                self.check_camera_without_campaign()
                self.cursor = self.vision.measurements()["latest_cursor"]
                self.change("ready")
                return
            if not parameters_match(actual, self.expected_parameters):
                raise ApiFailure("SPS-Rücklesen stimmt nicht mit dem Versuchsplan überein.")
            next_state = self.after_operation
            if next_state == "settling":
                self.record["readback_parameters"] = copy.deepcopy(actual)
                self.record["parameters_confirmed_utc"] = utc_now()
                self.store.save_trial(self.record)
                self.deadline = now + self.store.plan.settling_seconds
            self.change(next_state)
        if self.pause_requested and self.state not in CAPTURE_STATES and not self.archive:
            self.pause()
            if self.state != "paused":
                return
        if self.state == "settling" and now >= self.deadline:
            self.arm()
        elif self.state in CAPTURE_STATES:
            batch = self.vision.measurements(self.cursor)
            self.cursor = batch["next_cursor"]
            self.emit(
                "measurements",
                [r for r in batch["measurements"] if r.get("trial_id") == self.record["trial_id"]],
            )
            trial = self.vision.trial(self.record["trial_id"])
            if trial["status"] in TERMINAL:
                self.record.update(status="archiving", camera_result_status=trial["status"])
                self.store.save_trial(self.record)
                self.archive = self.pool.submit(self.store.finish, self.record, trial)
                self.change("archiving")
            elif now > self.deadline:
                raise ApiFailure("Kamera-Versuch antwortet nicht mehr.")
            else:
                mapped = {"pending": "arming", "armed": "waiting"}.get(
                    trial["status"], trial["status"]
                )
                if mapped != self.state:
                    self.change(mapped)
        elif self.archive and self.archive.done():
            future, self.archive = self.archive, None
            self.record = future.result()
            self.emit("result", copy.deepcopy(self.record))
            if self.pause_requested or self.record["evaluation"]["quality"] != "valid":
                self.disable("paused")
            else:
                self.record["resolution"] = "accepted"
                self.store.save_trial(self.record)
                old_point = self.point().index
                self.slot += 1
                self.record = None
                if self.finished():
                    self.disable("complete")
                elif self.point().index != old_point:
                    self.disable("gate")
                else:
                    self.begin_attempt(new_combination=False)

    def fail(self, error):
        self.emit("error", str(error))
        self.release(error=str(error))

    def release(self, *, error="Sitzung beendet"):
        if self.record and self.state in CAPTURE_STATES:
            try:
                self.vision.abort(self.record["trial_id"])
            except Exception:
                pass
        if self.archive:
            # Preserve pending copies even when the connection is lost.
            try:
                self.record = self.archive.result()
            except Exception as exc:
                error += "; Archivierung: " + str(exc)
            self.archive = None
        if self.record and self.record["status"] not in TERMINAL:
            self.record.update(status="interrupted", error=error)
            try:
                self.store.save_trial(self.record)
            except OSError as exc:
                self.emit("error", "Versuchsstatus konnte nicht gespeichert werden: " + str(exc))
        if (
            self.store
            and self.pressure.session_id
            and self.state
            not in {
                "disconnected",
                "connecting",
                "ready",
                "complete",
            }
        ):
            try:
                operation = self.pressure.apply(
                    self.store.plan.parameters_at(self.point(), enabled=False)
                )["operation_id"]
                deadline = time.monotonic() + 2
                while True:
                    result = self.pressure.operation(operation)
                    if result["status"] == "succeeded":
                        break
                    if result["status"] != "pending" or time.monotonic() >= deadline:
                        raise ApiFailure("Array-Deaktivierung wurde nicht bestätigt.")
                    time.sleep(0.02)
            except Exception as exc:
                self.emit("error", "Array-Deaktivierung nicht bestätigt: " + str(exc))
        for client in (self.vision, self.pressure):
            try:
                client.release()
            except Exception:
                pass
        self.operation = self.motor_operation = None
        self.emit("motor_pending", False)
        self.change("disconnected")

    def close(self):
        self.release()
        self.pool.shutdown(wait=True)
