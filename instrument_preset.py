"""Spatial use of a plane, independent of physical marker IDs and calibration."""
from dataclasses import dataclass, field
from pathlib import Path
import cv2 as cv
import numpy as np
from board_definition import polygon, read_json, safe_name


@dataclass(frozen=True)
class InstrumentPreset:
    preset_id: str
    chords: dict = field(default_factory=dict)
    strum: list = field(default_factory=list)
    axis: list = field(default_factory=list)
    interaction: dict = field(default_factory=dict)
    schema_version: int = 1
    chord_frame: object = None
    strum_frame: object = None
    degree_regions: object = None
    selection_frame: object = None

    def __post_init__(self):
        safe_name(self.preset_id)
        if self.schema_version not in (1, 2):
            raise ValueError('Unsupported preset schema_version')
        if self.schema_version == 1:
            if self.degree_regions is not None or self.selection_frame is not None:
                raise ValueError('Degree regions require preset schema_version 2')
            if not isinstance(self.chords, dict) or not self.chords or set(self.chords)-{'C', 'G', 'Am', 'F'}:
                raise ValueError('Legacy presets support chord regions C, G, Am, F')
            object.__setattr__(self, 'chords', {name: polygon(points, name).tolist() for name, points in self.chords.items()})
        else:
            if self.chords or self.chord_frame is not None:
                raise ValueError('Schema 2 uses degree_regions and selection_frame')
            if not isinstance(self.degree_regions, dict) or set(self.degree_regions) != set(map(str, range(1, 8))):
                raise ValueError('Exactly seven degree_regions (1..7) are required')
            object.__setattr__(self, 'degree_regions', {str(i): polygon(self.degree_regions[str(i)], str(i)).tolist() for i in range(1, 8)})
            if self.selection_frame is None:
                raise ValueError('selection_frame is required')
        object.__setattr__(self, 'strum', polygon(self.strum, 'strum').tolist())
        axis = np.asarray(self.axis, dtype=np.float64)
        if axis.shape != (2, 2) or not np.isfinite(axis).all() or np.linalg.norm(axis[1]-axis[0]) < 1e-6:
            raise ValueError('axis must contain two different finite points')
        if not isinstance(self.interaction, dict):
            raise ValueError('interaction must be an object')
        for name in ('chord_frame', 'strum_frame', 'selection_frame'):
            value = getattr(self, name)
            if value is not None:
                points = polygon(value, name, convex=True)
                if len(points) != 4:
                    raise ValueError(f'{name} must contain four ordered corners')
                transform = frame_transform(points)
                if name == 'strum_frame' and self.schema_version == 2 and any(
                        crosses_projective_horizon(shape, transform) for shape in (self.strum, axis)):
                    raise ValueError('Strum polygon or axis crosses its local projective horizon')
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

    @property
    def regions(self):
        return self.chords if self.schema_version == 1 else self.degree_regions

    @property
    def region_frame(self):
        return self.chord_frame if self.schema_version == 1 else self.selection_frame

    def for_live(self):
        """Upgrade only known geometry, without writing the user's file."""
        if self.schema_version == 2:
            return self
        frame = self.chord_frame
        if frame is None:
            expected = {name: [[.04+i*.14, .2], [.04+(i+1)*.14, .2],
                               [.04+(i+1)*.14, .8], [.04+i*.14, .8]]
                        for i, name in enumerate(('C', 'G', 'Am', 'F'))}
            if set(self.chords) == set(expected) and all(
                    np.asarray(self.chords[k]).shape == (4, 2) and np.allclose(self.chords[k], v)
                    for k, v in expected.items()):
                frame = [[.04, .2], [.60, .2], [.60, .8], [.04, .8]]
        if frame is None:
            raise ValueError('旧自定义预设缺少选音区四角，请重新划定选音区。')
        return self.from_frames(self.preset_id, frame, self.strum, self.axis,
                               self.interaction, self.strum_frame)

    @classmethod
    def from_frames(cls, preset_id, selection, strum, axis, interaction=None, strum_frame=None):
        inverse = np.linalg.inv(frame_transform(selection))
        regions = {}
        for i in range(7):
            points = np.array([[i/7, 0], [(i+1)/7, 0], [(i+1)/7, 1], [i/7, 1]])
            homogeneous = np.column_stack((points, np.ones(4))) @ inverse.T
            regions[str(i+1)] = (homogeneous[:, :2]/homogeneous[:, 2:]).tolist()
        return cls(preset_id=preset_id, schema_version=2, degree_regions=regions,
                   selection_frame=selection, strum=strum, axis=axis,
                   interaction=interaction or {}, strum_frame=strum_frame)

    def to_dict(self):
        result = dict(schema_version=self.schema_version, preset_id=self.preset_id,
                      strum=self.strum, axis=self.axis, interaction=self.interaction)
        result['chords' if self.schema_version == 1 else 'degree_regions'] = self.regions
        for name in ('chord_frame', 'strum_frame', 'selection_frame'):
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


def crosses_projective_horizon(points, matrix):
    denominators = np.asarray(points) @ matrix[2, :2] + matrix[2, 2]
    tolerance = 1e-10 * np.max(np.abs(matrix))
    return not (denominators.min() > tolerance or denominators.max() < -tolerance)


def signed_distance(point, points):
    p = np.asarray(point, dtype=np.float64)
    if p.shape != (2,) or not np.isfinite(p).all():
        return -np.inf
    return cv.pointPolygonTest(np.float32(points), (float(p[0]), float(p[1])), True)
