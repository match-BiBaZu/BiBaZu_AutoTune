import importlib.util
from pathlib import Path

import pytest


def installer():
    path = Path(__file__).parents[1] / "tools/install_pressure_test_plc.py"
    spec = importlib.util.spec_from_file_location("pressure_patch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_patch_is_small_idempotent_and_keeps_unrelated_source():
    patch = installer()
    source = "\n".join(
        [
            "PROGRAM MAIN\nVAR\n\tProbeCalibrationInterfaceVersion : UINT := 2;\nEND_VAR",
            patch.OLD_VALIDATION,
            "\t\t\tProbeCalibrationMaxOpenMs := GuiProbeCalibrationMaxOpenMs;",
            "\t\tGuiProbeCalibrationStop := FALSE;",
            patch.OLD_TIMEOUT,
            "UnrelatedValveLogic := KeepMeExactly;",
        ]
    )
    changed = patch.patched_source(source)
    assert patch.patched_source(changed) == changed
    assert "UnrelatedValveLogic := KeepMeExactly;" in changed
    assert "ProbeCalibrationFaultCode := 3;" in changed  # Existing long-open watchdog remains.
    assert "NOT GuiProbeCalibrationTimedPulse" in changed
    assert "GuiProbeCalibrationArray <> 2" in changed
    assert "GuiProbeCalibrationNozzle <> 1" in changed
    assert len(changed.splitlines()) - len(source.splitlines()) < 30


def test_unknown_calibration_source_is_rejected():
    with pytest.raises(ValueError):
        installer().patched_source("PROGRAM OTHER\nVAR\nEND_VAR")
