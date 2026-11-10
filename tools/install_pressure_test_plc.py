"""Install the optional short-pulse feature without changing normal calibration.

This edits source only: no build, download, PLC restart or actuation.
"""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path

LEGACY_DECLARATION = """	// Optional timed pulse; normal probe calibration remains unchanged.
	ProbeCalibrationTimedPulseVersion : UINT := 1;
	GuiProbeCalibrationTimedPulse : BOOL := FALSE;
	ProbeCalibrationTimedPulse : BOOL := FALSE;
"""
DECLARATION = LEGACY_DECLARATION.replace("UINT := 1;", "UINT := 2;")
VERSION_ANCHOR = "ProbeCalibrationInterfaceVersion := 2;"
VERSION_ASSIGNMENT = VERSION_ANCHOR + "\nProbeCalibrationTimedPulseVersion := 2;"
OLD_VALIDATION = (
    "\t\t\tOR (GuiProbeCalibrationMaxOpenMs < 1000) OR (GuiProbeCalibrationMaxOpenMs > 300000)"
)
LEGACY_VALIDATION = """			OR (NOT GuiProbeCalibrationTimedPulse AND
				((GuiProbeCalibrationMaxOpenMs < 1000) OR (GuiProbeCalibrationMaxOpenMs > 300000)))
			OR (GuiProbeCalibrationTimedPulse AND ((GuiProbeCalibrationMode <> 2)
				OR (GuiProbeCalibrationArray <> 2) OR (GuiProbeCalibrationNozzle <> 1)
				OR ((GuiProbeCalibrationMaxOpenMs <> 20) AND (GuiProbeCalibrationMaxOpenMs <> 50)
				AND (GuiProbeCalibrationMaxOpenMs <> 100))))"""
NEW_VALIDATION = """			OR (NOT GuiProbeCalibrationTimedPulse AND
				((GuiProbeCalibrationMaxOpenMs < 1000) OR (GuiProbeCalibrationMaxOpenMs > 300000)))
			OR (GuiProbeCalibrationTimedPulse AND ((GuiProbeCalibrationMode <> 2)
				OR (GuiProbeCalibrationArray <> 2) OR (GuiProbeCalibrationNozzle <> 1)
				OR (GuiProbeCalibrationMaxOpenMs < 20) OR (GuiProbeCalibrationMaxOpenMs > 100)
				OR ((GuiProbeCalibrationMaxOpenMs MOD 10) <> 0)))"""
OLD_TIMEOUT = (
    "\t\tIF ProbeCalibrationElapsedMs >= ProbeCalibrationMaxOpenMs "
    "THEN ProbeCalibrationFaultCode := 3; END_IF"
)
NEW_TIMEOUT = """		IF ProbeCalibrationElapsedMs >= ProbeCalibrationMaxOpenMs THEN
			IF ProbeCalibrationTimedPulse THEN
				ProbeCalibrationValveOpen := FALSE;
				ProbeCalibrationStatus := 1;
			ELSE
				ProbeCalibrationFaultCode := 3;
			END_IF
		END_IF"""


def patched_source(source):
    if "ProbeCalibrationTimedPulseVersion" in source:
        if all(
            text in source
            for text in (DECLARATION, NEW_VALIDATION, NEW_TIMEOUT, VERSION_ASSIGNMENT)
        ):
            return source
        if all(text in source for text in (LEGACY_DECLARATION, LEGACY_VALIDATION, NEW_TIMEOUT)):
            if source.count(VERSION_ANCHOR) != 1:
                raise ValueError("Versionszuweisung der SPS-Kalibrierfunktion nicht eindeutig.")
            return (
                source.replace(LEGACY_DECLARATION, DECLARATION)
                .replace(LEGACY_VALIDATION, NEW_VALIDATION)
                .replace(VERSION_ANCHOR, VERSION_ASSIGNMENT)
            )
        raise ValueError("Eine abweichende Kurzimpuls-Erweiterung ist bereits vorhanden.")
    replacements = [
        (
            "\tProbeCalibrationInterfaceVersion : UINT := 2;",
            "\tProbeCalibrationInterfaceVersion : UINT := 2;\n" + DECLARATION.rstrip(),
        ),
        (OLD_VALIDATION, NEW_VALIDATION),
        (VERSION_ANCHOR, VERSION_ASSIGNMENT),
        (
            "\t\t\tProbeCalibrationMaxOpenMs := GuiProbeCalibrationMaxOpenMs;",
            "\t\t\tProbeCalibrationMaxOpenMs := GuiProbeCalibrationMaxOpenMs;\n"
            "\t\t\tProbeCalibrationTimedPulse := GuiProbeCalibrationTimedPulse;",
        ),
        (
            "\t\tGuiProbeCalibrationStop := FALSE;",
            "\t\tGuiProbeCalibrationStop := FALSE;\n"
            "\t\tGuiProbeCalibrationTimedPulse := FALSE;\n\t\tProbeCalibrationTimedPulse := FALSE;",
        ),
        (OLD_TIMEOUT, NEW_TIMEOUT),
    ]
    for before, after in replacements:
        if source.count(before) != 1:
            raise ValueError("SPS-Quelle passt nicht zur geprüften Kalibrierfunktion: " + before)
        source = source.replace(before, after)
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_pou", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raw = args.main_pou.read_bytes()
    source = raw.decode("utf-8-sig").replace("\r\n", "\n")
    result = patched_source(source)
    print(
        "".join(
            difflib.unified_diff(
                source.splitlines(True),
                result.splitlines(True),
                fromfile="MAIN vorher",
                tofile="MAIN Kurzimpuls",
            )
        )
    )
    if args.apply and result != source:
        backup = args.main_pou.with_suffix(".TcPOU.before-pressure-test-v2")
        if backup.exists():
            raise FileExistsError("Sicherungsdatei besteht bereits: " + str(backup))
        backup.write_bytes(raw)
        newline = "\r\n" if b"\r\n" in raw else "\n"
        prefix = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        args.main_pou.write_bytes(prefix + result.replace("\n", newline).encode("utf-8"))


if __name__ == "__main__":
    main()
