"""Serialized, abortable sweep orchestration; short pulses are timed by the PLC."""

from __future__ import annotations

import copy
import time
import uuid

from .pressure_test import (
    COMBINATION_SETTLE_MS,
    FILETIME_MS,
    INTER_PULSE_PAUSE_MS,
    PRESSURE_SETTLE_MS,
    SIGNALS,
    PressureRecording,
    pressure_matrix,
)
from .pressure_test_plc import PressurePulsePlc


class PressureTestRunner:
    def __init__(self, backend, directory, cancel, guard, *, clock=time.monotonic, adapter=None):
        self.backend, self.directory = backend, directory
        self.cancel, self.guard, self.clock = cancel, guard, clock
        self.plc = adapter or PressurePulsePlc(backend)
        self.matrix = pressure_matrix()
        self.recording = None
        self.active = False
        self.phase = "idle"
        self.token = None
        self.index = 0
        self.mark = 0
        self.last_beat = 0
        self.last_closed = -100.0
        self.baseline = None
        self.final_status = None
        self.error = None
        self.last_rows = []
        self.next_publish = 0
        self.published = {
            "status": "idle",
            "phase": "idle",
            "completed": 0,
            "total": len(self.matrix),
        }

    def snapshot(self):
        return copy.deepcopy(self.published)

    def publish(self, status="running"):
        self.next_publish = self.clock() + 0.2
        pulse = self.matrix[min(self.index, len(self.matrix) - 1)]
        self.published = {
            "status": status,
            "phase": self.phase,
            "active": self.active,
            "completed": len([r for r in self.recording.results if r["complete"]])
            if self.recording
            else 0,
            "total": len(self.matrix),
            "current": pulse.__dict__,
            "error": self.error,
            "directory": str(self.recording.path) if self.recording else None,
            "results": copy.deepcopy(self.recording.results) if self.recording else [],
            "last_rows": copy.deepcopy(self.last_rows),
        }

    def check(self):
        return self.plc.check()

    def start(self, token, request_id=None):
        if self.active:
            raise RuntimeError("Eine Drucktestreihe läuft bereits.")
        self.token = token
        self.guard(token)
        self.check()
        self.baseline = copy.deepcopy(self.backend.snapshot()["parameters"])
        self.request_id = request_id or uuid.uuid4().hex
        self.recording = PressureRecording(self.directory, self.request_id, self.baseline)
        self.index, self.mark = 0, 0
        self.error = self.final_status = None
        self.last_rows = []
        self.last_closed = -100.0
        try:
            self.plc.subscribe()
        except Exception as exc:
            self.recording.finish("failed", str(exc))
            raise
        self.active = True
        self.phase = "preflight"
        self.deadline = self.clock() + 0.3
        self.last_beat = self.clock()
        self.publish()

    def checked(self):
        self.guard(self.token)
        if self.cancel.is_set():
            raise RuntimeError("Drucktest abgebrochen.")

    def enter(self):
        self.sequence = self.plc.enter(self.matrix[self.index], self.checked)
        self.phase = "entering"
        self.deadline = self.clock() + 3

    def validate_streams(self):
        events = self.plc.snapshot()
        times = {signal: sorted(t for s, t, _ in events if s == signal) for signal in SIGNALS}
        for signal, stamps in times.items():
            if len(stamps) < 100 or any(
                b - a != FILETIME_MS for a, b in zip(stamps, stamps[1:], strict=False)
            ):
                raise RuntimeError(f"{signal}: Keine lückenlosen 1-ms-ADS-Notifications verfügbar.")
        if len(set.intersection(*(set(t) for t in times.values()))) < 100:
            raise RuntimeError("ADS-Signale haben keine gemeinsamen 1-ms-Serverzeitstempel.")

    def tick(self):
        if not self.active:
            return
        try:
            previous_phase = self.phase
            if self.phase == "cleanup":
                self.finish_cleanup()
                return
            if self.cancel.is_set():
                self.abort("Vom Benutzer abgebrochen.", "aborted")
                return
            self.checked()
            now = self.clock()
            if now - self.last_beat >= 0.1:
                self.plc.beat(self.checked)
                self.last_beat = now
            info = self.plc.info()
            if info["fault"]:
                raise RuntimeError(f"SPS-Kalibrierfehler {info['fault']}; keine Wiederholung.")
            if self.phase == "preflight" and now >= self.deadline:
                self.validate_streams()
                self.enter()
            elif self.phase == "entering":
                if info["ack"] == self.sequence and info["active"] and info["status"] == 1:
                    pulse = self.matrix[self.index]
                    if (
                        info["pressure_mbar"],
                        info["pulse_ms"],
                        info["array"],
                        info["nozzle"],
                        info["timed"],
                    ) != (pulse.pressure_mbar, pulse.pulse_ms, 2, 1, True):
                        raise RuntimeError("SPS-Rücklesen der Testparameter stimmt nicht überein.")
                    self.phase = "settling"
                    previous = self.matrix[self.index - 1] if self.index else None
                    new_combination = previous is None or (
                        pulse.pulse_ms,
                        pulse.pressure_mbar,
                    ) != (previous.pulse_ms, previous.pressure_mbar)
                    settle_ms = COMBINATION_SETTLE_MS if new_combination else PRESSURE_SETTLE_MS
                    self.deadline = max(
                        now + settle_ms / 1000,
                        self.last_closed + INTER_PULSE_PAUSE_MS / 1000,
                    )
                elif now >= self.deadline:
                    raise RuntimeError("SPS hat den Testmodus nicht bestätigt.")
            elif self.phase == "settling" and now >= self.deadline:
                self.mark = self.plc.latest_tick()
                self.sequence = self.plc.fire(self.checked)
                self.phase = "capturing"
                self.deadline = now + 2
            elif self.phase == "capturing":
                if info["ack"] == self.sequence and info["status"] == 1 and not info["open"]:
                    if info["elapsed_ms"] != self.matrix[self.index].pulse_ms:
                        raise RuntimeError("SPS meldet eine unerwartete Impulsdauer.")
                    self.last_closed = now
                    self.phase = "post"
                    self.deadline = now + 0.25
                elif now >= self.deadline:
                    raise RuntimeError("Impulsabschluss wurde nicht bestätigt.")
            elif self.phase == "post" and now >= self.deadline:
                events = self.plc.snapshot(self.mark - 100 * FILETIME_MS)
                result, self.last_rows = self.recording.add(self.matrix[self.index], events)
                self.mark = 0
                if not result["complete"]:
                    raise RuntimeError("Messung unvollständig: " + result["error"])
                self.index += 1
                if self.index == len(self.matrix):
                    self.abort(None, "completed")
                    return
                self.sequence = self.plc.leave()
                self.phase = "leaving"
                self.deadline = now + 3
            elif self.phase == "leaving":
                if info["ack"] == self.sequence and not info["active"]:
                    self.enter()
                elif now >= self.deadline:
                    raise RuntimeError(
                        "SPS hat das Beenden des vorherigen Einzelstoßes nicht bestätigt."
                    )
            if self.phase != previous_phase or self.clock() >= self.next_publish:
                self.publish()
        except Exception as exc:
            if self.phase == "cleanup":
                self.complete("failed", f"Schließen/Rücklesen nicht bestätigt: {exc}")
            else:
                self.abort(str(exc), "failed")

    def abort(self, error="Vom Benutzer abgebrochen.", status="aborted"):
        if not self.active:
            return
        if self.phase == "cleanup":
            return
        self.error, self.final_status = error, status
        try:
            # The stop is deliberately independent of a cancelled/expired session.
            # ADS ownership is still checked by backend.write.
            self.plc.stop()
            if self.mark and self.recording:
                events = self.plc.snapshot(self.mark - 100 * FILETIME_MS)
                _, self.last_rows = self.recording.add(
                    self.matrix[self.index], events, error=error or "Abgebrochen"
                )
                self.mark = 0
            self.sequence = self.plc.leave()
            self.phase = "cleanup"
            self.deadline = self.clock() + 3
            self.publish()
        except Exception as exc:
            self.complete("failed", f"{error or ''}; Schließen nicht bestätigt: {exc}")

    def finish_cleanup(self):
        info = self.plc.info()
        if info["ack"] == self.sequence and not info["active"] and not info["open"]:
            restored = copy.deepcopy(self.baseline)
            for array in restored["arrays"]:
                array["enabled"] = False
            self.backend.apply(restored, lambda: None)
            self.complete(self.final_status, self.error)
        elif self.clock() >= self.deadline:
            raise RuntimeError("Beenden des Testmodus nicht bestätigt.")

    def complete(self, status, error):
        try:
            self.plc.unsubscribe()
        except Exception as exc:
            status, error = "failed", f"{error or ''}; {exc}"
        self.phase = status
        self.error = error
        if self.recording:
            try:
                self.recording.finish(status, error)
            except Exception as exc:
                self.phase = status = "failed"
                self.error = f"Messprotokoll konnte nicht abgeschlossen werden: {exc}"
                self.recording.manifest.update(status="failed", error=self.error)
                try:
                    self.recording.persist()
                except OSError:
                    pass
        self.active = False
        self.publish(status)
