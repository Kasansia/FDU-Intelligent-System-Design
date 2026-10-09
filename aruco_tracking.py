"""Locate an instrument plane using four marker centers in UNMIRRORED frames."""
from collections import Counter
import time
import cv2 as cv
import numpy as np
from instrument_coordinates import flip_frame_xy, project_xy

REQUIRED_IDS = (0, 1, 2, 3)
INSTRUMENT_CORNERS = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=np.float32)
PERIMETER = [0, 1, 3, 2]


def unavailable_instrument(reason='No camera frame'):
    return {'status': 'lost', 'dictionary': 'DICT_4X4_50',
            'required_ids': list(REQUIRED_IDS), 'visible_ids': [], 'markers': [],
            'homography_camera_to_instrument': None,
            'homography_instrument_to_camera': None, 'homography_age_s': None,
            'reason': reason, 'aruco_ms': 0.0}


def homography_from_centers(centers, min_area_px=400.0, min_separation_px=8.0):
    """Centers MUST be ordered by ID 0,1,2,3, regardless of image orientation.

    Reject duplicate/nearby centers, non-convex or self-intersecting perimeter,
    small area and ill-defined transforms rather than publishing plausible UV.
    """
    points = np.asarray(centers, dtype=np.float64)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError('invalid_centers')
    distances = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    if np.any(distances[np.triu_indices(4, k=1)] < min_separation_px):
        raise ValueError('centers_too_close')
    polygon = points[PERIMETER].astype(np.float32)
    if not cv.isContourConvex(polygon):
        raise ValueError('non_convex_or_crossed_layout')
    if abs(cv.contourArea(polygon)) < min_area_px:
        raise ValueError('plane_area_too_small')
    edges = np.roll(polygon, -1, axis=0) - polygon
    following = np.roll(edges, -1, axis=0)
    cross = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    sine = np.abs(cross) / (np.linalg.norm(edges, axis=1) * np.linalg.norm(following, axis=1))
    if np.min(sine) < .01:
        raise ValueError('nearly_collinear_centers')
    matrix = cv.getPerspectiveTransform(points.astype(np.float32), INSTRUMENT_CORNERS)
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise ValueError('singular_homography') from exc
    if not np.isfinite(matrix).all() or not np.isfinite(inverse).all():
        raise ValueError('non_finite_homography')
    mapped = project_xy(points, matrix)
    restored = project_xy(INSTRUMENT_CORNERS, inverse)
    if (mapped is None or restored is None or
            not np.allclose(mapped, INSTRUMENT_CORNERS, atol=1e-4) or
            not np.allclose(restored, points, atol=.05)):
        raise ValueError('unstable_homography')
    # A horizon must not cross the playing quadrilateral.
    denominators = np.c_[points, np.ones(4)] @ matrix[2]
    if not (np.all(denominators > 0) or np.all(denominators < 0)):
        raise ValueError('horizon_crosses_plane')
    return matrix, inverse


class InstrumentTracker:
    def __init__(self, hold_s=.25, min_area_px=400.0, clock=time.monotonic):
        if not np.isfinite(hold_s) or hold_s < 0 or not np.isfinite(min_area_px) or min_area_px <= 0:
            raise ValueError('Invalid hold time or minimum plane area')
        dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
        parameters = cv.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = cv.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv.aruco.ArucoDetector(dictionary, parameters)
        self.hold_s, self.min_area_px, self.clock = hold_s, min_area_px, clock
        self.reset()

    def reset(self):
        """Forget geometry on camera errors, resize or explicitly rejected layouts."""
        self._matrix = self._inverse = self._last_valid = self._shape = None

    def process(self, raw_bgr):
        """Return a JSON-compatible packet; never detect on a flipped frame.

        The injected monotonic clock permits deterministic expiry tests. Missing
        markers may hold an old transform; bad geometry or duplicate required
        IDs invalidate it immediately. A frame-size change also clears history.
        """
        started = time.perf_counter()
        now = float(self.clock())
        if not np.isfinite(now):
            raise ValueError('Clock must return finite seconds')
        shape = raw_bgr.shape[:2]
        if self._shape != shape or (self._last_valid is not None and now < self._last_valid):
            self.reset()
        self._shape = shape
        corners, ids, _ = self.detector.detectMarkers(raw_bgr)
        packet = unavailable_instrument('missing_markers')
        for marker_id, corner in zip([] if ids is None else ids.flatten(), corners):
            xy = np.asarray(corner, dtype=np.float64).reshape(4, 2)
            if np.isfinite(xy).all():
                packet['markers'].append({'id': int(marker_id), 'corners_px_raw': xy.tolist(),
                                          'center_px_raw': xy.mean(axis=0).tolist()})
        packet['markers'].sort(key=lambda marker: marker['id'])
        counts = Counter(marker['id'] for marker in packet['markers'])
        packet['visible_ids'] = sorted(counts)
        duplicate_ids = [i for i in REQUIRED_IDS if counts[i] > 1]
        if duplicate_ids:
            self.reset()
            packet['reason'] = 'duplicate_required_ids'
        elif all(counts[i] == 1 for i in REQUIRED_IDS):
            by_id = {m['id']: m['center_px_raw'] for m in packet['markers']}
            try:
                self._matrix, self._inverse = homography_from_centers(
                    [by_id[i] for i in REQUIRED_IDS], self.min_area_px)
                self._last_valid = now
                packet.update(status='valid', reason=None)
            except ValueError as exc:
                self.reset()
                packet['reason'] = str(exc)
        elif self._last_valid is not None and now - self._last_valid <= self.hold_s:
            packet.update(status='held', reason='temporary_marker_occlusion')
        if self._last_valid is not None:
            packet['homography_age_s'] = round(now - self._last_valid, 6)
        if packet['status'] in ('valid', 'held'):
            packet['homography_camera_to_instrument'] = self._matrix.tolist()
            packet['homography_instrument_to_camera'] = self._inverse.tolist()
        packet['aruco_ms'] = round((time.perf_counter() - started) * 1000, 3)
        return packet


def annotate_instrument(display, instrument, mirrored):
    """Draw raw detections on display coordinates; never redetect after mirroring."""
    canvas = display.copy()
    width = canvas.shape[1]
    color = {'valid': (90, 220, 90), 'held': (0, 190, 255), 'lost': (90, 90, 245)}[instrument['status']]
    for marker in instrument['markers']:
        if marker['id'] not in REQUIRED_IDS:
            continue
        points = np.rint(flip_frame_xy(marker['corners_px_raw'], width, mirrored)).astype(np.int32)
        center = np.rint(flip_frame_xy([marker['center_px_raw']], width, mirrored)[0]).astype(int)
        cv.polylines(canvas, [points], True, color, 2, cv.LINE_AA)
        cv.circle(canvas, tuple(center), 4, color, -1, cv.LINE_AA)
        cv.putText(canvas, f"ID {marker['id']}", (int(center[0])+6, int(center[1])-8),
                   cv.FONT_HERSHEY_SIMPLEX, .5, color, 2, cv.LINE_AA)
    inverse = instrument['homography_instrument_to_camera']
    if inverse is not None:
        raw = project_xy(INSTRUMENT_CORNERS[PERIMETER], inverse)
        if raw is not None:
            points = np.rint(flip_frame_xy(raw, width, mirrored)).astype(np.int32)
            # The held outline is explicitly an old estimate, drawn in amber.
            cv.polylines(canvas, [points], True, color, 2, cv.LINE_AA)
    visible = len(set(instrument['visible_ids']) & set(REQUIRED_IDS))
    label = f"Instrument: {instrument['status'].upper()}  {visible}/4"
    if instrument['status'] == 'held':
        label += f"  age {instrument['homography_age_s']:.2f}s"
    cv.rectangle(canvas, (0, 0), (min(width-1, 410), 32), (20, 25, 30), -1)
    cv.putText(canvas, label, (8, 23), cv.FONT_HERSHEY_SIMPLEX, .55, color, 1, cv.LINE_AA)
    return canvas
