"""ADS adapter, independent of Qt. Ported from BiBaZu Pressure Control (MIT).

Only the service's PLC thread may call this adapter. Connecting is read-only.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import pyads

from .recipes import validate_parameters


@dataclass(frozen=True)
class PlcConfig:
    ip: str = "192.168.10.23"
    ams_net_id: str = "10.145.4.14.1.1"
    port: int = pyads.PORT_TC3PLC1


def array_symbols(index):
    return {
        "enabled": f"MAIN.GuiArrayEnabled{index}",
        "pressure_mbar": f"MAIN.GuiPressureMbar{index}",
        "delay_ms": f"MAIN.GuiDelayMs{index}",
        "pulse_duration_ms": f"MAIN.GuiPulseDurationMs{index}",
        "offset_mm": f"MAIN.GuiOffsetMm{index}",
        "conveyor_velocity_delay": f"MAIN.GuiConveyorVelocityDelay{index}",
        **{f"nozzle_{n}": f"MAIN.GuiNozzleEnabled{(index - 1) * 6 + n}" for n in range(1, 7)},
    }


FLAGS = [
    "MAIN.GuiConveyorEnabled",
    "MAIN.GuiConveyorCalibrationMode",
    "MAIN.GuiVelocityCheckMode",
    "MAIN.GuiForceDelayMeasurementEnabled",
    "MAIN.ConveyorServo1TargetVelocity",
    "MAIN.ConveyorServo2TargetVelocity",
    "MAIN.StepperPosBusy",
    "MAIN.StepperPosError",
    "MAIN.StepperPosReadyToExecute",
    "MAIN.ConveyorServoCommissioned",
    "MAIN.ConveyorServoFaultCode",
    "MAIN.ConveyorServoState",
    "MAIN.ConveyorServoVelocityOnly",
    "MAIN.ConveyorServoAccelerationRpmPerSec",
    "MAIN.GuiConveyorReverse",
]
SAFE_STOP = {
    "MAIN.GuiCalibrationStop": True,
    "MAIN.GuiConveyorCalibrationMode": False,
    "MAIN.GuiVelocityCheckMode": False,
    "MAIN.GuiConveyorEnabled": False,
    "MAIN.GuiForceDelayMeasurementEnabled": False,
}
TANDEM_CONFIG = {
    "MAIN.ConveyorServoCommissioned": True,
    "MAIN.ConveyorServoMotorCount": 2,
    "MAIN.ConveyorServoSingleMotor": 1,
    "MAIN.ConveyorServoDirection1": -1,
    "MAIN.ConveyorServoDirection2": -1,
    "MAIN.ConveyorServoMotor2Ratio": 1.0,
    "MAIN.ConveyorServoMaxMotorRpm": 0.0,
}


class AdsBackend:
    def __init__(self, config=None, connection_factory=pyads.Connection):
        self.config, self.connection_factory = config or PlcConfig(), connection_factory
        self.plc = None

    def connect(self):
        if self.plc is None:
            plc = self.connection_factory(self.config.ams_net_id, self.config.port, self.config.ip)
            try:
                plc.open()
                plc.set_timeout(500)
            except Exception:
                plc.close()
                raise
            self.plc = plc

    def close(self):
        if self.plc is not None:
            self.plc.close()
            self.plc = None

    def read(self, names):
        if self.plc is None:
            raise RuntimeError("ADS offline")
        values = self.plc.read_list_by_name(names, cache_symbol_info=True)
        if any(name not in values for name in names):
            raise RuntimeError("Unvollständige ADS-Antwort")
        return values

    def write(self, values, guard=lambda: None):
        guard()
        if self.plc is None:
            raise RuntimeError("ADS offline")
        # Fail closed: inability to read ownership is not permission to write.
        owner = self.plc.read_by_name("MAIN.GuiReorientationControlActive", pyads.PLCTYPE_BOOL)
        if owner:
            raise RuntimeError("BiBaZu Reorientation Control besitzt derzeit die SPS.")
        guard()
        errors = self.plc.write_list_by_name(values, cache_symbol_info=True)
        failed = {
            name: error
            for name, error in errors.items()
            if error and str(error).lower() != "no error"
        }
        if set(errors) != set(values) or failed:
            raise RuntimeError(f"ADS sum write failed: {failed or 'missing acknowledgements'}")

    def snapshot(self):
        names = ["MAIN.GuiConveyorSpeedMmPerSec", *FLAGS]
        for index in range(1, 5):
            names.extend(array_symbols(index).values())
        values = self.read(names)
        arrays = []
        for index in range(1, 5):
            symbols = array_symbols(index)
            arrays.append(
                {
                    "index": index,
                    **{
                        field: values[name]
                        for field, name in symbols.items()
                        if not field.startswith("nozzle_")
                    },
                    "nozzles_enabled": [bool(values[symbols[f"nozzle_{n}"]]) for n in range(1, 7)],
                }
            )
        moving = bool(
            values["MAIN.GuiConveyorEnabled"]
            or values["MAIN.StepperPosBusy"]
            or values["MAIN.ConveyorServo1TargetVelocity"]
            or values["MAIN.ConveyorServo2TargetVelocity"]
        )
        return {
            "parameters": validate_parameters(
                {
                    "arrays": arrays,
                    "conveyor_speed_mm_per_sec": values["MAIN.GuiConveyorSpeedMmPerSec"],
                }
            ),
            "conveyor": {
                "enabled": bool(values["MAIN.GuiConveyorEnabled"]),
                "moving": moving,
                "reverse": bool(values["MAIN.GuiConveyorReverse"]),
                "ready": bool(values["MAIN.StepperPosReadyToExecute"]),
                "fault_code": int(values["MAIN.ConveyorServoFaultCode"]),
                "error": bool(values["MAIN.StepperPosError"]),
            },
        }

    def ensure_stationary(self):
        flags = self.read(FLAGS)
        if any(flags[name] for name in FLAGS[:7]):
            raise RuntimeError("Band ausschalten und Kalibrierung beenden.")
        return flags

    def apply(self, parameters, guard):
        parameters = validate_parameters(parameters)
        self.ensure_stationary()
        values = {"MAIN.GuiConveyorSpeedMmPerSec": parameters["conveyor_speed_mm_per_sec"]}
        for array in parameters["arrays"]:
            symbols = array_symbols(array["index"])
            for field, name in symbols.items():
                values[name] = (
                    array["nozzles_enabled"][int(field[7:]) - 1]
                    if field.startswith("nozzle_")
                    else array[field]
                )
        self.write(values, guard)
        actual = self.read(list(values))
        mismatch = [
            name
            for name, expected in values.items()
            if not math.isclose(float(actual[name]), float(expected), rel_tol=1e-6, abs_tol=1e-4)
        ]
        if mismatch:
            raise RuntimeError(f"SPS-Rücklesen stimmt nicht überein: {mismatch}")
        return self.snapshot()

    def machine_metadata(self):
        """Informational profile snapshot only; never restored to the PLC."""
        mapping = {
            "light_barrier_debounce_ms": "MAIN.GuiBarrierCalibrationDebounceMs",
            "conveyor_max_speed_mm_per_sec": "MAIN.GuiConveyorMaxSpeedMmPerSec",
            **{
                f"sensor_spacing_{a}{b}_mm": f"MAIN.GuiSensorSpacing{a}{b}Mm"
                for a, b in ((i, i + 1) for i in range(1, 8))
            },
            **{
                f"sensor_{2 * i}_to_array_{i}_spacing_mm": (
                    f"MAIN.GuiSensor{2 * i}ToArray{i}SpacingMm"
                )
                for i in range(1, 5)
            },
        }
        names = list(mapping.values())
        names += [f"MAIN.GuiLightBarrierInvert{i}" for i in range(1, 9)]
        names += [f"MAIN.GuiLightBarrierDebounceEnabled{i}" for i in range(1, 9)]
        names += [f"MAIN.GuiForceSingleNozzleResponseDelayMs{i}" for i in range(1, 5)]
        calibration = {
            "marker_distance_mm": "MAIN.GuiCalibrationMarkerDistanceMm",
            "mm_per_full_step": "MAIN.GuiConveyorMmPerFullStep",
            "valid": "MAIN.GuiConveyorCalibrationValid",
        }
        names += list(calibration.values())
        values = self.read(names)
        delays = [values[f"MAIN.GuiForceSingleNozzleResponseDelayMs{i}"] for i in range(1, 5)]
        return {
            **{field: values[name] for field, name in mapping.items()},
            "light_barrier_inverted": [
                bool(values[f"MAIN.GuiLightBarrierInvert{i}"]) for i in range(1, 9)
            ],
            "light_barrier_debounce_enabled": [
                bool(values[f"MAIN.GuiLightBarrierDebounceEnabled{i}"]) for i in range(1, 9)
            ],
            "conveyor_calibration": {field: values[name] for field, name in calibration.items()},
            "force_delays_ms": delays,
            "force_response_delays_ms": delays,
            "force_single_nozzle_response_delays_ms": delays,
        }

    def wait(self, predicate, guard, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            guard()
            flags = self.read(FLAGS)
            if predicate(flags):
                return flags
            time.sleep(0.05)
        raise RuntimeError("Tandem conveyor timeout")

    def start_conveyor(self, guard):
        flags = self.ensure_stationary()
        if not flags["MAIN.ConveyorServoVelocityOnly"]:
            raise RuntimeError("PLC conveyor block is not the velocity-only tandem version")
        acceleration = float(flags["MAIN.ConveyorServoAccelerationRpmPerSec"])
        if not 0.1 <= acceleration <= 300:
            raise RuntimeError("Invalid configured conveyor acceleration")
        try:
            self.write(SAFE_STOP, guard)
            self.wait(
                lambda f: (
                    not f["MAIN.StepperPosBusy"]
                    and not f["MAIN.ConveyorServo1TargetVelocity"]
                    and not f["MAIN.ConveyorServo2TargetVelocity"]
                ),
                guard,
            )
            self.write(
                {**TANDEM_CONFIG, "MAIN.ConveyorServoAccelerationRpmPerSec": acceleration}, guard
            )
            time.sleep(0.25)
            self.write({"MAIN.GuiConveyorReset": True}, guard)
            time.sleep(0.05)
            self.wait(
                lambda f: (
                    not f["MAIN.ConveyorServoFaultCode"]
                    and f["MAIN.ConveyorServoCommissioned"]
                    and not f["MAIN.StepperPosError"]
                    and f["MAIN.ConveyorServoState"] == 0
                    and not f["MAIN.StepperPosBusy"]
                ),
                guard,
            )
            self.write({"MAIN.GuiConveyorReset": False}, guard)
            self.write({"MAIN.GuiConveyorEnabled": True}, guard)
            flags = self.wait(
                lambda f: (
                    f["MAIN.StepperPosReadyToExecute"]
                    or (
                        f["MAIN.StepperPosBusy"]
                        and f["MAIN.ConveyorServoState"] == 30
                        and f["MAIN.ConveyorServo1TargetVelocity"]
                        and f["MAIN.ConveyorServo2TargetVelocity"]
                    )
                    or f["MAIN.ConveyorServoFaultCode"]
                ),
                guard,
            )
            if flags["MAIN.ConveyorServoFaultCode"]:
                raise RuntimeError(f"Tandem conveyor fault: {flags['MAIN.ConveyorServoFaultCode']}")
            return self.snapshot()
        except Exception:
            self.stop_conveyor()
            raise

    def stop_conveyor(self):
        self.write({"MAIN.GuiConveyorEnabled": False, "MAIN.GuiConveyorReset": False})
        return self.snapshot()
