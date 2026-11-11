"""A confirmed or staged part selection shared by the operator UI and analysis."""

from dataclasses import dataclass
from pathlib import Path

from .geometry import MeshAnalysis


@dataclass(frozen=True)
class WorkpieceSelection:
    context: dict
    path: Path | None
    unit: str = "mm"
    analysis: MeshAnalysis | None = None
