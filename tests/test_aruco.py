"""Camera-free integration checks using freshly generated ArUco images."""
import copy
import csv
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
import cv2 as cv
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aruco_tracking import InstrumentTracker, homography_from_centers, annotate_instrument
from instrument_coordinates import flip_frame_xy, project_xy, hand_to_instrument_uv, fuse_instrument_coordinates
from app import CSV_FIELDS, landmark_csv_rows
from hand_tracking import NAMES

CENTERS = np.array([[100, 80], [540, 80], [100, 400], [540, 400]], np.float64) - .5
UV_CORNERS = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], np.float64)


def board(hidden=(), rotations=(0, 0, 0, 0), extras=()):
    image = np.full((480, 640, 3), 255, np.uint8)
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    placements = [(i, tuple((CENTERS[i]+.5).astype(int)), rotations[i]) for i in range(4) if i not in hidden]
    placements += [(i, center, 0) for i, center in extras]
    for marker_id, (x, y), rotation in placements:
        marker = cv.aruco.generateImageMarker(dictionary, marker_id, 60)
        image[y-30:y+30, x-30:x+30] = np.rot90(marker, rotation)[..., None]
    return image


def hand_packet(raw_xy=(319.5, 239.5), mirrored=True):
    xy = flip_frame_xy([raw_xy], 640, mirrored)[0].tolist()
    return {'schema_version': 1, 'frame_id': 5, 'timestamp_unix_s': 100.,
            'frame_width': 640, 'frame_height': 480, 'mirrored': mirrored,
            'inference_ms': 12.5, 'landmark_names': NAMES,
            'hands': [{'handedness': 'Left', 'confidence': .95, 'track_id': 2,
                       'landmarks_px': [xy + [-3.0] for _ in range(21)],
                       'landmarks_normalized': [[xy[0]/640, xy[1]/480, -3/640] for _ in range(21)],
                       'landmarks_world_m': [[.01, .02, .03] for _ in range(21)]}]}


class ArucoTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.tracker = InstrumentTracker(clock=lambda: self.now)

    def test_detection_centers_and_interior_point(self):
        packet = self.tracker.process(board())
        self.assertEqual(packet['status'], 'valid')
        self.assertEqual(packet['visible_ids'], [0, 1, 2, 3])
        centers = np.array([m['center_px_raw'] for m in packet['markers']])
        for marker in packet['markers']:
            np.testing.assert_allclose(np.mean(marker['corners_px_raw'], axis=0), marker['center_px_raw'])
        np.testing.assert_allclose(project_xy(centers, packet['homography_camera_to_instrument']), UV_CORNERS, atol=1e-5)
        np.testing.assert_allclose(project_xy([[319.5, 239.5]], packet['homography_camera_to_instrument']), [[.5, .5]], atol=.002)
        np.testing.assert_allclose(project_xy(UV_CORNERS, packet['homography_instrument_to_camera']), centers, atol=.01)
        json.dumps(packet, allow_nan=False)

    def test_perspective_and_translation_preserve_uv(self):
        raw_corners = np.float32([[0, 0], [639, 0], [639, 479], [0, 479]])
        target = np.float32([[160, 70], [715, 150], [720, 570], [60, 510]])
        warp = cv.getPerspectiveTransform(raw_corners, target)
        transformed = cv.warpPerspective(board(), warp, (800, 650), borderValue=(255, 255, 255))
        packet = self.tracker.process(transformed)
        self.assertEqual(packet['status'], 'valid')
        original_point = CENTERS[0] + [.3*440, .65*320]
        warped_point = project_xy([original_point], warp)
        np.testing.assert_allclose(project_xy(warped_point, packet['homography_camera_to_instrument']), [[.3, .65]], atol=.008)
        shifted = cv.warpAffine(board(), np.float32([[1, 0, 120], [0, 1, 60]]), (800, 600), borderValue=(255, 255, 255))
        shifted_packet = self.tracker.process(shifted)
        np.testing.assert_allclose(project_xy([original_point+[120, 60]], shifted_packet['homography_camera_to_instrument']), [[.3, .65]], atol=.002)

    def test_plane_rotation_uses_ids_not_screen_order(self):
        for mode in [cv.ROTATE_180, cv.ROTATE_90_CLOCKWISE]:
            with self.subTest(mode=mode):
                packet = self.tracker.process(cv.rotate(board(), mode))
                self.assertEqual(packet['status'], 'valid')
                centers = [m['center_px_raw'] for m in packet['markers']]
                np.testing.assert_allclose(project_xy(centers, packet['homography_camera_to_instrument']), UV_CORNERS, atol=1e-5)

    def test_individual_marker_rotation_does_not_define_axes(self):
        packet = self.tracker.process(board(rotations=(1, 2, 3, 1)))
        self.assertEqual(packet['status'], 'valid')
        np.testing.assert_allclose(project_xy(CENTERS, packet['homography_camera_to_instrument']), UV_CORNERS, atol=.002)

    def test_hold_expiry_and_recovery(self):
        initial = self.tracker.process(board())
        self.now += .1
        held = self.tracker.process(board(hidden=(1,)))
        self.assertEqual(held['status'], 'held')
        self.assertEqual(held['homography_camera_to_instrument'], initial['homography_camera_to_instrument'])
        self.assertAlmostEqual(held['homography_age_s'], .1)
        self.assertIsNotNone(fuse_instrument_coordinates(hand_packet(), held)['hands'][0]['landmarks_instrument_uv'])
        self.now = 10.251
        lost = self.tracker.process(board(hidden=(1,)))
        self.assertEqual(lost['status'], 'lost')
        self.assertIsNone(lost['homography_camera_to_instrument'])
        self.assertIsNone(lost['homography_instrument_to_camera'])
        self.assertIsNone(fuse_instrument_coordinates(hand_packet(), lost)['hands'][0]['landmarks_instrument_uv'])
        self.now = 10.3
        self.assertEqual(self.tracker.process(board())['status'], 'valid')

    def test_missing_marker_cannot_initialize(self):
        packet = self.tracker.process(board(hidden=(3,)))
        self.assertEqual(packet['status'], 'lost')
        self.assertIsNone(packet['homography_age_s'])

    def test_extra_ids_cannot_replace_required_id(self):
        packet = self.tracker.process(board(extras=[(9, (320, 240))]))
        self.assertEqual(packet['visible_ids'], [0, 1, 2, 3, 9])
        np.testing.assert_allclose(project_xy(CENTERS, packet['homography_camera_to_instrument']), UV_CORNERS, atol=.002)
        missing = InstrumentTracker().process(board(hidden=(3,), extras=[(9, (540, 400))]))
        self.assertEqual(missing['status'], 'lost')

    def test_duplicate_required_ids_fail_closed_without_hold(self):
        self.tracker.process(board())
        self.now += .01
        packet = self.tracker.process(board(extras=[(0, (320, 240))]))
        self.assertEqual(packet['status'], 'lost')
        self.assertEqual(packet['reason'], 'duplicate_required_ids')

    def test_degenerate_geometry_rejected(self):
        configurations = [
            [[0, 0], [0, 0], [0, 100], [100, 100]],
            [[0, 0], [100, 0], [200, 0], [300, 0]],
            [[0, 0], [100, 100], [0, 100], [100, 0]],
            [[0, 0], [10, 0], [0, 10], [10, 10]],
            [[0, 0], [100, 0], [0, 100], [30, 30]],
            [[0, 0], [100, 0], [0, 100], [np.nan, 100]],
        ]
        for points in configurations:
            with self.subTest(points=points), self.assertRaises(ValueError):
                homography_from_centers(points)

    def test_bad_full_layout_immediately_clears_cached_h(self):
        self.tracker.process(board())
        centers = CENTERS[[0, 3, 2, 1]]  # A crossed ID perimeter.
        corners = [np.float32([p+[-10, -10], p+[10, -10], p+[10, 10], p+[-10, 10]])[None] for p in centers]
        self.tracker.detector = Mock()
        self.tracker.detector.detectMarkers.return_value = (corners, np.arange(4).reshape(-1, 1), [])
        packet = self.tracker.process(board())
        self.assertEqual(packet['status'], 'lost')
        self.assertIsNone(packet['homography_camera_to_instrument'])

    def test_resize_and_clock_regression_invalidate_hold(self):
        self.tracker.process(board())
        self.assertEqual(self.tracker.process(np.full((600, 800, 3), 255, np.uint8))['status'], 'lost')
        self.tracker.process(board())
        self.now -= 1
        self.assertEqual(self.tracker.process(board(hidden=(0,)))['status'], 'lost')

    def test_mirror_fusion_and_legacy_fields(self):
        instrument = self.tracker.process(board())
        original = hand_packet(mirrored=False)
        saved = copy.deepcopy(original)
        a = fuse_instrument_coordinates(original, instrument)
        b = fuse_instrument_coordinates(hand_packet(mirrored=True), instrument)
        self.assertEqual(original, saved)
        self.assertEqual(a['schema_version'], 2)
        self.assertEqual(a['inference_ms'], original['inference_ms'])
        for key, value in original['hands'][0].items():
            self.assertEqual(a['hands'][0][key], value)
        uv_a, uv_b = [x['hands'][0]['landmarks_instrument_uv'] for x in [a, b]]
        self.assertEqual(np.array(uv_a).shape, (21, 2))
        np.testing.assert_allclose(uv_a, uv_b, atol=1e-9)
        np.testing.assert_allclose(uv_a, [[.5, .5]]*21, atol=.002)
        json.dumps(a, allow_nan=False)

    def test_no_clamp_and_no_fake_point_at_horizon(self):
        instrument = self.tracker.process(board())
        h = instrument['homography_camera_to_instrument']
        uv = hand_to_instrument_uv([[0, 0, 0], [639, 479, 0]], 640, False, h)
        self.assertTrue(all(v < 0 for v in uv[0]))
        self.assertTrue(all(v > 1 for v in uv[1]))
        self.assertIsNone(project_xy([[5, 10]], [[1, 0, 0], [0, 1, 0], [1, 0, -5]]))
        self.assertIsNone(project_xy([[np.inf, 0]], np.eye(3)))

    def test_display_overlay_reflects_raw_marker_positions(self):
        packet = self.tracker.process(board())
        blank = np.full((480, 640, 3), 255, np.uint8)
        normal = annotate_instrument(blank, packet, False)
        mirrored = annotate_instrument(blank, packet, True)
        # Corner ID 0's center and reflected location must receive colored paint.
        for image, x in [(normal, 100), (mirrored, 539)]:
            self.assertFalse(np.all(image[80, x] == 255))
        self.assertTrue(np.all(blank == 255))

    def test_csv_uv_columns_and_blank_when_lost(self):
        for image, available in [(board(), True), (board(hidden=(2,)), False)]:
            instrument = InstrumentTracker().process(image)
            packet = fuse_instrument_coordinates(hand_packet(), instrument)
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(CSV_FIELDS)
            writer.writerows(landmark_csv_rows(packet))
            stream.seek(0)
            rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 21)
            self.assertEqual(rows[0]['z_relative_px'], '-3.0')
            if available:
                self.assertAlmostEqual(float(rows[8]['instrument_u']), .5, places=3)
                self.assertAlmostEqual(float(rows[8]['instrument_v']), .5, places=3)
            else:
                self.assertEqual(rows[8]['instrument_u'], '')
                self.assertEqual(rows[8]['instrument_v'], '')
            json.dumps(packet, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
