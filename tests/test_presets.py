import unittest
import numpy as np
from instrument_preset import InstrumentPreset
from instrument_geometry import InstrumentLayout, overlay_polygons, project, display_to_raw_xy
from instrument_logic import InstrumentLogic
from tests.helpers import packet
from tests.plane_fixtures import six_marker_board


class PresetTests(unittest.TestCase):
    def test_concave_regions_outside_unit_square_and_shared_overlay(self):
        preset = InstrumentPreset('custom', {'C': [[-.5, .2], [.2, .2], [.2, .4], [-.2, .4], [-.2, .8], [-.5, .8]]},
                                  [[.6, 0], [1.3, 0], [1.3, 1], [.6, 1]], [[.9, .9], [.9, .1]])
        layout = InstrumentLayout(preset=preset)
        self.assertEqual(layout.chord_at((-.3, .6)), 'C')
        self.assertIsNone(layout.chord_at((.1, .6)))
        self.assertTrue(layout.in_strum_band((1.2, .5)))
        p = packet(0)
        p['instrument']['homography_instrument_to_camera'] = [[600, 0, 100], [0, 500, 80], [.1, -.1, 1]]
        shapes = overlay_polygons(p, layout)
        H = np.linalg.inv(p['instrument']['homography_instrument_to_camera'])
        for name, points in layout.regions()['chords'].items():
            np.testing.assert_allclose(project(shapes[name], H), points, atol=1e-9)

    def test_directed_vertical_line_crossing_uses_normal_motion(self):
        preset = InstrumentPreset('vertical', {'C': [[0, .2], [.4, .2], [.4, .8], [0, .8]]},
                                  [[.6, .1], [1., .1], [1., .9], [.6, .9]], [[.8, .9], [.8, .1]])
        for flipped in (False, True):
            layout = InstrumentLayout(preset=preset, flip_u=flipped)
            now = [0.]
            logic = InstrumentLogic(layout, clock=lambda: now[0])
            def step(t, u):
                now[0] = t
                return logic.process(packet(t, left=layout.local((.2, .5)), right=layout.local((u, .5))))
            step(0, .65)
            step(.11, .65)
            event = step(.2, .95)[1][0]
            self.assertEqual(event['direction'], 'down')
            self.assertEqual(event['chord'], 'C')
            self.assertEqual(step(.4, .65)[1][0]['direction'], 'up')

    def test_board_is_independent_and_bad_preset_rejected(self):
        board = six_marker_board()
        before = board.to_dict()
        InstrumentLayout(preset=InstrumentPreset.default(), flip_u=True)
        self.assertEqual(board.to_dict(), before)
        data = InstrumentPreset.default().to_dict()
        for change in ({'axis': [[0, 0], [0, 0]]}, {'chords': {'D': [[0, 0], [1, 0], [0, 1]]}},
                       {'strum': [[0, 0], [1, 1], [1, 0], [0, 1]]}, {'interaction': {'cooldown_s': -1}},
                       {'chord_frame': [[0, 0], [1, 0], [0, 1]]},
                       {'strum_frame': [[0, 0], [1, 1], [1, 0], [0, 1]]}):
            with self.assertRaises(ValueError):
                InstrumentPreset.from_dict(dict(data, **change))
