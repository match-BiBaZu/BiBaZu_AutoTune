"""Public parameter schema, compatible with the version-1 pressure API."""

from typing import Annotated

from pydantic import Field, model_validator

from .api_runtime import StrictModel


class ArrayParameters(StrictModel):
    index: Annotated[int, Field(ge=1, le=4)]
    enabled: bool
    nozzles_enabled: Annotated[list[bool], Field(min_length=6, max_length=6)]
    pressure_mbar: Annotated[int, Field(ge=0, le=6000)]
    pulse_duration_ms: Annotated[int, Field(ge=1, le=500)]
    delay_ms: Annotated[int, Field(ge=0, le=1000)]
    offset_mm: Annotated[float, Field(ge=0, le=5000)]
    conveyor_velocity_delay: bool


class Parameters(StrictModel):
    arrays: Annotated[list[ArrayParameters], Field(min_length=4, max_length=4)]
    conveyor_speed_mm_per_sec: Annotated[float, Field(ge=0, le=200)]

    @model_validator(mode="after")
    def check_arrays(self):
        if sorted(a.index for a in self.arrays) != [1, 2, 3, 4]:
            raise ValueError("Genau die Arrays 1 bis 4 sind erforderlich.")
        if self.conveyor_speed_mm_per_sec == 0 and any(
            a.enabled and a.conveyor_velocity_delay for a in self.arrays
        ):
            raise ValueError("Offset mit Bandgeschwindigkeit benötigt mehr als 0 mm/s.")
        return self
