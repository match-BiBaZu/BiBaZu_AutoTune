"""SPS-only operator session; no camera, experiments or run-file side effects."""

from __future__ import annotations

import time

from .clients import ApiFailure
from .recipes import validate_parameters


class PlcSession:
    def __init__(self, pressure, emit=lambda *args: None, clock=time.monotonic):
        self.pressure, self.emit, self.clock = pressure, emit, clock
        self.state = "disconnected"
        self.operation_id = None
        self.operation_started = 0
        self.conveyor_operation = None
        self.conveyor_started = 0
        self.last_heartbeat = 0
        self.last_poll = -1

    def change(self, state):
        self.state = state
        self.emit("state", state)

    def connect(self):
        if self.state != "disconnected":
            raise ApiFailure("Sitzung zuerst beenden.")
        response = self.pressure.acquire()
        self.operation_id = response["snapshot_operation_id"]
        self.operation_started = self.last_heartbeat = self.clock()
        self.last_poll = -1
        self.change("connecting")

    def apply_parameters(self, parameters):
        if self.state != "ready":
            raise ApiFailure("Laufende SPS-Operation zuerst abschließen.")
        self.operation_id = self.pressure.apply(validate_parameters(parameters))["operation_id"]
        self.operation_started = self.clock()
        self.change("updating")

    def conveyor(self, start):
        if start and (self.state != "ready" or self.conveyor_operation):
            raise ApiFailure("Laufende SPS-Operation zuerst abschließen.")
        if self.state == "disconnected":
            raise ApiFailure("SPS zuerst verbinden.")
        response = self.pressure.start_conveyor() if start else self.pressure.stop_conveyor()
        self.conveyor_operation = response["operation_id"]
        self.conveyor_started = self.clock()
        self.emit("conveyor_pending", True)

    def tick(self):
        if self.state == "disconnected":
            return
        now = self.clock()
        if now - self.last_heartbeat >= 0.5:
            self.pressure.heartbeat()
            self.last_heartbeat = now
        if self.conveyor_operation:
            result = self.pressure.operation(self.conveyor_operation)
            if result["status"] != "pending":
                self.conveyor_operation = None
                self.emit("conveyor_pending", False)
                if result["status"] != "succeeded":
                    raise ApiFailure(result.get("error", "Bandoperation fehlgeschlagen."))
            elif now - self.conveyor_started > 30:
                raise ApiFailure("Bandoperation antwortet nicht.")
        if self.operation_id:
            result = self.pressure.operation(self.operation_id)
            if result["status"] == "pending":
                if now - self.operation_started > 15:
                    raise ApiFailure(
                        "SPS-Operation antwortet nicht; keine automatische Wiederholung."
                    )
                return
            if result["status"] != "succeeded":
                raise ApiFailure(result.get("error", "SPS-Operation fehlgeschlagen."))
            snapshot = result.get("snapshot") or self.pressure.state()
            parameters = validate_parameters(snapshot["parameters"])
            if self.state == "connecting":
                self.emit("connected", {"parameters": parameters})
            else:
                self.emit("parameters_applied", parameters)
            self.operation_id = None
            self.emit("pressure_state", snapshot)
            self.last_poll = now
            self.change("ready")
        if now - self.last_poll >= 0.5:
            self.emit("pressure_state", self.pressure.state())
            self.last_poll = now

    def fail(self, error):
        self.emit("error", str(error))
        self.release()

    def release(self):
        try:
            self.pressure.release()
        except Exception:
            pass
        self.operation_id = self.conveyor_operation = None
        self.emit("conveyor_pending", False)
        self.change("disconnected")
