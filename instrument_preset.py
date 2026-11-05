"""Spatial use of a plane, independent of physical marker IDs and calibration."""
from dataclasses import dataclass, field
from pathlib import Path
import cv2 as cv
import numpy as np
from board_definition import polygon, read_json, safe_name


@dataclass(frozen=True)
class InstrumentPreset:
    preset_id: str
    chords: dict
    strum: list
    axis: list
    interaction: dict = field(default_factory=dict)
    schema_version: int = 1
    chord_frame: object = None
    strum_frame: object = None

    def __post_init__(self):
        safe_name(self.preset_id)
        if self.schema_version != 1:
            raise ValueError('Unsupported preset schema_version')
        if not isinstance(self.chords, dict) or not self.chords or set(self.chords)-{'C', 'G', 'Am', 'F'}:
            raise ValueError('This audio mode supports chord regions C, G, Am, F')
        clean = {name: polygon(points, name).tolist() for name, points in self.chords.items()}
        object.__setattr__(self, 'chords', clean)
        object.__setattr__(self, 'strum', polygon(self.strum, 'strum').tolist())
        axis = np.asarray(self.axis, dtype=np.float64)
        if axis.shape != (2, 2) or not np.isfinite(axis).all() or np.linalg.norm(axis[1]-axis[0]) < 1e-6:
            raise ValueError('axis must contain two different finite points')
        if not isinstance(self.interaction, dict):
            raise ValueError('interaction must be an object')
        for name in ('chord_frame', 'strum_frame'):
            value = getattr(self, name)
            if value is not None:
                points = polygon(value, name, convex=True)
                if len(points) != 4:
                    raise ValueError(f'{name} must contain four ordered corners')
                frame_transform(points)  # Reject singular/projectively unstable frames at load.
                object.__setattr__(self, name, points.tolist())
        # Validate fields with the existing action state machine, without importing it during module initialization.
        from instrument_logic import InteractionConfig
        try:
            InteractionConfig(**self.interaction)
        except TypeError as exc:
            raise ValueError(f'Invalid interaction settings: {exc}') from exc

    @classmethod
    def from_dict(cls, value):
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError(f'Invalid preset fields: {exc}') from exc

    @classmethod
    def load(cls, path):
        return cls.from_dict(read_json(path))

    @classmethod
    def default(cls):
        return cls.load(Path(__file__).resolve().parent/'config/presets/guitar.json')

    def to_dict(self):
        result = dict(schema_version=1, preset_id=self.preset_id, chords=self.chords,
                      strum=self.strum, axis=self.axis, interaction=self.interaction)
        for name in ('chord_frame', 'strum_frame'):
            if getattr(self, name) is not None:
                result[name] = getattr(self, name)
        return result


def frame_transform(points):
    """Plane UV -> region-local unit square; corner order defines playing direction."""
    matrix = cv.getPerspectiveTransform(np.float32(points), np.float32([[0, 0], [1, 0], [1, 1], [0, 1]]))
    denominator = np.column_stack((points, np.ones(4))) @ matrix[2]
    if (not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12 or
            denominator.min()*denominator.max() <= 0):
        raise ValueError('Unstable region coordinate frame')
    return matrix


def signed_distance(point, points):
    p = np.asarray(point, dtype=np.float64)
    if p.shape != (2,) or not np.isfinite(p).all():
        return -np.inf
    return cv.pointPolygonTest(np.float32(points), (float(p[0]), float(p[1])), True)
