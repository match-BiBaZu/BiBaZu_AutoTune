"""User-selected startup defaults, separate from trial/profile serialization."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from .analytical_model import ModelConfiguration, NozzleMount, default_mounts

DEFAULT_DENSITY_KG_M3 = 1200.0
PARAMETER_FIELDS = {f.name for f in fields(ModelConfiguration)} - {
    "mounts",
    "manual_axis",
    "manual_angle_deg",
}


def default_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return base / "BiBaZuAutoTune" / "autotune_defaults.json"


def validate_density(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("Dichte muss eine positive, endliche Zahl in kg/m³ sein.")
    return float(value)


@dataclass(frozen=True)
class ModelDefaults:
    configuration: ModelConfiguration = field(default_factory=ModelConfiguration)
    density_kg_m3: float = DEFAULT_DENSITY_KG_M3


class ModelDefaultsStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_path()

    def _read(self):
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1}
        if (
            not isinstance(raw, dict)
            or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
            or set(raw) - {"schema_version", "model_parameters", "mounts"}
        ):
            raise ValueError("Unbekanntes Format der Autotune-Standardwerte.")
        return raw

    @staticmethod
    def _decode(raw):
        parameters = raw.get("model_parameters", {})
        if not isinstance(parameters, dict) or set(parameters) - PARAMETER_FIELDS - {
            "density_kg_m3"
        }:
            raise ValueError("Ungültige gespeicherte Modellparameter.")
        density = validate_density(parameters.get("density_kg_m3", DEFAULT_DENSITY_KG_M3))
        mounts = default_mounts()
        if "mounts" in raw:
            values = raw["mounts"]
            mount_fields = {f.name for f in fields(NozzleMount)}
            if (
                not isinstance(values, list)
                or len(values) != len(mounts)
                or any(not isinstance(v, dict) or set(v) != mount_fields for v in values)
            ):
                raise ValueError("Ungültige gespeicherte Montagewerte.")
            mounts = tuple(NozzleMount(**v) for v in values)
        try:
            config = ModelConfiguration(
                **{k: v for k, v in parameters.items() if k in PARAMETER_FIELDS}, mounts=mounts
            )
            config.validate()
        except (TypeError, OverflowError) as exc:
            raise ValueError("Ungültige Zahlen in den gespeicherten Standardwerten.") from exc
        return ModelDefaults(config, density)

    def load(self) -> ModelDefaults:
        return self._decode(self._read())

    def save_parameters(self, configuration: ModelConfiguration, density: float):
        config = replace(
            configuration, mounts=default_mounts(), manual_axis=None, manual_angle_deg=90
        )
        config.validate()
        density = validate_density(density)
        raw = self._read()
        self._decode(raw)
        raw["model_parameters"] = {
            key: value for key, value in asdict(config).items() if key in PARAMETER_FIELDS
        }
        raw["model_parameters"]["density_kg_m3"] = density
        self._write(raw)

    def save_mounts(self, mounts: tuple[NozzleMount, ...]):
        ModelConfiguration(mounts=mounts).validate()
        if len(mounts) != len(default_mounts()):
            raise ValueError("Montagewerte für alle acht Array-/Düsenkombinationen erforderlich.")
        raw = self._read()
        self._decode(raw)
        raw["mounts"] = [asdict(m) for m in mounts]
        self._write(raw)

    def _write(self, raw):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name,
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(raw, stream, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
