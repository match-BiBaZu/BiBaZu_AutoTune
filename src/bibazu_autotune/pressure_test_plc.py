"""Small adapter for the existing probe interlock and timestamped ADS notifications."""

from __future__ import annotations

import ctypes
import threading
from collections import deque

import pyads

from .pressure_test import TIMED_PULSE_FEATURE_VERSION

INFO = {
    "feature_version": "ProbeCalibrationTimedPulseVersion",
    "active": "ProbeCalibrationActive",
    "status": "ProbeCalibrationStatus",
    "fault": "ProbeCalibrationFaultCode",
    "ack": "ProbeCalibrationAckSequence",
    "opened_ms": "ProbeCalibrationOpenedAtMs",
    "elapsed_ms": "ProbeCalibrationElapsedMs",
    "open": "ProbeCalibrationValveOpen",
    "sequence": "GuiProbeCalibrationCommandSequence",
    "heartbeat": "GuiProbeCalibrationHeartbeat",
    "beam_clear": "LightBarrierStable4",
    "pressure_mbar": "ProbeCalibrationPressureMbar",
    "pulse_ms": "ProbeCalibrationMaxOpenMs",
    "array": "ProbeCalibrationArray",
    "nozzle": "ProbeCalibrationNozzle",
    "timed": "ProbeCalibrationTimedPulse",
}
STREAMS = {
    "raw1": ("RawNozzlePressure", pyads.PLCTYPE_INT),
    "raw2": ("RawNozzlePressure2", pyads.PLCTYPE_INT),
    "bar1": ("NozzlePressure", pyads.PLCTYPE_REAL),
    "bar2": ("NozzlePressure2", pyads.PLCTYPE_REAL),
    "valve": ("OpenValve5", pyads.PLCTYPE_BOOL),
}


class PressurePulsePlc:
    def __init__(self, backend):
        self.backend = backend
        self.handles = []
        self.callbacks = []
        self.events = deque(maxlen=100_000)
        self.lock = threading.Lock()
        self.sequence = 0
        self.heartbeat = 0

    def info(self):
        values = self.backend.read(["MAIN." + name for name in INFO.values()])
        return {key: values["MAIN." + name] for key, name in INFO.items()}

    def check(self):
        try:
            version = self.backend.read(["MAIN.ProbeCalibrationTimedPulseVersion"])
        except Exception as exc:
            raise RuntimeError(
                "SPS-Kurzimpuls-Erweiterung fehlt oder ist nicht lesbar: " + str(exc)
            ) from exc
        if version["MAIN.ProbeCalibrationTimedPulseVersion"] != TIMED_PULSE_FEATURE_VERSION:
            raise RuntimeError(
                f"SPS-Kurzimpuls-Erweiterung Version {TIMED_PULSE_FEATURE_VERSION} erforderlich "
                "(20–100 ms in 10-ms-Schritten)."
            )
        info = self.info()
        if info["feature_version"] != TIMED_PULSE_FEATURE_VERSION:
            raise RuntimeError(
                "SPS-Kurzimpuls-Erweiterung fehlt "
                f"(ProbeCalibrationTimedPulseVersion={TIMED_PULSE_FEATURE_VERSION})."
            )
        if info["active"]:
            raise RuntimeError("Eine Sondenkalibrierung ist bereits aktiv.")
        if info["fault"]:
            raise RuntimeError(
                "Vorhandenen Kalibrierfehler zuerst im zuständigen Werkzeug beenden."
            )
        if not info["beam_clear"]:
            raise RuntimeError(
                "Lichtschranke 4 muss frei sein; Bauteile aus dem Testbereich entfernen."
            )
        self.backend.ensure_stationary()
        self.sequence = max(info["sequence"], info["ack"])
        self.heartbeat = info["heartbeat"]
        return info

    def command(self, command, guard, payload=None):
        if payload:
            self.backend.write({"MAIN." + key: value for key, value in payload.items()}, guard)
        self.backend.write({"MAIN.GuiProbeCalibrationCommand": command}, guard)
        self.sequence += 1
        self.backend.write({"MAIN.GuiProbeCalibrationCommandSequence": self.sequence}, guard)
        return self.sequence

    def enter(self, pulse, guard):
        return self.command(
            1,
            guard,
            {
                "GuiProbeCalibrationTimedPulse": True,
                "GuiProbeCalibrationMode": 2,
                "GuiProbeCalibrationArray": 2,
                "GuiProbeCalibrationNozzle": 1,
                "GuiProbeCalibrationTriggerSensor": 4,
                "GuiProbeCalibrationPressureMbar": pulse.pressure_mbar,
                "GuiProbeCalibrationMaxOpenMs": pulse.pulse_ms,
            },
        )

    def fire(self, guard):
        return self.command(5, guard)

    def leave(self):
        return self.command(4, lambda: None)

    def stop(self):
        self.backend.write({"MAIN.GuiProbeCalibrationStop": True})

    def beat(self, guard):
        self.heartbeat = (self.heartbeat + 1) % 2**32
        self.backend.write({"MAIN.GuiProbeCalibrationHeartbeat": self.heartbeat}, guard)

    def subscribe(self):
        self.unsubscribe()
        self.events.clear()
        plc = self.backend.plc
        try:
            for signal, (name, datatype) in STREAMS.items():

                def receive(handle, name, timestamp, value, signal=signal):
                    with self.lock:
                        self.events.append((signal, int(timestamp), value))

                callback = plc.notification(datatype, timestamp_as_filetime=True)(receive)
                self.callbacks.append(callback)
                attr = pyads.NotificationAttrib(
                    ctypes.sizeof(datatype), pyads.ADSTRANS_SERVERCYCLE, max_delay=5, cycle_time=1
                )
                self.handles.append(plc.add_device_notification("MAIN." + name, attr, callback))
        except Exception:
            self.unsubscribe()
            raise

    def snapshot(self, after=0):
        with self.lock:
            return [event for event in self.events if event[1] >= after]

    def latest_tick(self):
        with self.lock:
            return max((event[1] for event in self.events), default=0)

    def unsubscribe(self):
        errors = []
        for handles in self.handles:
            try:
                self.backend.plc.del_device_notification(*handles)
            except Exception as exc:
                errors.append(str(exc))
        self.handles.clear()
        self.callbacks.clear()
        if errors:
            raise RuntimeError(
                "ADS-Notifications konnten nicht vollständig beendet werden: " + "; ".join(errors)
            )
