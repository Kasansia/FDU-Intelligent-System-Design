import json
import unittest
import cv2 as cv
import numpy as np
from instrument_geometry import (BOARD_UV, InstrumentLayout, project, raw_to_display_xy,
                                 display_to_raw_xy, instrument_to_display_xy, fuse_hands, overlay_polygons)
from tests.helpers import packet


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.raw = np.float32([[80, 80], [590, 130], [30, 430], [520, 460]])
        self.h = cv.getPerspectiveTransform(self.raw, BOARD_UV)
        self.inverse = np.linalg.inv(self.h)

    def test_mirror_round_trip(self):
        uv = np.array([[.1, .2], [-.2, 1.3], [.85, .5]])
        for mirrored in (True, False):
            display = instrument_to_display_xy(uv, self.inverse, 640, mirrored)
            raw = display_to_raw_xy(display, 640, mirrored)
            np.testing.assert_allclose(project(raw, self.h), uv, atol=1e-9)
        self.assertEqual(raw_to_display_xy([[0, 0]], 640, True).tolist(), [[639, 0]])

    def test_regions_and_hit_test_share_inverse(self):
        for flipped in (False, True):
            layout = InstrumentLayout(flip_u=flipped)
            p = packet(0)
            p['mirrored'] = True
            p['instrument']['homography_instrument_to_camera'] = self.inverse.tolist()
            shapes = overlay_polygons(p, layout)
            for name in map(str, range(1, 8)):
                recovered = project(display_to_raw_xy(shapes[name], 640, True), self.h)
                np.testing.assert_allclose(recovered, layout.regions()['degree_regions'][name], atol=1e-9)
                self.assertEqual(layout.chord_at(recovered.mean(axis=0)), name)
            self.assertTrue(layout.in_strum_band(layout.local((.78, .5))))

    def test_invalid_projection_and_full_shapes(self):
        for matrix, points in ((None, [[0, 0]]), (np.zeros((3, 3)), [[0, 0]]),
                               (np.eye(3), [[float('nan'), 0]]),
                               ([[1, 0, 0], [0, 1, 0], [0, 1, 0]], [[1, 0]])):
            self.assertIsNone(project(points, matrix))
        p = packet(0)
        instrument = {'homography_camera_to_instrument': self.h.tolist()}
        fuse_hands(p, instrument)
        for hand in p['hands']:
            self.assertEqual(np.shape(hand['landmarks_instrument_uv']), (21, 2))
            self.assertEqual(np.shape(hand['landmarks_world_m']), (21, 3))
        json.dumps(p, allow_nan=False)
        fuse_hands(p, {'homography_camera_to_instrument': None})
        self.assertTrue(all(h['landmarks_instrument_uv'] is None for h in p['hands']))
