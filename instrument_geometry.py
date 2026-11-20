"""One board layout and one projective geometry for interaction and drawing.

Region dimensions reference MotionMuse/SMF-an; see docs/integration/BASELINE.md.
UV is a 2-D image projection, not a contact or depth measurement.
"""
from dataclasses import dataclass
from copy import deepcopy
import numpy as np
from instrument_preset import InstrumentPreset, crosses_projective_horizon, frame_transform, signed_distance

CHORDS = ('C', 'G', 'Am', 'F')
BOARD_UV = np.float32(((0, 0), (1, 0), (0, 1), (1, 1)))
PERIMETER = (0, 1, 3, 2)


def raw_to_display_xy(points, width, mirrored):
    result = np.asarray(points, dtype=np.float64).copy()
    if mirrored:
        result[..., 0] = width - 1 - result[..., 0]
    return result


display_to_raw_xy = raw_to_display_xy  # A horizontal reflection is its own inverse.


def project(points, matrix):
    """Return Nx2, or None for invalid H/points/near-zero denominator. Never clip UV."""
    if matrix is None:
        return None
    try:
        points = np.asarray(points, dtype=np.float64)
        matrix = np.asarray(matrix, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 2 or matrix.shape != (3, 3):
            return None
        if not np.isfinite(points).all() or not np.isfinite(matrix).all():
            return None
        scale = np.max(np.abs(matrix))
        if scale == 0:
            return None
        matrix = matrix / scale
        homogeneous = np.column_stack((points, np.ones(len(points)))) @ matrix.T
        denominator = homogeneous[:, 2]
        if np.any(np.abs(denominator) < 1e-10):
            return None
        with np.errstate(over='ignore', invalid='ignore'):
            result = homogeneous[:, :2] / denominator[:, None]
        return result if np.isfinite(result).all() else None
    except (TypeError, ValueError):
        return None


camera_to_instrument_uv = project


def instrument_to_display_xy(points, inverse, width, mirrored):
    raw = project(points, inverse)
    return None if raw is None else raw_to_display_xy(raw, width, mirrored)


@dataclass(frozen=True)
class InstrumentLayout:
    """flip_u reflects regions, never anatomical handedness or published board UV."""
    flip_u: bool = False
    chord_u: tuple = (.04, .60)
    chord_v: tuple = (.20, .80)
    strum_u: tuple = (.68, .90)
    strum_v: tuple = (.15, .85)
    axis_v: float = .5
    preset: object = None

    def __post_init__(self):
        if self.preset is None:
            preset = InstrumentPreset.default()
            # Preserve callers using the old rectangular layout constructor.
            if (self.chord_u, self.chord_v, self.strum_u, self.strum_v, self.axis_v) != ((.04, .60), (.20, .80), (.68, .90), (.15, .85), .5):
                width = (self.chord_u[1]-self.chord_u[0])/4
                rect = lambda x0, x1, y0, y1: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
                preset = InstrumentPreset('legacy_custom',
                    {name: rect(self.chord_u[0]+i*width, self.chord_u[0]+(i+1)*width, *self.chord_v) for i, name in enumerate(CHORDS)},
                    rect(*self.strum_u, *self.strum_v), [[u, self.axis_v] for u in self.strum_u])
            object.__setattr__(self, 'preset', preset)
        chord_transform = None if self.preset.region_frame is None else frame_transform(self.preset.region_frame)
        strum_transform = None if self.preset.strum_frame is None else frame_transform(self.preset.strum_frame)
        object.__setattr__(self, '_chord_transform', chord_transform)
        object.__setattr__(self, '_strum_transform', strum_transform)
        chords = {name: points if chord_transform is None else project(points, chord_transform)
                  for name, points in self.preset.regions.items()}
        axis = self.preset.axis if strum_transform is None else project(self.preset.axis, strum_transform)
        if any(points is None for points in chords.values()) or axis is None:
            raise ValueError('Region or axis crosses its local projective horizon')
        strum = self.preset.strum
        if strum_transform is not None:
            # A historical preset can rely on board-space band checks during replay.
            strum = None if crosses_projective_horizon(strum, strum_transform) else project(strum, strum_transform)
        object.__setattr__(self, '_chords', chords)
        object.__setattr__(self, '_axis', np.asarray(axis))
        object.__setattr__(self, '_strum_polygon', strum)

    def local(self, point):
        u, v = point
        return (1 - u if self.flip_u else u), v

    def chord_at(self, point, margin=0.0):
        point = self._region_point(point, self._chord_transform)
        for name, points in self._chords.items():
            if signed_distance(point, points) >= margin-1e-8:
                return name
        return None

    def contains_chord(self, chord, point, margin=0.):
        points = self._chords.get(chord)
        return points is not None and signed_distance(self._region_point(point, self._chord_transform), points) >= margin-1e-8

    def selection_margin(self, key, legacy_margin, fraction):
        """New seven-cell margins scale with one cell, not the whole selection frame."""
        if fraction is None or self.preset.schema_version == 1 or key not in self._chords:
            return legacy_margin
        extent = np.ptp(np.asarray(self._chords[key]), axis=0)
        return min(legacy_margin, float(extent.min())*fraction)

    def _region_point(self, point, matrix):
        point = self.local(point)
        if matrix is None:
            return np.asarray(point)
        mapped = project([point], matrix)
        return np.full(2, np.nan) if mapped is None else mapped[0]

    def strum_motion_point(self, point):
        return self._region_point(point, self._strum_transform)

    def in_strum_band(self, point):
        return signed_distance(self.local(point), self.preset.strum) >= -1e-8

    def in_strum_tracking_zone(self, point, margin=.35):
        """Observe approach/departure, but require an actual finite-axis crossing to play."""
        return (self._strum_polygon is not None and
                signed_distance(self.strum_motion_point(point), self._strum_polygon) >= -margin-1e-8)

    def strum_path_crosses(self, first, second):
        """Does the observed segment intersect the finite strum axis inside its region?"""
        if self._strum_polygon is None:
            return False
        p, q = self.strum_motion_point(first), self.strum_motion_point(second)
        if not np.isfinite([p, q]).all():
            return False
        a, b = self._axis
        cross = lambda u, v: float(u[0]*v[1]-u[1]*v[0])
        denominator = cross(q-p, b-a)
        if abs(denominator) < 1e-10:
            return False
        t, u = cross(a-p, b-a)/denominator, cross(a-p, q-p)/denominator
        if not (0 <= t <= 1 and 0 <= u <= 1):
            return False
        return signed_distance(p+t*(q-p), self._strum_polygon) >= -1e-8

    def strum_coordinate(self, point):
        """Signed distance along the directed axis normal; .5 keeps v-threshold compatibility."""
        a, b = self._axis
        tangent = b-a
        normal = np.array([-tangent[1], tangent[0]])/np.linalg.norm(tangent)
        return .5+float(np.dot(self.strum_motion_point(point)-a, normal))

    def _polygon(self, x0, x1, y0, y1):
        return [list(self.local(p)) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]

    def regions(self):
        transform = lambda points: [list(self.local(p)) for p in points]
        return {'name': 'flip-u' if self.flip_u else 'standard', 'preset_id': self.preset.preset_id,
                'degree_regions': {name: transform(points) for name, points in (self.preset.degree_regions or {}).items()},
                'chords': {name: transform(points) for name, points in self.preset.chords.items()},
                'strum': transform(self.preset.strum), 'axis': transform(self.preset.axis)}


def fuse_hands(packet, instrument):
    """Schema 3 distinguishes mathematical projection from permission to interact."""
    matrix = instrument['homography_camera_to_instrument']
    for hand in packet['hands']:
        raw = display_to_raw_xy(np.asarray(hand['landmarks_px'])[:, :2],
                                packet['frame_width'], packet['mirrored'])
        uv = project(raw, matrix)
        hand['landmarks_instrument_uv'] = uv.tolist() if uv is not None and uv.shape == (21, 2) else None
        available = hand['landmarks_instrument_uv'] is not None
        hand['projection_available'] = available
        hand['projection_status'] = instrument.get('status', 'unavailable') if available else 'unavailable'
        hand['interaction_eligible'] = available and instrument.get('status') == 'valid'
    packet.update(schema_version=3, instrument=instrument)


def legacy_packet(packet):
    """Explicit schema-2 adapter for existing /api/landmarks and /api/state clients."""
    result = deepcopy(packet)
    result['schema_version'] = 2
    for hand in result.get('hands', []):
        hand['projection_status'] = 'valid' if hand.get('landmarks_instrument_uv') is not None else 'unavailable'
    instrument = result.get('instrument', {})
    if instrument.get('status') == 'degraded':
        instrument['status'] = 'held'
    return result


def overlay_polygons(packet, layout):
    """Return the exact polygons used by both the layout hit-test and the renderer."""
    regions = layout.regions()
    result = {}
    shapes = dict(regions['degree_regions'] or regions['chords'], strum=regions['strum'], axis=regions['axis'],
                  board=packet['instrument'].get('outline_uv') or BOARD_UV[list(PERIMETER)])
    for name, points in shapes.items():
        projected = instrument_to_display_xy(points, packet['instrument']['homography_instrument_to_camera'],
                                             packet['frame_width'], packet['mirrored'])
        result[name] = None if projected is None else projected.tolist()
    return result
