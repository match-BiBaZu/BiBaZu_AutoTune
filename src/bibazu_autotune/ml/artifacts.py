"""Portable GP artifacts and their compatibility checks."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import numpy as np

from .. import model_defaults
from .features import FEATURE_VERSION, FEATURES

MODEL_KIND = "gp_physics_classifier"
FLIP_CLASSES = ("not_flipped", "flipped", "overflipped")
SCHEMA_VERSION = 2
FEATURE_SCHEMA = [{"name": name, "unit": unit} for name, unit in FEATURES]
GP_TRUSTED_TYPES = (
    "sklearn.gaussian_process._gpc._BinaryGaussianProcessClassifierLaplace",
    "sklearn.gaussian_process.kernels.ConstantKernel",
    "sklearn.gaussian_process.kernels.Product",
    "sklearn.gaussian_process.kernels.RBF",
)


def model_directory():
    return model_defaults.default_path().parent / "ml_models"


def library_versions():
    return {name: version(name) for name in ("numpy", "scipy", "scikit-learn", "skops")}


def read_metadata(directory):
    data = json.loads((Path(directory) / "metadata.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Ungültige Modellmetadaten.")
    return data


def save_model(estimator, metadata, directory=None):
    from skops.io import dump

    trained_classes = set(estimator.classes_)
    classes = tuple(label for label in FLIP_CLASSES if label in trained_classes)
    if trained_classes != set(classes) or "flipped" not in classes or len(classes) < 2:
        raise ValueError("Das Flipmodell benötigt Flips und mindestens eine Gegenklasse.")
    directory = Path(directory) if directory is not None else model_directory()
    directory.mkdir(parents=True, exist_ok=True)
    model_id = uuid4().hex
    target = directory / model_id
    metadata = {
        **metadata,
        # Three-class exports also work with readers of the original format.
        "schema_version": 1 if classes == FLIP_CLASSES else SCHEMA_VERSION,
        "model_kind": MODEL_KIND,
        "model_id": model_id,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_version": FEATURE_VERSION,
        "features": FEATURE_SCHEMA,
        "classes": classes,
        "libraries": library_versions(),
    }
    with TemporaryDirectory(prefix=".training-", dir=directory) as staging:
        bundle = Path(staging) / "bundle"
        bundle.mkdir()
        dump(estimator, bundle / "model.skops")
        (bundle / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        bundle.rename(target)
    return target


@dataclass(frozen=True)
class FlipModel:
    estimator: object
    metadata: dict
    feature_bounds: np.ndarray

    @property
    def classes(self):
        return tuple(self.metadata["classes"])

    def probabilities(self, features):
        prediction = self.estimator.predict_proba(features)
        # scikit-learn chooses its own column order.
        classes = list(self.estimator.classes_)
        return prediction[:, [classes.index(label) for label in self.classes]]


def load_model(directory):
    from sklearn.gaussian_process import GaussianProcessClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from skops.io import get_untrusted_types, loads

    directory = Path(directory)
    metadata = read_metadata(directory)
    schema_version = metadata.get("schema_version")
    if schema_version not in (1, SCHEMA_VERSION):
        raise ValueError("Unbekanntes Modellformat.")
    if metadata.get("model_kind") != MODEL_KIND:
        raise ValueError("Dieses Modell wird von AutoTune nicht unterstützt.")
    if (
        metadata.get("feature_version") != FEATURE_VERSION
        or metadata.get("features") != FEATURE_SCHEMA
    ):
        raise ValueError("Das Modell verwendet einen anderen Merkmalsatz. Bitte erneut trainieren.")
    if metadata.get("libraries") != library_versions():
        raise ValueError(
            "Die Bibliotheksversionen passen nicht zum Modell. Beide Programme abgleichen "
            "und das Modell erneut trainieren."
        )
    classes = metadata.get("classes")
    if (
        not isinstance(classes, list)
        or "flipped" not in classes
        or len(classes) < 2
        or classes != [label for label in FLIP_CLASSES if label in classes]
        or (schema_version == 1 and classes != list(FLIP_CLASSES))
    ):
        raise ValueError("Ungültige Flipklassen in der Modelldatei.")
    bounds = np.asarray(metadata.get("feature_bounds"), dtype=float)
    if (
        bounds.shape != (len(FEATURES), 2)
        or not np.isfinite(bounds).all()
        or (bounds[:, 0] > bounds[:, 1]).any()
        or not isinstance(metadata.get("part_groups"), list)
        or not isinstance(metadata.get("validation"), dict)
    ):
        raise ValueError("Ungültiger Trainingsbereich oder fehlende Prüfmetadaten.")

    data = (directory / "model.skops").read_bytes()
    untrusted = get_untrusted_types(data=data)
    if set(untrusted).difference(GP_TRUSTED_TYPES):
        raise ValueError("Die Modelldatei enthält nicht unterstützte Typen.")
    estimator = loads(data, trusted=untrusted)
    if (
        type(estimator) is not Pipeline
        or len(estimator.steps) != 2
        or type(estimator.steps[0][1]) is not StandardScaler
        or type(estimator.steps[1][1]) is not GaussianProcessClassifier
        or estimator.steps[1][1].multi_class != "one_vs_rest"
        or set(getattr(estimator, "classes_", ())) != set(classes)
        or getattr(estimator, "n_features_in_", None) != len(FEATURES)
    ):
        raise ValueError("Die geladene Pipeline passt nicht zum Flipmodell.")
    return FlipModel(estimator, metadata, bounds)
