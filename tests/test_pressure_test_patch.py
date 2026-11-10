import importlib.util
import re
import sys
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
            patch.VERSION_ANCHOR,
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


def test_upgrade_version_1_preserves_other_logic_and_is_idempotent():
    patch = installer()
    source = "\n".join(
        [
            patch.LEGACY_DECLARATION,
            patch.LEGACY_VALIDATION,
            patch.NEW_TIMEOUT,
            patch.VERSION_ANCHOR,
            "KeepMe := TRUE;",
        ]
    )
    changed = patch.patched_source(source)
    assert changed == source.replace(patch.LEGACY_DECLARATION, patch.DECLARATION).replace(
        patch.LEGACY_VALIDATION, patch.NEW_VALIDATION
    ).replace(patch.VERSION_ANCHOR, patch.VERSION_ASSIGNMENT)
    assert "ProbeCalibrationTimedPulseVersion : UINT := 2;" in changed
    assert patch.patched_source(changed) == changed
    with pytest.raises(ValueError):
        patch.patched_source(source.replace("UINT := 1;", "UINT := 99;"))


@pytest.mark.parametrize("timed", [False, True])
@pytest.mark.parametrize("duration", [0, 10, 19, *range(20, 101), 110, 999, 1000, 300000, 300001])
def test_sps_validation_accepts_only_supported_durations(timed, duration):
    # Execute the actual ST boolean expression with equivalent Python operators.
    # This checks the emitted validation, including the unchanged long-open path.
    expression = installer().NEW_VALIDATION.strip().removeprefix("OR ")
    for before, after in (("<>", "!="), ("MOD", "%"), ("AND", "and"), ("OR", "or"), ("NOT", "not")):
        expression = expression.replace(before, after)
    expression = re.sub(r"\s+", " ", expression)
    inputs = {
        "GuiProbeCalibrationTimedPulse": timed,
        "GuiProbeCalibrationMaxOpenMs": duration,
        "GuiProbeCalibrationMode": 2,
        "GuiProbeCalibrationArray": 2,
        "GuiProbeCalibrationNozzle": 1,
    }
    allowed = duration in range(20, 101, 10) if timed else 1000 <= duration <= 300000
    assert eval(expression, {"__builtins__": {}}, inputs) is not allowed
    if timed:
        for key in (
            "GuiProbeCalibrationMode",
            "GuiProbeCalibrationArray",
            "GuiProbeCalibrationNozzle",
        ):
            assert eval(expression, {"__builtins__": {}}, {**inputs, key: 99})


def test_upgrade_backup_preserves_original_backup_and_line_endings(tmp_path, monkeypatch):
    patch = installer()
    source = "\n".join(
        [patch.LEGACY_DECLARATION, patch.LEGACY_VALIDATION, patch.NEW_TIMEOUT, patch.VERSION_ANCHOR]
    )
    raw = b"\xef\xbb\xbf" + source.replace("\n", "\r\n").encode()
    path = tmp_path / "MAIN.TcPOU"
    path.write_bytes(raw)
    old_backup = path.with_suffix(".TcPOU.before-pressure-test")
    old_backup.write_bytes(b"original backup")
    monkeypatch.setattr(sys, "argv", ["installer", str(path)])
    patch.main()  # Preview does not write anything.
    assert path.read_bytes() == raw
    monkeypatch.setattr(sys, "argv", ["installer", str(path), "--apply"])
    patch.main()
    upgraded = path.read_bytes()
    assert upgraded.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in upgraded.replace(b"\r\n", b"")
    assert path.with_suffix(".TcPOU.before-pressure-test-v2").read_bytes() == raw
    assert old_backup.read_bytes() == b"original backup"
    patch.main()  # Repeated application does not replace the backup.
    assert path.read_bytes() == upgraded
