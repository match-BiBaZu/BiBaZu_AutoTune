"""One-shot acquisition state machine. Only explicit conveyor commands start the belt."""

from __future__ import annotations

import copy
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from ..clients import ApiFailure
from ..recipes import validate_parameters
from .models import TrialRecord
from .storage import CampaignStore, InsufficientSpaceError, read_json, utc_now

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
    signature = {
        key: copy.deepcopy(state.get(key))
        for key in (
            "processing",
            "camera_geometry",
            "has_background",
            "capture_config",
            "demo",
        )
    }
    capture = signature.get("capture_config")
    if isinstance(capture, dict) and not capture.get("presence_confirm_seconds", 0):
        # Old camera versions did not serialize the disabled optional guard.
        capture.pop("presence_confirm_seconds", None)
    return signature


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
        self.external_stop = lambda: False
        self.deferred_disable = None
        self.repeat_queue = []
        self.repeat_active = False
        self.return_slot = 0
        self.repeat_scope = None
        self.pending_repeat_slots = {}
        self.campaign_bytes = 0

    def change(self, state):
        self.state = state
        self.emit("state", state)
        if self.store:
            try:
                self.store.checkpoint(
                    self.slot,
                    state,
                    scheduler={
                        "repeat_queue": self.repeat_queue,
                        "repeat_active": self.repeat_active,
                        "return_slot": self.return_slot,
                        "repeat_scope": self.repeat_scope,
                    },
                )
            except OSError as exc:
                if state != "disconnected":
                    raise
                self.emit("error", "Fortschritt konnte nicht gespeichert werden: " + str(exc))
            self.emit_progress()

    def emit_progress(self):
        if not self.store:
            return
        total = len(self.store.plan.points()) * self.store.plan.repetitions
        self.emit(
            "progress",
            {
                "slot": self.slot,
                "total": total,
                "point": asdict(self.point()),
                "repetition": self.slot % self.store.plan.repetitions + 1,
                "directory": str(self.store.directory),
                "can_skip": bool(self.record and self.record["slot"] == self.slot),
                "sweep_slot": self.return_slot if self.repeat_active else self.slot,
                "repeat_active": self.repeat_active,
                "pending_repeats": len(self.pending_repeat_slots),
            },
        )

    def refresh_pending_repeats(self):
        self.pending_repeat_slots = {r["slot"]: r["trial_id"] for r in self.store.pending_repeats()}

    def prune_repeat_queue(self):
        """Only call between attempts; never remove the head of an active capture."""
        self.repeat_queue = [
            task
            for task in self.repeat_queue
            if self.pending_repeat_slots.get(task["slot"]) == task["trial_id"]
        ]
        if self.repeat_queue:
            self.slot = self.repeat_queue[0]["slot"]
            self.replaces = self.repeat_queue[0]["trial_id"]
        else:
            self.repeat_active = False
            self.slot = self.return_slot
            self.replaces = None

    def refresh_reviews(self, directory):
        if self.store is None or self.store.directory != Path(directory).resolve():
            return
        self.refresh_pending_repeats()
        if self.repeat_active and self.state == "gate":
            previous = list(self.repeat_queue)
            self.prune_repeat_queue()
            if previous != self.repeat_queue:
                self.record = None
                if self.repeat_active:
                    next_state = "gate"
                elif any(
                    self.repeat_scope is None
                    or slot // self.store.plan.repetitions == self.repeat_scope
                    for slot in self.pending_repeat_slots
                ):
                    next_state = "review_pending"
                else:
                    self.repeat_scope = None
                    next_state = "complete" if self.finished() else "gate"
                self.change(next_state)
                return
        if self.state == "review_pending" and not any(
            self.repeat_scope is None or slot // self.store.plan.repetitions == self.repeat_scope
            for slot in self.pending_repeat_slots
        ):
            self.repeat_scope = None
            self.change("complete" if self.finished() else "gate")
        else:
            self.emit_progress()

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
        if self.store and camera_signature(state) != camera_signature(
            self.store.manifest["metadata"]["camera"]
        ):
            raise ApiFailure("Kamera-/Messkonfiguration geändert; neue Kampagne anlegen.")
        if self.store:
            self.check_start_mode(self.store.plan.capture_start_mode)
            self.check_storage_profile(self.store.plan.storage_profile)
            self.check_presence_guard(self.store.plan)
        return state

    def check_start_mode(self, mode):
        if mode is not None and not self.vision.capabilities().get("trial_start_mode_override"):
            raise ApiFailure(
                "Kamera-API unterstützt die Durchlauferkennung noch nicht. "
                "Aktualisierte Kamera-GUI neu starten und erneut verbinden."
            )

    def refresh_storage(self, directory):
        if self.store and self.store.directory == Path(directory).resolve():
            self.campaign_bytes = self.store.storage_bytes()

    def check_presence_guard(self, plan):
        if plan.capture_start_mode == "presence" and plan.presence_confirm_seconds is not None:
            if not self.vision.capabilities().get("qualified_presence_start"):
                raise ApiFailure(
                    "Kamera-GUI für bestätigten Bauteilstart aktualisieren und neu starten."
                )

    def check_storage_profile(self, profile):
        if profile == "review_video_v1":
            capabilities = self.vision.capabilities()
            if not capabilities.get("review_video_v1") or not capabilities.get(
                "direct_trial_output"
            ):
                raise ApiFailure("Kamera-GUI für kompakte Videos aktualisieren und neu starten.")

    def create(self, root, plan, assets, metadata):
        if self.state not in {"ready", "complete"} or self.operation or self.motor_operation:
            raise ApiFailure("Zuerst verbinden und laufende Operationen abschließen.")
        camera = self.check_camera_without_campaign()
        self.check_start_mode(plan.capture_start_mode)
        self.check_storage_profile(plan.storage_profile)
        self.check_presence_guard(plan)
        self.store = CampaignStore.create(
            root,
            plan,
            assets=assets,
            metadata={**metadata, "camera": camera_signature(camera), "plc": self.pressure.state()},
        )
        self.slot, self.record, self.replaces = 0, None, None
        self.repeat_queue, self.repeat_active, self.return_slot, self.repeat_scope = (
            [],
            False,
            0,
            None,
        )
        self.pending_repeat_slots = {}
        self.campaign_bytes = self.store.storage_bytes()
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
        self.campaign_bytes = self.store.storage_bytes()
        self.refresh_pending_repeats()
        scheduler = read_json(self.store.directory / "progress.json").get("scheduler", {})
        self.repeat_active = bool(scheduler.get("repeat_active", False))
        self.repeat_queue = scheduler.get("repeat_queue", []) if self.repeat_active else []
        self.repeat_scope = scheduler.get("repeat_scope")
        self.return_slot = self.store.next_slot()
        records = self.store.trials()
        for task in self.repeat_queue:
            if not any(
                r["trial_id"] == task["trial_id"] and r["slot"] == task["slot"] for r in records
            ):
                raise ApiFailure("Wiederholungsauftrag gehört nicht zur Kampagne.")
        while self.repeat_queue and any(
            r.get("replaces_trial_id") == self.repeat_queue[0]["trial_id"]
            and r.get("resolution") in {"accepted", "uncertain", "skipped"}
            for r in records
        ):
            self.repeat_queue.pop(0)
        self.repeat_queue = [
            task
            for task in self.repeat_queue
            if self.pending_repeat_slots.get(task["slot"]) == task["trial_id"]
        ]
        self.slot = self.store.next_slot()
        if self.repeat_queue:
            self.slot = self.repeat_queue[0]["slot"]
        else:
            self.repeat_active = False
        pending = [r for r in self.store.trials() if r["slot"] == self.slot]
        self.replaces = pending[-1]["trial_id"] if pending else None
        if self.repeat_active:
            self.replaces = self.repeat_queue[0]["trial_id"]
        self.record = None
        self.pause_requested = False
        self.emit("campaign", str(self.store.directory))
        self.disable(
            "review_pending"
            if self.pending_repeat_slots
            and not self.repeat_active
            and (
                self.finished()
                or self.repeat_scope is not None
                and any(
                    slot // self.store.plan.repetitions == self.repeat_scope
                    for slot in self.pending_repeat_slots
                )
            )
            else "complete"
            if self.finished()
            else "gate"
        )

    def finished(self):
        return self.slot >= len(self.store.plan.points()) * self.store.plan.repetitions

    def repeat_uncertain(self):
        if self.state == "gate":
            self.refresh_reviews(self.store.directory)
            return  # Ignore a stale button click after the series was resolved by review.
        if (
            self.state not in {"review_pending", "complete"}
            or self.operation
            or self.motor_operation
        ):
            raise ApiFailure("Unsichere Versuche erst nach Abschluss der Reihe wiederholen.")
        scope = self.repeat_scope
        self.refresh_reviews(self.store.directory)
        pending = [
            {"slot": slot, "trial_id": trial_id}
            for slot, trial_id in sorted(self.pending_repeat_slots.items())
            if scope is None or slot // self.store.plan.repetitions == scope
        ]
        if not pending:
            return  # A review may have resolved the last task since the button was displayed.
        self.return_slot = self.store.next_slot()
        self.repeat_queue = [{"slot": r["slot"], "trial_id": r["trial_id"]} for r in pending]
        self.repeat_active = True
        self.slot = self.repeat_queue[0]["slot"]
        self.replaces = self.repeat_queue[0]["trial_id"]
        self.record = None
        self.pause_requested = False
        self.state = "gate"
        self.confirm()

    def defer_uncertain(self):
        if self.state in {"gate", "complete"}:
            return
        if self.state != "review_pending" or self.operation or self.motor_operation:
            raise ApiFailure("Laufende Reihe zuerst abschließen.")
        self.repeat_scope = None
        self.change("complete" if self.finished() else "gate")

    def advance(self):
        old_point = self.point().index
        self.record = None
        self.refresh_pending_repeats()
        if self.repeat_active:
            self.repeat_queue.pop(0)
            self.prune_repeat_queue()
            if self.repeat_queue:
                self.slot = self.repeat_queue[0]["slot"]
                self.replaces = self.repeat_queue[0]["trial_id"]
                if self.point().index == old_point:
                    self.begin_attempt(new_combination=False)
                else:
                    self.disable("gate")
                return
            self.repeat_active = False
            self.slot = self.return_slot
            pending = any(
                self.repeat_scope is None
                or slot // self.store.plan.repetitions == self.repeat_scope
                for slot in self.pending_repeat_slots
            )
            if pending:
                self.disable("review_pending")
            else:
                self.repeat_scope = None
                self.disable("complete" if self.finished() else "gate")
            return
        self.slot += 1
        if self.finished():
            self.repeat_scope = None
            self.disable("review_pending" if self.pending_repeat_slots else "complete")
        elif self.point().index != old_point:
            self.repeat_scope = old_point
            if self.store.plan.uncertain_repeat_timing == "series_end" and (
                any(
                    slot // self.store.plan.repetitions == old_point
                    for slot in self.pending_repeat_slots
                )
            ):
                self.disable("review_pending")
            else:
                self.repeat_scope = None
                self.disable("gate")
        else:
            self.begin_attempt(new_combination=False)

    def end_campaign(self, delete, expected_directory):
        if self.store is None or self.store.directory != Path(expected_directory).resolve():
            raise ApiFailure("Die ausgewählte Kampagne ist nicht mehr aktuell.")
        if self.state not in {
            "paused",
            "ready",
            "complete",
            "review_pending",
            "disconnected",
            "gate",
        } or (self.operation or self.motor_operation or self.archive):
            raise ApiFailure("Aufnahme und laufende Operationen zuerst beenden.")
        store = self.store
        self.release(error="Kampagne abgebrochen")
        store.checkpoint(self.slot, "cancelled")
        self.store, self.record, self.replaces = None, None, None
        self.slot = 0
        self.pending_repeat_slots = {}
        self.pause_requested = False
        deleted = False
        try:
            if delete:
                store.delete()
                deleted = True
        finally:
            self.emit(
                "campaign_ended",
                {
                    "directory": str(store.directory),
                    "deleted": deleted,
                    "delete_requested": bool(delete),
                },
            )

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
        if self.external_stop():
            self.deferred_disable = next_state
            if self.state != "stopping":
                self.change("stopping")
            return
        parameters = self.store.plan.parameters_at(self.point(), enabled=False)
        self.apply(parameters, next_state)

    def confirm(self):
        if self.state != "gate" or self.operation or self.motor_operation or self.finished():
            raise ApiFailure("Keine neue Reihe zur Freigabe bereit.")
        if self.repeat_active:
            old_point = self.point().index
            self.refresh_reviews(self.store.directory)
            if not self.repeat_active or self.point().index != old_point:
                return  # A different parameter combination needs its own operator release.
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
            storage_profile=self.store.plan.storage_profile,
            slot=self.slot,
            analytical=self.store.plan.snapshot.get("analytical_by_duration", {}).get(
                str(point.pulse_duration_ms), {}
            ),
            settling_seconds=self.store.plan.settling_seconds if new_combination else 0,
        )
        self.store.save_trial(self.record)
        try:
            self.store.reserve_capture(self.record)
        except InsufficientSpaceError as exc:
            self.pause_requested = True
            self.record.update(status="interrupted", error=str(exc))
            self.store.save_trial(self.record)
            self.emit("error", str(exc))
            self.disable("paused")
            return
        self.replaces = None
        self.emit("attempt_started", self.record["trial_id"])
        if new_combination:
            self.apply(parameters, "settling")
        else:
            actual = self.pressure.state()["parameters"]
            if not parameters_match(actual, parameters):
                raise ApiFailure("SPS-Parameter wurden außerhalb der Messreihe geändert.")
            self.record["readback_parameters"] = copy.deepcopy(actual)
            self.arm()

    def arm(self):
        if self.pause_requested or self.external_stop():
            self.pause()
            return
        try:
            self.store.check_space()
        except InsufficientSpaceError as exc:
            self.emit("error", str(exc))
            self.pause()
            return
        state = self.check_camera()
        mode = self.store.plan.capture_start_mode
        if mode is not None:
            state = copy.deepcopy(state)
            state["capture_config"]["start_mode"] = mode
            if mode == "presence" and self.store.plan.presence_confirm_seconds is not None:
                seconds = self.store.plan.presence_confirm_seconds
                state["capture_config"].update(
                    presence_confirm_seconds=seconds,
                    pre_roll_seconds=max(
                        state["capture_config"].get("pre_roll_seconds", 0), seconds + 0.05
                    ),
                )
        self.emit("capture_mode", state["capture_config"]["start_mode"])
        self.emit("capture_guard", state["capture_config"].get("presence_confirm_seconds"))
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
            **({"start_mode": mode} if mode is not None else {}),
            **(
                {"presence_confirm_seconds": self.store.plan.presence_confirm_seconds}
                if mode == "presence" and self.store.plan.presence_confirm_seconds is not None
                else {}
            ),
            **(
                {
                    "storage_profile": "review_video_v1",
                    "output_directory": str(self.store.trial_directory(self.record["trial_id"])),
                }
                if self.store.plan.storage_profile == "review_video_v1"
                else {}
            ),
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
        if self.state in {
            "disconnected",
            "connecting",
            "ready",
            "complete",
            "review_pending",
            "paused",
        }:
            return
        self.pause_requested = True
        if self.state in CAPTURE_STATES:
            try:
                self.vision.abort(self.record["trial_id"])
            except ApiFailure:
                # Completion can race the abort request. Archive that result normally.
                if self.vision.trial(self.record["trial_id"])["status"] not in TERMINAL:
                    raise
        elif self.archive or self.operation or self.motor_operation or self.deferred_disable:
            return
        else:
            if self.record and self.record["status"] not in TERMINAL:
                self.record.update(status="interrupted", error="Manuell pausiert.")
                self.store.save_trial(self.record)
            self.disable("paused")

    def retry(self):
        if self.state != "paused":
            raise ApiFailure("Wiederholung erst nach abgeschlossener Fehlerpause.")
        self.replaces = (
            self.repeat_queue[0]["trial_id"]
            if self.repeat_active
            else self.record["trial_id"]
            if self.record
            else self.replaces
        )
        self.pause_requested = False
        self.state = "gate"
        self.confirm()

    def skip(self):
        if self.state != "paused" or not self.record:
            raise ApiFailure("Kein pausierter Einzelversuch vorhanden.")
        self.record["resolution"] = "skipped"
        self.store.save_trial(self.record)
        self.refresh_pending_repeats()
        self.pause_requested = False
        # Explicit skip keeps arrays disabled and requires a new operator release.
        if self.repeat_active:
            self.repeat_queue.pop(0)
            self.prune_repeat_queue()
            if self.repeat_queue:
                self.slot = self.repeat_queue[0]["slot"]
                self.replaces = self.repeat_queue[0]["trial_id"]
            else:
                self.repeat_active = False
                self.slot = self.return_slot
        else:
            self.slot += 1
        self.record = None
        self.change(
            "review_pending"
            if self.finished() and self.pending_repeat_slots
            else "complete"
            if self.finished()
            else "gate"
        )

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
                "review_pending",
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
            if self.store:
                self.emit(
                    "storage_info",
                    {
                        "free_bytes": shutil.disk_usage(self.store.directory).free,
                        "campaign_bytes": self.campaign_bytes,
                    },
                )
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
        if self.deferred_disable:
            if not self.external_stop():
                next_state, self.deferred_disable = self.deferred_disable, None
                self.disable(next_state)
            return
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
            self.campaign_bytes += sum(
                p.stat().st_size
                for p in self.store.trial_directory(self.record["trial_id"]).rglob("*")
                if p.is_file()
            )
            self.emit("result", copy.deepcopy(self.record))
            if self.pause_requested:
                self.disable("paused")
            else:
                self.record["resolution"] = (
                    "accepted" if self.record["evaluation"]["quality"] == "valid" else "uncertain"
                )
                self.store.save_trial(self.record)
                if self.record["resolution"] == "uncertain":
                    self.pending_repeat_slots[self.slot] = self.record["trial_id"]
                else:
                    self.pending_repeat_slots.pop(self.slot, None)
                self.advance()

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
            and not self.external_stop()
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
        self.deferred_disable = None
        self.emit("motor_pending", False)
        self.change("disconnected")

    def close(self):
        self.release()
        self.pool.shutdown(wait=True)
